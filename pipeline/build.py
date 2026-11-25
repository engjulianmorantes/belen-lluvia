"""Orquestador: ejecuta cada fuente de forma independiente y escribe JSON para la PWA.

Uso:  python build.py [--sin-radar]
Cada fuente que falle queda registrada en estado.json sin tumbar las demás.
"""
import datetime as dt
import json
import os
import sys
import traceback

import config as C


def _write(name, obj):
    os.makedirs(C.OUT_DIR, exist_ok=True)
    with open(os.path.join(C.OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _step(name, fn, estado):
    t0 = dt.datetime.now(dt.timezone.utc)
    try:
        res = fn()
        estado[name] = {"ok": True, "inicio_utc": t0.isoformat(timespec="seconds"),
                        "duracion_s": round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1)}
        return res
    except Exception as e:
        estado[name] = {"ok": False, "inicio_utc": t0.isoformat(timespec="seconds"),
                        "error": f"{type(e).__name__}: {e}"[:500]}
        traceback.print_exc()
        return None


def main():
    estado = {}
    sitio = {"nombre": C.SITE_NAME, "lat": C.SITE_LAT_DEG, "lon": C.SITE_LON_DEG, "alt_m": C.SITE_ALT_M,
             "radio_km": C.AOI_RADIUS_KM}
    import forecast, imerg, stations
    for name, fn, out in (("estaciones", stations.run, "estaciones.json"),
                          ("pronostico", forecast.run, "pronostico.json"),
                          ("imerg", lambda: imerg.run(C.OUT_DIR), "imerg.json")):
        res = _step(name, fn, estado)
        if res is not None:
            _write(out, res)
    if "--sin-radar" not in sys.argv:
        import radar
        fecha = dt.date.fromisoformat(C.RADAR_FECHA) if C.RADAR_FECHA else None
        res = _step("radar", lambda: radar.run(C.OUT_DIR, fecha=fecha), estado)
        if res is not None:
            estado["radar"]["resultado"] = res
    _write("estado.json", {"sitio": sitio, "generado_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "pasos": estado})
    print(json.dumps(estado, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
