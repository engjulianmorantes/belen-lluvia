"""GPM IMERG Early Run, media hora, 0.1° (NASA GES DISC). Latencia típica ~4 h.

Requiere EARTHDATA_TOKEN (https://urs.earthdata.nasa.gov → Generate Token).
Variable 'Grid/precipitation' [mm/h], dimensiones (time, lon=3600, lat=1800),
centros lon = −179.95 + 0.1·i, lat = −89.95 + 0.1·j.
Lámina por archivo = tasa · 0.5 h. Se guarda un estado para no volver a descargar.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile

import numpy as np
import requests

import config as C

BASE = f"https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/{C.IMERG_COLLECTION}"
FILE_RE = re.compile(r'href="(3B-HHR-E\.MS\.MRG\.3IMERG\.(\d{8})-S(\d{6})-E\d{6}\.\d{4}\.V\w+\.RT-H5)"')


def _session():
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {C.EARTHDATA_TOKEN}", "User-Agent": C.USER_AGENT})
    return s


def _list_day(s, day):
    url = f"{BASE}/{day:%Y}/{day.timetuple().tm_yday:03d}/"
    r = s.get(url, timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = {}
    for name, ymd, hms in FILE_RE.findall(r.text):
        t = dt.datetime.strptime(ymd + hms, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        out[t] = url + name
    return sorted(out.items())


def _extract(path):
    import h5py
    with h5py.File(path, "r") as f:
        g = f["Grid"]
        var = g["precipitation"] if "precipitation" in g else g["precipitationCal"]
        i = int(np.floor((C.SITE_LON_DEG + 180.0) / 0.1))
        j = int(np.floor((C.SITE_LAT_DEG + 90.0) / 0.1))
        win = np.array(var[0, i - 1:i + 2, j - 1:j + 2], dtype=float)
        fill = var.attrs.get("_FillValue", -9999.9)
        win[win == fill] = np.nan
        win[win < 0] = np.nan
        return float(win[1, 1]), float(np.nanmean(win)) if np.isfinite(win).any() else None


def run(out_root):
    state_path = os.path.join(out_root, "imerg_estado.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    base = {"fuente": {"nombre": "NASA GPM IMERG Early Run V07 (media hora, 0.1°)",
                       "url": "https://disc.gsfc.nasa.gov/datasets/GPM_3IMERGHHE_07/summary",
                       "licencia": "Datos NASA, uso libre con cita",
                       "nota": "Satelital, celda ~11 km; subestima lluvia orográfica. Indicativo."}}
    if not C.EARTHDATA_TOKEN:
        return {**base, "estado": "desactivado (falta EARTHDATA_TOKEN)"}
    s = _session()
    now = dt.datetime.now(dt.timezone.utc)
    t0 = now - dt.timedelta(hours=C.IMERG_LOOKBACK_H + 6)
    files = []
    for d in sorted({(t0 + dt.timedelta(days=k)).date() for k in range(0, 3)}):
        if d <= now.date():
            files += _list_day(s, d)
    files = [(t, u) for t, u in files if t >= t0]
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for t, url in files:
            key = t.isoformat()
            if key in state:
                continue
            p = os.path.join(tmp, "f.h5")
            try:
                r = s.get(url, timeout=120)
                r.raise_for_status()
                with open(p, "wb") as fh:
                    fh.write(r.content)
                cell, mean3 = _extract(p)
                state[key] = {"mmh_celda": cell, "mmh_media3x3": mean3, "archivo": url.rsplit("/", 1)[-1]}
            except Exception as e:
                errors.append(f"{key}: {type(e).__name__}: {e}"[:200])
    state = {k: v for k, v in state.items() if dt.datetime.fromisoformat(k) >= now - dt.timedelta(hours=72)}
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    serie = [{"t_utc": k, **v, "mm_30min": None if v["mmh_celda"] is None else round(v["mmh_celda"] * 0.5, 2)}
             for k, v in sorted(state.items())]
    last24 = [x for x in serie if dt.datetime.fromisoformat(x["t_utc"]) > now - dt.timedelta(hours=24)]
    return {**base, "estado": "ok" if serie else "sin_archivos", "errores": errors,
            "consultado_utc": now.isoformat(timespec="seconds"),
            "ultimo_t_utc": serie[-1]["t_utc"] if serie else None,
            "acum_24h_mm": round(sum(x["mm_30min"] or 0 for x in last24), 1),
            "n_medias_horas_24h": len(last24), "serie": serie[-96:]}
