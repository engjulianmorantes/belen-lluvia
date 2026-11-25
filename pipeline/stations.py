"""Pluviómetros automáticos IDEAM vía datos.gov.co (Socrata), conjunto s54a-sgyg "Precipitación".

valorobservado = lámina de precipitación en el intervalo de 10 min [mm].
Si una estación reporta con más de un sensor (p. ej. 0240 y 0257), se usa SOLO el
sensor con más registros en la ventana, para no duplicar lámina.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict

import requests

import config as C
import geo

BASE = f"https://www.datos.gov.co/resource/{C.STATIONS_DATASET}.json"
TZ = dt.timezone(dt.timedelta(hours=C.STATIONS_TZ_OFFSET_H))


def _get(params, timeout=120, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(BASE, params=params, timeout=timeout, headers={"User-Agent": C.USER_AGENT})
            r.raise_for_status()
            return r.json(), r.url
        except requests.RequestException as e:  # Socrata devuelve 5xx ocasionales en consultas agregadas
            last = e
            time.sleep(5 * (i + 1))
    raise last


def _deptos_sql():
    # El conjunto cambió de "BOYACÁ" (histórico) a "Boyacá" (nov-2026): se compara en mayúsculas.
    return ",".join("'" + d.upper().replace("'", "''") + "'" for d in C.STATIONS_DEPTOS)


def catalog(now):
    since = (now.astimezone(TZ) - dt.timedelta(days=C.STATIONS_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows, url = _get({
        "$select": "codigoestacion,nombreestacion,municipio,latitud,longitud,max(fechaobservacion) as ultima",
        "$where": f"fechaobservacion >= '{since}' AND upper(departamento) in({_deptos_sql()})",
        "$group": "codigoestacion,nombreestacion,municipio,latitud,longitud",
        "$limit": 5000,
    })
    out = []
    for r in rows:
        try:
            lat, lon = float(r["latitud"]), float(r["longitud"])
        except (KeyError, ValueError):
            continue
        d_km = float(geo.geodesic(C.SITE_LAT_DEG, C.SITE_LON_DEG, lat, lon)[1]) / 1000.0
        if d_km <= C.STATIONS_RADIUS_KM:
            out.append({**r, "lat": lat, "lon": lon, "dist_km": round(d_km, 1)})
    return out, url


def _parse_t(s):
    return dt.datetime.fromisoformat(s[:19]).replace(tzinfo=TZ)


def series(codes, since_local):
    codes_sql = ",".join("'" + c + "'" for c in codes)
    rows, out, offset, urls = [], [], 0, []
    while True:
        chunk, url = _get({
            "$select": "codigoestacion,codigosensor,fechaobservacion,valorobservado",
            "$where": f"codigoestacion in({codes_sql}) AND fechaobservacion >= '{since_local:%Y-%m-%dT%H:%M:%S}'",
            "$order": "fechaobservacion", "$limit": 50000, "$offset": offset,
        })
        urls.append(url)
        rows += chunk
        if len(chunk) < 50000:
            break
        offset += 50000
    return rows, urls[0] if urls else None


def summarize(st, rows, now):
    by_sensor = defaultdict(dict)
    for r in rows:
        try:
            by_sensor[r["codigosensor"]][_parse_t(r["fechaobservacion"])] = float(r["valorobservado"])
        except (KeyError, ValueError):
            continue
    if not by_sensor:
        return {**_base(st), "estado": "sin_datos_recientes"}
    sensor, obs = max(by_sensor.items(), key=lambda kv: len(kv[1]))
    ts = sorted(obs)
    last = ts[-1]
    neg = sum(1 for t in ts if obs[t] < 0)

    def acc(t0, t1):
        return round(sum(max(obs[t], 0.0) for t in ts if t0 < t <= t1), 1)

    today0 = dt.datetime.combine(now.astimezone(TZ).date(), dt.time(0), TZ)
    # Día local del último dato (los datos llegan con 1–1,5 días de retraso)
    lday0 = dt.datetime.combine((last - dt.timedelta(minutes=1)).date(), dt.time(0), TZ)
    hourly = defaultdict(float)
    for t in ts:
        if t > last - dt.timedelta(hours=48):
            hourly[(t - dt.timedelta(minutes=1)).replace(minute=0, second=0).isoformat()] += max(obs[t], 0.0)
    return {
        **_base(st),
        "estado": "ok",
        "sensor_usado": sensor,
        "sensores_disponibles": sorted(by_sensor),
        "ultima_obs_local": last.isoformat(),
        "edad_min": round((now - last).total_seconds() / 60.0),
        "ultimo_10min_mm": round(max(obs[last], 0.0), 1),
        "acum_1h_mm": acc(last - dt.timedelta(hours=1), last),
        "acum_24h_mm": acc(last - dt.timedelta(hours=24), last),
        "acum_hoy_mm": acc(today0, now) if last > today0 else None,
        "dia_ultimo_dato": lday0.date().isoformat(),
        "acum_dia_ultimo_dato_mm": acc(lday0, lday0 + dt.timedelta(days=1)),
        "dia_ultimo_dato_completo": last >= lday0 + dt.timedelta(hours=23, minutes=50),
        "n_registros_24h": sum(1 for t in ts if t > last - dt.timedelta(hours=24)),
        "registros_negativos_descartados": neg,
        "horario_48h": [{"h": k, "mm": round(v, 1)} for k, v in sorted(hourly.items())],
    }


def _base(st):
    return {"codigo": st["codigoestacion"], "nombre": st.get("nombreestacion"), "municipio": st.get("municipio"),
            "lat": st["lat"], "lon": st["lon"], "dist_km": st["dist_km"]}


def run():
    now = dt.datetime.now(dt.timezone.utc)
    cat, cat_url = catalog(now)
    result = {
        "fuente": {"nombre": "IDEAM – Precipitación estaciones automáticas (datos.gov.co)",
                   "conjunto": C.STATIONS_DATASET, "licencia": "CC BY-SA 4.0",
                   "url": f"https://www.datos.gov.co/d/{C.STATIONS_DATASET}", "consulta_catalogo": cat_url,
                   "nota": "Datos preliminares no validados por IDEAM. Zona horaria asumida UTC-5."},
        "consultado_utc": now.isoformat(timespec="seconds"),
        "radio_km": C.STATIONS_RADIUS_KM, "estaciones": [],
    }
    if not cat:
        result["estado"] = f"sin estaciones con datos en {C.STATIONS_LOOKBACK_DAYS} d dentro de {C.STATIONS_RADIUS_KM} km"
        return result
    newest = max(_parse_t(s["ultima"]) for s in cat)
    rows, url = series([s["codigoestacion"] for s in cat], newest - dt.timedelta(hours=48))
    result["fuente"]["consulta_series"] = url
    per = defaultdict(list)
    for r in rows:
        per[r["codigoestacion"]].append(r)
    result["estaciones"] = sorted((summarize(s, per[s["codigoestacion"]], now) for s in cat),
                                  key=lambda x: x["dist_km"])
    result["estado"] = "ok"
    return result
