"""Procesamiento de volúmenes IRIS (.RAW) del radar IDEAM de Barrancabermeja.

Flujo por día local (UTC-5):
  1. Listar en S3 los volúmenes cuyo sello de tiempo (en el nombre, UTC) cae en el día local.
  2. Por volumen: abrir con xradar (engine IRIS), escoger el barrido de menor elevación
     cuyo alcance cubra toda la ventana de Belén, remuestrear dBZ a la malla lat/lon
     por vecino más cercano en (azimut, rango oblicuo).
  3. R = (Z/a)^(1/b)  (Marshall-Palmer, a=200, b=1.6), acumulación por regla rectangular
     hacia adelante con Δt = min(t_{i+1} − t_i, RADAR_MAX_DT_MIN).
  4. PNG de reflectividad cada RADAR_FRAME_STEP_MIN, PNG de acumulado diario y meta.json.

LIMITACIÓN: sin corrección de bloqueo orográfico, atenuación ni calibración con
pluviómetros. Los mm del radar sobre Belén son indicativos, no medidos.
"""
from __future__ import annotations

import datetime as dt
import json
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

# Escala de reflectividad (dBZ) tipo NWS: límite inferior de cada clase y color RGB
DBZ_LEVELS = [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65]
DBZ_COLORS = [(4, 233, 231), (1, 159, 244), (3, 0, 244), (2, 253, 2), (1, 197, 1),
              (0, 142, 0), (253, 248, 2), (229, 188, 0), (253, 149, 0), (253, 0, 0),
              (212, 0, 0), (188, 0, 0)]
# Clases de acumulado diario (mm) — mismas del visor SAB/IDIGER
ACC_LEVELS = [0.1, 10.0, 30.0, 50.0]
ACC_COLORS = [(52, 120, 246), (46, 170, 70), (245, 140, 20), (140, 60, 190)]


# ----------------------------------------------------------------------------- S3
def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def list_keys_utc_day(s3, day_utc: dt.date) -> list[str]:
    prefix = f"{C.RADAR_PREFIX}/{day_utc:%Y/%m/%d}/{C.RADAR_SITE}/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=C.RADAR_BUCKET, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def key_time_utc(key: str) -> dt.datetime | None:
    m = FNAME_RE.match(os.path.basename(key))
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d%H%M%S").replace(tzinfo=UTC)


def task_class(t: dt.datetime) -> int:
    """El radar corre 4 tareas por ciclo de 5 min (observado nov-2026: :x0:04, :x1:08, :x2:39, :x3:17).
    El minuto módulo 5 identifica la tarea sin abrir el archivo."""
    return t.minute % 5


def dedupe(keys):
    """Cada volumen se publica DOS veces con igual tamaño/ETag (extensiones .RAW7xxx y .RAWZxxx).
    Se conserva una clave por sello de tiempo."""
    seen, out = set(), []
    for k in sorted(keys):
        t = key_time_utc(k)
        if t and t not in seen:
            seen.add(t)
            out.append(k)
    return out


def keys_for_local_day(s3, day_local: dt.date):
    """Volúmenes del día local [00:00, 24:00) UTC-5. Las carpetas del bucket se organizan por día
    LOCAL (la carpeta D contiene sellos UTC de ~05 h del día D a ~05 h del día D+1); por robustez
    se listan D y D+1 y se filtra por el sello del nombre (UTC).
    Devuelve (keys_ordenadas_sin_duplicados, completo)."""
    t0 = dt.datetime.combine(day_local, dt.time(0), COL)
    t1 = t0 + dt.timedelta(days=1)
    raw = list_keys_utc_day(s3, day_local) + list_keys_utc_day(s3, day_local + dt.timedelta(days=1))
    keys = [k for k in dedupe(raw) if t0 <= key_time_utc(k) < t1]
    keys.sort(key=key_time_utc)
    complete = bool(keys) and key_time_utc(keys[-1]) >= t1 - dt.timedelta(minutes=20)
    return keys, complete


def find_latest_local_day(s3, today_local: dt.date):
    for back in range(1, C.RADAR_MAX_LOOKBACK_DAYS + 1):
        d = today_local - dt.timedelta(days=back)
        keys, complete = keys_for_local_day(s3, d)
        if keys:
            return d, keys, complete
    return None, [], False


