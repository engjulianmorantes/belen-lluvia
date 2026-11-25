"""Procesamiento multi-radar de volúmenes IRIS (.RAW) de la red IDEAM (s3://s3-radaresideam).

Por cada radar de C.RADARS y cada día local (UTC-5):
  1. Listar el día (carpetas D y D+1, filtrando por el sello UTC del nombre: el bucket ha usado
     carpetas por día UTC (ene-2026) y por día local (nov-2026)). Quitar duplicados por
     (sello, tamaño): Barrancabermeja publica cada volumen dos veces (.RAW7xxx/.RAWZxxx, mismo
     tamaño); Carimagua publica volúmenes DISTINTOS con el mismo sello (distinto tamaño).
  2. Elegir UNA tarea IRIS (atributo `scan_name` del archivo) probando los volúmenes de una
     ventana de 30 min: la de menor elevación que alcanza Belén; si ninguna lo alcanza, la de
     mayor alcance (solo vista regional). Se filtran los volúmenes del día por la "huella" de la
     tarea (posición en el ciclo de 5 min y tamaño) y se verifica `scan_name` al abrir cada uno.
  3. Remuestrear dBZ del barrido más bajo de esa tarea a dos mallas lat/lon (vecino más cercano
     en azimut y rango oblicuo, Tierra efectiva 4/3): local (Belén, 40 km) y regional.
  4. R = (Z/a)^(1/b) (Marshall-Palmer), acumulado con Δt = min(t_{i+1} − t_i, RADAR_MAX_DT_MIN).
  5. Compuesto "haz más bajo": en cada celda y paso de tiempo se toma el radar que cubre la celda
     con el centro del haz más cercano al suelo.

LIMITACIÓN: sin corrección de bloqueo orográfico, atenuación, banda brillante ni ajuste con
pluviómetros. Las láminas del radar son indicativas.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import config as C
import geo

UTC = dt.timezone.utc
COL = dt.timezone(dt.timedelta(hours=-5))
FNAME_RE = re.compile(r"^[A-Z]{3}(\d{12})\.RAW", re.I)
DBZ_FIELDS = ("DBZH", "DBZ", "reflectivity", "DBTH")

DBZ_LEVELS = [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65]
DBZ_COLORS = [(4, 233, 231), (1, 159, 244), (3, 0, 244), (2, 253, 2), (1, 197, 1),
              (0, 142, 0), (253, 248, 2), (229, 188, 0), (253, 149, 0), (253, 0, 0),
              (212, 0, 0), (188, 0, 0)]
ACC_LEVELS = [0.1, 10.0, 30.0, 50.0]          # clases SAB/IDIGER, mm
ACC_COLORS = [(52, 120, 246), (46, 170, 70), (245, 140, 20), (140, 60, 190)]


# ============================================================================ utilidades
def key_time_utc(key: str) -> dt.datetime | None:
    m = FNAME_RE.match(os.path.basename(key))
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d%H%M%S").replace(tzinfo=UTC)


def cycle_offset_s(t: dt.datetime) -> int:
    """Posición del volumen dentro del ciclo de 5 min, s (0–299)."""
    return (t.minute % 5) * 60 + t.second


def slot_name(t: dt.datetime, step_min: int) -> str:
    m = (t.hour * 60 + t.minute) // step_min * step_min
    return f"{m // 60:02d}{m % 60:02d}"


def dedupe(objs):
    """objs: [{'Key','Size'}]. Quita copias idénticas (mismo sello y tamaño)."""
    seen, out = set(), []
    for o in sorted(objs, key=lambda o: o["Key"]):
        t = key_time_utc(o["Key"])
        if t is None:
            continue
        sig = (t, int(o.get("Size", -1)))
        if sig not in seen:
            seen.add(sig)
            out.append(o)
    return out


def dbz_to_rate_mmh(dbz):
    """Marshall-Palmer: Z = a·R^b → R = (10^(dBZ/10)/a)^(1/b) [mm/h]."""
    dbz = np.asarray(dbz, dtype=float)
    with np.errstate(invalid="ignore"):
        z = 10.0 ** (np.minimum(dbz, C.RADAR_HAIL_CAP_DBZ) / 10.0)
        r = (z / C.ZR_A) ** (1.0 / C.ZR_B)
    return np.where(np.isfinite(dbz) & (dbz >= C.RADAR_MIN_DBZ), r, 0.0)


def enc_dbz(dbz):
    """dBZ → uint8 (0 = sin eco/NaN; paso 0,5 dB desde −32 dBZ)."""
    v = np.rint((np.asarray(dbz, float) + 32.0) * 2.0)
    v = np.where(np.isfinite(v), np.clip(v, 1, 255), 0)
    return v.astype(np.uint8)


def dec_dbz(u8):
    u = np.asarray(u8, float)
    return np.where(u > 0, u / 2.0 - 32.0, np.nan)


# ============================================================================ S3
def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED,
                                                                       max_pool_connections=16))


def list_folder(s3, site, day):
    prefix = f"{C.RADAR_PREFIX}/{day:%Y/%m/%d}/{site}/"
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=C.RADAR_BUCKET, Prefix=prefix):
        out += [{"Key": o["Key"], "Size": o["Size"]} for o in page.get("Contents", [])]
    return out


def objs_for_local_day(s3, site, day_local):
    t0 = dt.datetime.combine(day_local, dt.time(0), COL)
    t1 = t0 + dt.timedelta(days=1)
    raw = list_folder(s3, site, day_local) + list_folder(s3, site, day_local + dt.timedelta(days=1))
    objs = [o for o in dedupe(raw) if t0 <= key_time_utc(o["Key"]) < t1]
    objs.sort(key=lambda o: key_time_utc(o["Key"]))
    complete = bool(objs) and key_time_utc(objs[-1]["Key"]) >= t1 - dt.timedelta(minutes=20)
    return objs, complete


def folder_exists(s3, site, day):
    r = s3.list_objects_v2(Bucket=C.RADAR_BUCKET, Prefix=f"{C.RADAR_PREFIX}/{day:%Y/%m/%d}/{site}/", MaxKeys=1)
    return r.get("KeyCount", 0) > 0


def last_seen(s3, site, today, cached=None):
    """Último día con carpeta del radar. Diario hasta 30 d, luego semanal hasta ~18 meses.
    Si hay un valor en caché, solo se revisan los días posteriores a él."""
    stop = dt.date.fromisoformat(cached) if cached else None
    steps = list(range(0, 31)) + list(range(35, 550, 7))
    for k in steps:
        d = today - dt.timedelta(days=k)
        if stop and d <= stop:
            return cached
        if folder_exists(s3, site, d):
            return d.isoformat()
    return cached


# ============================================================================ IRIS
def read_volume(path, site_ground_m):
    """Devuelve dict con tarea, posición y barrido elegido, o None si no hay campo dBZ.
    Barrido: el de menor elevación que alcanza site_ground_m; si ninguno, el de mayor alcance."""
    import xarray as xr
    tree = xr.open_datatree(path, engine="iris")
    root = tree.to_dataset()
    info = {"tarea": str(tree.attrs.get("scan_name", "")).strip() or "?",
            "lat": float(root["latitude"].values), "lon": float(root["longitude"].values),
            "alt": float(root["altitude"].values)}
    cands = []
    for name in tree.children:
        if not name.startswith("sweep_"):
            continue
        ds = tree[name].to_dataset()
        field = next((f for f in DBZ_FIELDS if f in ds.data_vars), None)
        if field is None:
            continue
        elev = float(ds["sweep_fixed_angle"].values)
        reach = float(geo.ground_range_m(float(ds["range"].values[-1]), elev))
        cands.append((ds, field, elev, reach))
    if not cands:
        return None
    covering = [c for c in cands if c[3] >= site_ground_m]
    ds, field, elev, reach = (min(covering, key=lambda c: (c[2], -c[3])) if covering
                              else max(cands, key=lambda c: (c[3], -c[2])))
    info.update(ds=ds, field=field, elev=elev, reach_m=reach, cubre_sitio=bool(covering))
    return info


def polar_to_grid(ds, field, elev_deg, grid_az, grid_ground, max_az_gap_deg=1.0):
    """Vecino más cercano en azimut (ordenado, con vuelta en 360°) y en rango oblicuo.
    Devuelve (dBZ en la malla con NaN donde no hay eco o no hay cobertura, máscara de cobertura)."""
    az = np.asarray(ds["azimuth"].values, dtype=float) % 360.0
    rng = np.asarray(ds["range"].values, dtype=float)
    data = np.asarray(ds[field].values, dtype=float)
    order = np.argsort(az)
    az_s = az[order]
    ext = np.concatenate([az_s[-1:] - 360.0, az_s, az_s[:1] + 360.0])
    idx_ext = np.concatenate([order[-1:], order, order[:1]])
    pos = np.clip(np.searchsorted(ext, grid_az), 1, ext.size - 1)
    left, right = ext[pos - 1], ext[pos]
    use_left = (grid_az - left) <= (right - grid_az)
    j = np.where(use_left, pos - 1, pos)
    ia = idx_ext[j]
    az_ok = np.abs(grid_az - ext[j]) <= max_az_gap_deg
    r_slant = geo.slant_from_ground_m(grid_ground, elev_deg)
    dr = rng[1] - rng[0]
    ir = np.rint((r_slant - rng[0]) / dr)
    r_ok = np.isfinite(ir) & (ir >= 0) & (ir < rng.size)
    ir = np.where(r_ok, ir, 0).astype(int)
    cover = az_ok & r_ok
    out = data[ia, ir]
    out[~cover] = np.nan
    return out, cover


# ============================================================================ mallas
class Grid:
    def __init__(self, name, lats, lons, bounds, inside, step_min):
        self.name, self.lats, self.lons, self.bounds, self.inside = name, lats, lons, bounds, inside
        self.step_min = step_min
        self.LAT, self.LON = np.meshgrid(lats, lons, indexing="ij")


def local_grid():
    lats, lons, bounds, inside = geo.make_grid(C.SITE_LAT_DEG, C.SITE_LON_DEG, C.AOI_RADIUS_KM, C.GRID_STEP_DEG)
    return Grid("local", lats, lons, bounds, inside, C.RADAR_FRAME_STEP_MIN)


def regional_grid():
    (s, w), (n, e) = C.REGION_BOUNDS
    st = C.REGION_STEP_DEG
    lats = np.arange(n - st / 2, s, -st)
    lons = np.arange(w + st / 2, e, st)
    bounds = [[float(lats[-1] - st / 2), float(lons[0] - st / 2)], [float(lats[0] + st / 2), float(lons[-1] + st / 2)]]
    return Grid("regional", lats, lons, bounds, np.ones((lats.size, lons.size), bool), C.REGION_FRAME_STEP_MIN)


# ============================================================================ imágenes
def classify_rgba(values, levels, colors, mask, alpha=200):
    v = np.asarray(values, float)
    rgba = np.zeros(v.shape + (4,), np.uint8)
    with np.errstate(invalid="ignore"):
        for i, lo in enumerate(levels):
            hi = levels[i + 1] if i + 1 < len(levels) else np.inf
            m = (v >= lo) & (v < hi) & mask
            rgba[m, :3] = colors[i]
            rgba[m, 3] = alpha
    return rgba


def save_png(rgba, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ============================================================================ un radar, un día
def process_radar_day(site, day_local, objs, complete, day_dir, s3, grids, fetch_local=None):
    """Procesa un radar en un día. Escribe PNG/meta y grids.npz en day_dir/<sitio>/.
    fetch_local(key)->ruta permite pruebas sin S3."""
    cfg = C.RADARS[site]
    out = os.path.join(day_dir, site)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f"rad_{site}_")
    geo_cache = {}
    meta = {"sitio": site, "dia_local": day_local.isoformat(), "zona_horaria": "UTC-05:00",
            "completo": bool(complete), "volumenes_listados": len(objs), "omitidos": []}

    def fetch(o):
        if fetch_local:
            return o, fetch_local(o["Key"])
        p = os.path.join(tmp, os.path.basename(o["Key"]) + f".{o['Size']}")
        s3.download_file(C.RADAR_BUCKET, o["Key"], p)
        return o, p

    def cleanup(p):
        if not fetch_local and os.path.exists(p):
            os.remove(p)

    def site_ground(lat, lon):
        return float(geo.geodesic(lat, lon, C.SITE_LAT_DEG, C.SITE_LON_DEG)[1])

    try:
        # ---------- 1) prueba de tareas en una ventana de 30 min (mediodía local, o la mitad del día)
        times = [key_time_utc(o["Key"]) for o in objs]
        mid = dt.datetime.combine(day_local, dt.time(12), COL)
        win = [o for o, t in zip(objs, times) if mid <= t < mid + dt.timedelta(minutes=30)]
        if len(win) < 3:
            k0 = len(objs) // 2
            win = objs[k0:k0 + 30]
        tareas = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for o, p in ex.map(fetch, win):
                try:
                    v = read_volume(p, cfg.get("_ground_m", 0.0) or 1e9)
                    if v is None:
                        continue
                    if "_ground_m" not in cfg:
                        cfg["_ground_m"] = site_ground(v["lat"], v["lon"])
                        v = read_volume(p, cfg["_ground_m"])
                    d = tareas.setdefault(v["tarea"], {"n_prueba": 0, "offsets_s": set(), "tam": [], "elev_deg": v["elev"],
                                                       "alcance_km": round(v["reach_m"] / 1000, 1), "cubre_belen": v["cubre_sitio"]})
                    d["n_prueba"] += 1
                    d["offsets_s"].add(cycle_offset_s(key_time_utc(o["Key"])))
                    d["tam"].append(o["Size"])
                except Exception as e:
                    meta["omitidos"].append({"key": o["Key"], "motivo": f"prueba: {type(e).__name__}: {e}"[:200]})
                finally:
                    cleanup(p)
        if not tareas:
            meta["estado"] = "sin volúmenes legibles en la ventana de prueba"
            return meta, None
        cub = [k for k, v in tareas.items() if v["cubre_belen"]]
        best = (min(cub, key=lambda k: (tareas[k]["elev_deg"], -tareas[k]["alcance_km"])) if cub
                else max(tareas, key=lambda k: (tareas[k]["alcance_km"], -tareas[k]["elev_deg"])))
        bt = tareas[best]
        offs, tmin, tmax = sorted(bt["offsets_s"]), min(bt["tam"]), max(bt["tam"])
        meta["tareas_probadas"] = {k: {**{kk: vv for kk, vv in v.items() if kk not in ("offsets_s", "tam")},
                                       "offsets_s": sorted(v["offsets_s"]), "tam_bytes": [min(v["tam"]), max(v["tam"])],
                                       "elegida": k == best} for k, v in tareas.items()}

        def near_offset(o):
            x = cycle_offset_s(key_time_utc(o["Key"]))
            return min(min(abs(x - y), 300 - abs(x - y)) for y in offs) <= 25

        cand = [o for o in objs if near_offset(o) and 0.5 * tmin <= o["Size"] <= 2.0 * tmax]
        meta["tarea"] = best
        meta["volumenes_candidatos"] = len(cand)

        # ---------- 2) proceso
        scans = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i0 in range(0, len(cand), 16):
                for o, p in ex.map(fetch, cand[i0:i0 + 16]):
                    t = key_time_utc(o["Key"])
                    try:
                        v = read_volume(p, cfg["_ground_m"])
                        if v is None or v["tarea"] != best:
                            meta["omitidos"].append({"key": o["Key"], "motivo": f"otra tarea ({None if v is None else v['tarea']})"})
                            continue
                        if abs(v["elev"] - bt["elev_deg"]) > 0.05:
                            meta["omitidos"].append({"key": o["Key"], "motivo": f"elevación {v['elev']:.2f}° ≠ {bt['elev_deg']:.2f}°"})
                            continue
                        gkey = (round(v["lat"], 5), round(v["lon"], 5))
                        res = {}
                        for g in grids:
                            ck = (g.name, gkey)
                            if ck not in geo_cache:
                                geo_cache[ck] = geo.geodesic(v["lat"], v["lon"], g.LAT, g.LON)
                            az, gr = geo_cache[ck]
                            dbz_g, cov_g = polar_to_grid(v["ds"], v["field"], v["elev"], az, gr)
                            res[g.name] = (dbz_g.astype(np.float32), cov_g)
                        scans.append((t, o["Key"], res, v))
                    except Exception as e:
                        meta["omitidos"].append({"key": o["Key"], "motivo": f"{type(e).__name__}: {e}"[:200]})
                    finally:
                        cleanup(p)
        if not scans:
            meta["estado"] = "sin volúmenes válidos de la tarea elegida"
            return meta, None

        scans.sort(key=lambda s: s[0])
        tt = [s[0] for s in scans]
        gaps = [(tt[i + 1] - tt[i]).total_seconds() / 60 for i in range(len(tt) - 1)]
        med = float(np.median(gaps)) if gaps else 5.0
        v0 = scans[0][3]
        rlat, rlon, ralt, elev = v0["lat"], v0["lon"], v0["alt"], v0["elev"]

        npz, gmeta = {}, {}
        for g in grids:
            acc = np.zeros(g.LAT.shape)
            cover = np.zeros(g.LAT.shape, bool)
            frames, fr_u8, fr_names, last = [], [], [], None
            for i, (t, k, res, _) in enumerate(scans):
                dbz, cov = res[g.name]
                cover |= cov
                dtm = min(gaps[i] if i < len(gaps) else med, C.RADAR_MAX_DT_MIN)
                acc += dbz_to_rate_mmh(dbz) * dtm / 60.0
                sn = slot_name(t, g.step_min)
                if sn != last:
                    last = sn
                    save_png(classify_rgba(dbz, DBZ_LEVELS, DBZ_COLORS, g.inside), os.path.join(out, g.name, "frames", f"{sn}.png"))
                    frames.append({"slot": sn, "t_utc": t.isoformat()})
                    fr_u8.append(enc_dbz(dbz))
                    fr_names.append(sn)
            az, gr = geo_cache[(g.name, (round(rlat, 5), round(rlon, 5)))]
            beam = geo.beam_height_m(geo.slant_from_ground_m(gr, elev), elev) + ralt
            beam = np.where(cover, beam, np.nan)
            save_png(classify_rgba(acc, ACC_LEVELS, ACC_COLORS, g.inside & cover, alpha=170), os.path.join(out, g.name, "acumulado.png"))
            npz[f"{g.name}_frames"] = np.stack(fr_u8)
            npz[f"{g.name}_slots"] = np.array(fr_names)
            npz[f"{g.name}_acc"] = acc.astype(np.float32)
            npz[f"{g.name}_beam"] = beam.astype(np.float32)
            gmeta[g.name] = {"bounds": g.bounds, "frames": frames, "acumulado_png": f"{site}/{g.name}/acumulado.png",
                             "cobertura_frac": round(float(cover[g.inside].mean()), 3),
                             "acumulado_max_mm": round(float(np.max(acc[g.inside & cover])) if (g.inside & cover).any() else 0.0, 1)}
            if g.name == "local":
                ic, jc = len(g.lats) // 2, len(g.lons) // 2
                near5 = geo.geodesic(C.SITE_LAT_DEG, C.SITE_LON_DEG, g.LAT, g.LON)[1] <= 5000.0
                gmeta["local"]["acumulado_belen_mm"] = round(float(acc[ic, jc]), 1) if cover[ic, jc] else None
                gmeta["local"]["serie_belen"] = [{
                    "t_utc": t.isoformat(),
                    "dbz_belen": None if not np.isfinite(res["local"][0][ic, jc]) else round(float(res["local"][0][ic, jc]), 1),
                    "r_max5km_mmh": round(float(np.max(dbz_to_rate_mmh(res["local"][0])[near5])), 2)}
                    for t, _, res, _ in scans]
        np.savez_compressed(os.path.join(out, "grids.npz"), **npz)

        dist = float(geo.geodesic(rlat, rlon, C.SITE_LAT_DEG, C.SITE_LON_DEG)[1])
        r_sl = float(geo.slant_from_ground_m(dist, elev))
        cubre = bool(bt["cubre_belen"])
        meta.update({
            "estado": "ok",
            "radar": {"sitio": site, "lat": rlat, "lon": rlon, "alt_m": ralt, "campo": v0["field"],
                      "elevacion_deg": round(elev, 2), "alcance_km": round(v0["reach_m"] / 1000, 1),
                      "coordenadas": "leídas del archivo IRIS"},
            "geometria_belen": {
                "distancia_km": round(dist / 1000, 1), "cubre_belen": cubre,
                "altura_centro_haz_msnm": round(float(geo.beam_height_m(r_sl, elev)) + ralt) if cubre and np.isfinite(r_sl) else None,
                "altura_sitio_msnm": C.SITE_ALT_M,
                "modelo": "Tierra efectiva 4/3 (Doviak & Zrnić 1993, ec. 2.28b)"},
            "volumenes_usados": len(scans), "brecha_max_min": round(max(gaps), 1) if gaps else None,
            "zr": {"a": C.ZR_A, "b": C.ZR_B, "ref": "Marshall & Palmer (1948)", "umbral_dbz": C.RADAR_MIN_DBZ,
                   "tope_granizo_dbz": C.RADAR_HAIL_CAP_DBZ, "dt_max_min": C.RADAR_MAX_DT_MIN},
            "mallas": gmeta,
            "fuente": {"nombre": "IDEAM – Red de radares (AWS Open Data)", "licencia": "CC BY 4.0",
                       "url": "https://registry.opendata.aws/ideam-radares/", "bucket": f"s3://{C.RADAR_BUCKET}",
                       "archivos": [s[1] for s in scans]},
            "procesado_utc": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        })
        if meta["geometria_belen"]["altura_centro_haz_msnm"] is not None:
            meta["geometria_belen"]["margen_sobre_sitio_m"] = meta["geometria_belen"]["altura_centro_haz_msnm"] - round(C.SITE_ALT_M)
        meta["omitidos"] = meta["omitidos"][:200]
        return meta, npz
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)


# ============================================================================ compuesto
def build_composite(day_dir, sites, grids):
    """Compuesto 'haz más bajo' a partir de los grids.npz de cada radar."""
    data = {}
    for s in sites:
        p = os.path.join(day_dir, s, "grids.npz")
        if os.path.exists(p):
            data[s] = dict(np.load(p))
    out = os.path.join(day_dir, "compuesto")
    shutil.rmtree(out, ignore_errors=True)
    meta = {"metodo": "haz más bajo: en cada celda, el radar que la cubre con el centro del haz más cercano al suelo",
            "radares": sorted(data), "mallas": {}}
    for g in grids:
        avail = [s for s in data if f"{g.name}_beam" in data[s] and np.isfinite(data[s][f"{g.name}_beam"]).any()]
        if len(avail) < 2:
            continue
        beams = np.stack([np.where(np.isfinite(data[s][f"{g.name}_beam"]), data[s][f"{g.name}_beam"], np.inf) for s in avail])
        owner = np.argmin(beams, axis=0)
        covered = np.isfinite(np.min(beams, axis=0))
        # dominio (qué radar manda en cada celda)
        rgba = np.zeros(owner.shape + (4,), np.uint8)
        for i, s in enumerate(avail):
            m = (owner == i) & covered & g.inside
            rgba[m, :3] = _hex_rgb(C.RADARS[s]["color"])
            rgba[m, 3] = 110
        save_png(rgba, os.path.join(out, g.name, "dominio.png"))
        # acumulado
        acc = np.zeros(owner.shape)
        for i, s in enumerate(avail):
            acc = np.where(owner == i, data[s][f"{g.name}_acc"], acc)
        save_png(classify_rgba(acc, ACC_LEVELS, ACC_COLORS, g.inside & covered, alpha=170), os.path.join(out, g.name, "acumulado.png"))
        # cuadros por paso de tiempo
        slots = sorted(set().union(*[set(data[s][f"{g.name}_slots"].tolist()) for s in avail]))
        idx = {s: {sn: k for k, sn in enumerate(data[s][f"{g.name}_slots"].tolist())} for s in avail}
        frames = []
        for sn in slots:
            have = [i for i, s in enumerate(avail) if sn in idx[s]]
            b = np.stack([beams[i] if i in have else np.full(owner.shape, np.inf) for i in range(len(avail))])
            own = np.argmin(b, axis=0)
            dbz = np.full(owner.shape, np.nan)
            for i in have:
                s = avail[i]
                dbz = np.where(own == i, dec_dbz(data[s][f"{g.name}_frames"][idx[s][sn]]), dbz)
            dbz[~np.isfinite(np.min(b, axis=0))] = np.nan
            save_png(classify_rgba(dbz, DBZ_LEVELS, DBZ_COLORS, g.inside), os.path.join(out, g.name, "frames", f"{sn}.png"))
            frames.append({"slot": sn, "radares": [avail[i] for i in have]})
        meta["mallas"][g.name] = {"bounds": g.bounds, "radares": avail, "frames": frames,
                                  "acumulado_png": f"compuesto/{g.name}/acumulado.png",
                                  "dominio_png": f"compuesto/{g.name}/dominio.png",
                                  "fraccion_por_radar": {s: round(float(((owner == i) & covered & g.inside).sum() / max((covered & g.inside).sum(), 1)), 3)
                                                         for i, s in enumerate(avail)}}
        if g.name == "local":
            ic, jc = len(g.lats) // 2, len(g.lons) // 2
            meta["mallas"]["local"]["acumulado_belen_mm"] = round(float(acc[ic, jc]), 1)
            meta["mallas"]["local"]["radar_en_belen"] = avail[int(owner[ic, jc])]
    if meta["mallas"]:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    return meta


# ============================================================================ índice y ejecución
def _load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def update_index(out_root, estado_radares):
    rdir = os.path.join(out_root, "radar")
    os.makedirs(rdir, exist_ok=True)
    days = sorted(d for d in os.listdir(rdir) if d.isdigit())
    keep_fixed = set(C.RADAR_KEEP_EXTRA_DAYS) | set(estado_radares.get("_dias_fijos", []))
    recent = [d for d in days if d not in keep_fixed][-C.RADAR_KEEP_DAYS:]
    for d in days:
        if d not in recent and d not in keep_fixed:
            shutil.rmtree(os.path.join(rdir, d), ignore_errors=True)
    idx = []
    for d in sorted(set(recent) | (keep_fixed & set(days))):
        ent = {"dia": d, "radares": {}, "compuesto": None}
        for s in C.RADARS:
            m = _load(os.path.join(rdir, d, s, "meta.json"), None)
            if m:
                ent["radares"][s] = {"estado": m.get("estado"), "completo": m.get("completo"),
                                     "volumenes": m.get("volumenes_usados"),
                                     "cubre_belen": (m.get("geometria_belen") or {}).get("cubre_belen")}
        cm = _load(os.path.join(rdir, d, "compuesto", "meta.json"), None)
        if cm:
            ent["compuesto"] = sorted(cm["mallas"])
        idx.append(ent)
    radares = []
    for s, cfg in C.RADARS.items():
        e = estado_radares.get(s, {})
        if not isinstance(e, dict):
            continue
        radares.append({"sitio": s, "color": cfg["color"], "lat": e.get("lat", cfg["lat"]), "lon": e.get("lon", cfg["lon"]),
                        "coordenadas": e.get("coordenadas", cfg["coord_fuente"]), "alcance_km": C.RADAR_NOMINAL_RANGE_KM,
                        "ultimo_dia_visto": e.get("ultimo_dia_visto"), "verificado": e.get("verificado")})
    with open(os.path.join(rdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"radares": radares, "dias": idx, "region_bounds": regional_grid().bounds}, f, ensure_ascii=False)
    return idx


def run(out_root, today_local=None, fecha=None):
    today_local = today_local or dt.datetime.now(COL).date()
    s3 = _s3()
    rdir = os.path.join(out_root, "radar")
    os.makedirs(rdir, exist_ok=True)
    est_path = os.path.join(rdir, "estado_radares.json")
    est = _load(est_path, {})
    grids = [local_grid(), regional_grid()]
    resumen, changed_days = {}, set()
    if fecha:  # día histórico pedido a mano: se conserva aunque salga de la ventana de días recientes
        est.setdefault("_dias_fijos", [])
        if f"{fecha:%Y%m%d}" not in est["_dias_fijos"]:
            est["_dias_fijos"].append(f"{fecha:%Y%m%d}")

    for site in C.RADARS:
        e = est.setdefault(site, {})
        if e.get("verificado") != today_local.isoformat():
            e["ultimo_dia_visto"] = last_seen(s3, site, today_local, e.get("ultimo_dia_visto"))
            e["verificado"] = today_local.isoformat()
        if fecha:
            days = [fecha]
        else:
            days = []
            for back in range(1, C.RADAR_MAX_LOOKBACK_DAYS + 1):
                d = today_local - dt.timedelta(days=back)
                if folder_exists(s3, site, d) or folder_exists(s3, site, d + dt.timedelta(days=1)):
                    days = [d]
                    break
        if not days:
            resumen[site] = {"estado": "sin datos recientes", "ultimo_dia_visto": e.get("ultimo_dia_visto")}
            continue
        d = days[0]
        objs, complete = objs_for_local_day(s3, site, d)
        if not objs:
            resumen[site] = {"estado": f"sin volúmenes el {d}", "ultimo_dia_visto": e.get("ultimo_dia_visto")}
            continue
        day_dir = os.path.join(rdir, f"{d:%Y%m%d}")
        old = _load(os.path.join(day_dir, site, "meta.json"), {})
        if old.get("estado") == "ok" and old.get("completo") and old.get("volumenes_listados") == len(objs):
            resumen[site] = {"estado": "sin cambios", "dia": d.isoformat()}
            continue
        meta, _ = process_radar_day(site, d, objs, complete, day_dir, s3, grids)
        if meta.get("radar"):
            e.update(lat=meta["radar"]["lat"], lon=meta["radar"]["lon"], coordenadas="leídas del archivo IRIS")
        resumen[site] = {"estado": meta.get("estado"), "dia": d.isoformat(), "tarea": meta.get("tarea"),
                         "volumenes": meta.get("volumenes_usados")}
        changed_days.add(f"{d:%Y%m%d}")

    for d in changed_days:
        build_composite(os.path.join(rdir, d), list(C.RADARS), grids)
    with open(est_path, "w", encoding="utf-8") as f:
        json.dump(est, f, ensure_ascii=False)
    update_index(out_root, est)
    return resumen
