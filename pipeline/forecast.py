"""Pronóstico horario Open-Meteo (modelo 'best_match'), CC BY 4.0. Es MODELO, no observación."""
import datetime as dt

import requests

import config as C

URL = "https://api.open-meteo.com/v1/forecast"


def run():
    params = {
        "latitude": C.SITE_LAT_DEG, "longitude": C.SITE_LON_DEG, "elevation": C.SITE_ALT_M,
        "hourly": "precipitation,precipitation_probability,temperature_2m,weather_code",
        "daily": "precipitation_sum,precipitation_probability_max",
        "timezone": "America/Bogota", "forecast_days": C.FORECAST_DAYS, "past_days": C.FORECAST_PAST_DAYS,
    }
    r = requests.get(URL, params=params, timeout=60, headers={"User-Agent": C.USER_AGENT})
    r.raise_for_status()
    j = r.json()
    h = j["hourly"]
    return {
        "fuente": {"nombre": "Open-Meteo Forecast API (best_match)", "licencia": "CC BY 4.0",
                   "url": r.url, "atribucion": "Weather data by Open-Meteo.com"},
        "consultado_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "elevacion_modelo_m": j.get("elevation"),
        "unidades": j.get("hourly_units"),
        "horario": [{"t": t, "mm": p, "prob": q, "temp": tt, "wmo": w}
                    for t, p, q, tt, w in zip(h["time"], h["precipitation"], h["precipitation_probability"],
                                              h["temperature_2m"], h["weather_code"])],
        "diario": [{"d": d, "mm": p, "prob": q} for d, p, q in
                   zip(j["daily"]["time"], j["daily"]["precipitation_sum"], j["daily"]["precipitation_probability_max"])],
    }