# ------------------------------------------------------------------- lectura IRIS
def open_lowest_sweep(path: str, need_ground_range_m: float):
    """Abre el archivo y devuelve (dataset_barrido, nombre_campo, elevación_°, lat, lon, alt).
    Escoge el barrido de menor elevación cuyo rango cubre need_ground_range_m."""
    import xarray as xr
    tree = xr.open_datatree(path, engine="iris")
    root = tree.to_dataset()
    lat = float(root["latitude"].values)
    lon = float(root["longitude"].values)
    alt = float(root["altitude"].values)
    best = None
    for name in tree.children:
        if not name.startswith("sweep_"):
            continue
        ds = tree[name].to_dataset()
        field = next((f for f in DBZ_FIELDS if f in ds.data_vars), None)
        if field is None:
            continue
        elev = float(ds["sweep_fixed_angle"].values)
        rmax = float(ds["range"].values[-1])
        if geo.ground_range_m(rmax, elev) < need_ground_range_m:
            continue
        if best is None or elev < best[2]:
            best = (ds, field, elev)
    if best is None:
        return None
    ds, field, elev = best
    return ds, field, elev, lat, lon, alt


def polar_to_grid(ds, field, elev_deg, grid_az, grid_ground_m, max_az_gap_deg=1.0):
    """Vecino más cercano en azimut y rango oblicuo. Devuelve dBZ en la malla (NaN fuera)."""
    az = np.asarray(ds["azimuth"].values, dtype=float)
    rng = np.asarray(ds["range"].values, dtype=float)
    data = np.asarray(ds[field].values, dtype=float)  # (n_az, n_rng)
    # índice de azimut: diferencia angular mínima (con vuelta en 360°)
    d = np.abs(((grid_az[..., None] - az[None, None, :]) + 180.0) % 360.0 - 180.0)
    ia = np.argmin(d, axis=-1)
    az_ok = np.take_along_axis(d, ia[..., None], axis=-1)[..., 0] <= max_az_gap_deg
    # índice de rango: distancia sobre superficie → rango oblicuo
    r_slant = geo.slant_from_ground_m(grid_ground_m, elev_deg)
    dr = rng[1] - rng[0]
    ir = np.rint((r_slant - rng[0]) / dr)
    r_ok = np.isfinite(ir) & (ir >= 0) & (ir < rng.size)
    ir = np.where(r_ok, ir, 0).astype(int)
    out = data[ia, ir]
    out[~(az_ok & r_ok)] = np.nan
    return out


def dbz_to_rate_mmh(dbz):
    """Marshall-Palmer: Z = a·R^b  →  R = (10^(dBZ/10) / a)^(1/b)   [mm/h]."""
    dbz = np.asarray(dbz, dtype=float)
    z = 10.0 ** (np.minimum(dbz, C.RADAR_HAIL_CAP_DBZ) / 10.0)
    r = (z / C.ZR_A) ** (1.0 / C.ZR_B)
    r = np.where(np.isfinite(dbz) & (dbz >= C.RADAR_MIN_DBZ), r, 0.0)
    return r


# -------------------------------------------------------------------- imágenes
def _classify_rgba(values, levels, colors, inside, alpha=200):
    v = np.asarray(values, float)
    rgba = np.zeros(v.shape + (4,), np.uint8)
    for i, lo in enumerate(levels):
        hi = levels[i + 1] if i + 1 < len(levels) else np.inf
        m = (v >= lo) & (v < hi) & inside
        rgba[m, :3] = colors[i]
        rgba[m, 3] = alpha
    return rgba


def save_png(rgba, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


# -------------------------------------------------------------------- proceso
def process_local_day(day_local, keys, complete, out_root, s3=None, local_files=None):
    """Procesa un día. local_files: lista de rutas locales (pruebas) en lugar de S3."""
    lats, lons, bounds, inside = geo.make_grid(C.SITE_LAT_DEG, C.SITE_LON_DEG, C.AOI_RADIUS_KM, C.GRID_STEP_DEG)
    ic, jc = len(lats) // 2, len(lons) // 2  # celda de Belén
    LAT, LON = np.meshgrid(lats, lons, indexing="ij")
    near5 = geo.geodesic(C.SITE_LAT_DEG, C.SITE_LON_DEG, LAT, LON)[1] <= 5000.0

    day_dir = os.path.join(out_root, "radar", f"{day_local:%Y%m%d}")
    os.makedirs(day_dir, exist_ok=True)
    acc = np.zeros(LAT.shape)
    series, frames, used_keys, skipped = [], [], [], []
    radar_geom = None
    grid_az = grid_ground = None
    last_frame_slot = None

    items = local_files if local_files is not None else keys
    tmp = tempfile.mkdtemp(prefix="radar_")
    try:
        def fetch(k):
            if local_files is not None:
                return k, k
            p = os.path.join(tmp, os.path.basename(k))
            s3.download_file(C.RADAR_BUCKET, k, p)
            return k, p

        def cleanup(p):
            if local_files is None and os.path.exists(p):
                os.remove(p)

        def need_range(p):
            nonlocal radar_geom, grid_az, grid_ground
            if radar_geom is None:  # primera lectura: posición real del radar
                import xarray as xr
                root = xr.open_datatree(p, engine="iris").to_dataset()
                radar_geom = (float(root["latitude"].values), float(root["longitude"].values))
                grid_az, grid_ground = geo.geodesic(radar_geom[0], radar_geom[1], LAT, LON)
            return float(np.nanmax(grid_ground[inside]))

        # 1) Selección de tarea: se prueba un volumen de cada tarea (minuto mód 5) y se usa
        #    solo la que cubre toda la ventana con la menor elevación. Mezclar tareas daría
        #    elevaciones distintas entre cuadros y sesgo en el acumulado.
        tareas = {}
        if local_files is None:
            by_cls = {}
            for k in items:
                by_cls.setdefault(task_class(key_time_utc(k)), []).append(k)
            for c, ks in sorted(by_cls.items()):
                k, p = fetch(ks[len(ks) // 2])
                try:
                    res = open_lowest_sweep(p, need_range(p))
                    tareas[c] = {"n": len(ks), "muestra": k, "cubre": res is not None,
                                 "elev_deg": None if res is None else round(res[2], 2),
                                 "rango_max_km": None if res is None else round(float(res[0]["range"].values[-1]) / 1000, 1)}
                except Exception as e:
                    tareas[c] = {"n": len(ks), "muestra": k, "cubre": False, "error": f"{type(e).__name__}: {e}"[:200]}
                finally:
                    cleanup(p)
            ok = [c for c, v in tareas.items() if v["cubre"]]
            if ok:
                best = min(ok, key=lambda c: (tareas[c]["elev_deg"], -tareas[c]["rango_max_km"], -tareas[c]["n"]))
                tareas[best]["elegida"] = True
                items = by_cls[best]

        # 2) Proceso por lotes (acota el disco usado a ~12 volúmenes a la vez)
        scans = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for i0 in range(0, len(items), 12):
                for k, p in ex.map(fetch, items[i0:i0 + 12]):
                    t = key_time_utc(k)
                    try:
                        res = open_lowest_sweep(p, need_range(p))
                        if res is None:
                            skipped.append({"key": k, "motivo": "ningún barrido cubre la ventana"})
                            continue
                        ds, field, elev, rlat, rlon, ralt = res
                        if (rlat, rlon) != radar_geom:
                            grid_az, grid_ground = geo.geodesic(rlat, rlon, LAT, LON)
                            radar_geom = (rlat, rlon)
                        dbz = polar_to_grid(ds, field, elev, grid_az, grid_ground)
                        scans.append((t, k, dbz, field, elev, rlat, rlon, ralt))
                    except Exception as e:  # archivo corrupto, formato inesperado
                        skipped.append({"key": k, "motivo": f"{type(e).__name__}: {e}"[:200]})
                    finally:
                        cleanup(p)

        scans.sort(key=lambda s: s[0])
        times = [s[0] for s in scans]
        gaps = [(times[i + 1] - times[i]).total_seconds() / 60 for i in range(len(times) - 1)]
        med = float(np.median(gaps)) if gaps else 5.0
        for i, (t, k, dbz, field, elev, rlat, rlon, ralt) in enumerate(scans):
            dt_min = min(gaps[i] if i < len(gaps) else med, C.RADAR_MAX_DT_MIN)
            rate = dbz_to_rate_mmh(dbz)
            acc += rate * dt_min / 60.0
            used_keys.append(k)
            v = dbz[ic, jc]
            series.append({
                "t_utc": t.isoformat(),
                "dbz_belen": None if not np.isfinite(v) else round(float(v), 1),
                "r_belen_mmh": round(float(rate[ic, jc]), 2),
                "r_max5km_mmh": round(float(np.max(rate[near5])), 2),
                "dt_min": round(dt_min, 2),
            })
            slot = int((t.hour * 60 + t.minute) // C.RADAR_FRAME_STEP_MIN)
            if slot != last_frame_slot:
                last_frame_slot = slot
                name = f"{t:%H%M}.png"
                save_png(_classify_rgba(dbz, DBZ_LEVELS, DBZ_COLORS, inside), os.path.join(day_dir, "frames", name))
                frames.append({"t_utc": t.isoformat(), "png": f"radar/{day_local:%Y%m%d}/frames/{name}"})

        save_png(_classify_rgba(acc, ACC_LEVELS, ACC_COLORS, inside, alpha=170), os.path.join(day_dir, "acumulado.png"))

        meta = {"dia_local": day_local.isoformat(), "zona_horaria": "UTC-05:00", "completo": bool(complete)}
        if scans:
            _, _, _, field, elev, rlat, rlon, ralt = scans[0]
            dist = float(geo.geodesic(rlat, rlon, C.SITE_LAT_DEG, C.SITE_LON_DEG)[1])
            r_sl = float(geo.slant_from_ground_m(dist, elev))
            h_beam = float(geo.beam_height_m(r_sl, elev)) + ralt
            elevs = sorted({round(s[4], 2) for s in scans})
            meta.update({
                "radar": {"sitio": C.RADAR_SITE, "lat": rlat, "lon": rlon, "alt_m": ralt,
                          "campo": field, "elevaciones_usadas_deg": elevs},
                "geometria_belen": {
                    "distancia_km": round(dist / 1000, 1),
                    "elevacion_deg": elevs[0],
                    "altura_centro_haz_msnm": round(h_beam),
                    "altura_sitio_msnm": C.SITE_ALT_M,
                    "margen_sobre_sitio_m": round(h_beam - C.SITE_ALT_M),
                    "modelo": "Tierra efectiva 4/3 (Doviak & Zrnić 1993, ec. 2.28b)",
                },
            })
        meta.update({
            "volumenes_listados": len(keys) if keys is not None else len(items),
            "volumenes_tarea_elegida": len(items), "volumenes_usados": len(scans), "omitidos": skipped,
            "tareas_por_minuto_mod5": tareas,
            "brecha_max_min": round(max(gaps), 1) if gaps else None,
            "zr": {"a": C.ZR_A, "b": C.ZR_B, "ref": "Marshall & Palmer (1948)",
                   "umbral_dbz": C.RADAR_MIN_DBZ, "tope_granizo_dbz": C.RADAR_HAIL_CAP_DBZ,
                   "dt_max_min": C.RADAR_MAX_DT_MIN},
            "acumulado_belen_mm": round(float(acc[ic, jc]), 1),
            "acumulado_max_ventana_mm": round(float(np.max(acc[inside])), 1),
            "bounds": bounds, "acumulado_png": f"radar/{day_local:%Y%m%d}/acumulado.png",
            "frames": frames, "serie_belen": series,
            "fuente": {"nombre": "IDEAM – Red de radares (AWS Open Data)", "licencia": "CC BY 4.0",
                       "url": "https://registry.opendata.aws/ideam-radares/",
                       "bucket": f"s3://{C.RADAR_BUCKET}", "archivos": used_keys},
            "procesado_utc": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        })
        with open(os.path.join(day_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_index(out_root):
    rdir = os.path.join(out_root, "radar")
    days = sorted(d for d in os.listdir(rdir) if d.isdigit()) if os.path.isdir(rdir) else []
    for d in days[:-C.RADAR_KEEP_DAYS]:
        shutil.rmtree(os.path.join(rdir, d), ignore_errors=True)
    days = days[-C.RADAR_KEEP_DAYS:]
    idx = []
    for d in days:
        try:
            with open(os.path.join(rdir, d, "meta.json"), encoding="utf-8") as f:
                m = json.load(f)
            idx.append({"dia": d, "completo": m.get("completo"), "volumenes": m.get("volumenes_usados"),
                        "acumulado_belen_mm": m.get("acumulado_belen_mm")})
        except FileNotFoundError:
            pass
    with open(os.path.join(rdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"dias": idx}, f, ensure_ascii=False)
    return idx


def run(out_root, today_local=None):
    today_local = today_local or dt.datetime.now(COL).date()
    s3 = _s3()
    day, keys, complete = find_latest_local_day(s3, today_local)
    if day is None:
        update_index(out_root)
        return {"estado": "sin_datos", "buscado_hasta_dias": C.RADAR_MAX_LOOKBACK_DAYS}
    meta_path = os.path.join(out_root, "radar", f"{day:%Y%m%d}", "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("completo") and old.get("volumenes_listados") == len(keys):
            update_index(out_root)
            return {"estado": "sin_cambios", "dia": day.isoformat()}
    meta = process_local_day(day, keys, complete, out_root, s3=s3)
    update_index(out_root)
    return {"estado": "procesado", "dia": day.isoformat(), "volumenes": meta["volumenes_usados"]}
