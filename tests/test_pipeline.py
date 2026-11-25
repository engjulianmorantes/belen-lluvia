"""Pruebas sin red. Ejecutar:  cd pipeline && python -m pytest ../tests -q"""
import datetime as dt
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import config as C  # noqa: E402
import geo  # noqa: E402
import radar  # noqa: E402
import stations  # noqa: E402

P = "l2_data/2026/11/24/Barrancabermeja/"
# Nombres reales listados en s3://s3-radaresideam el 2026-11-25 (pares duplicados .RAW7/.RAWZ)
REAL = ["BAR261124180004.RAW7HT7", "BAR261124180004.RAWZU7V", "BAR261124180108.RAW7HTC",
        "BAR261124180108.RAWZU7X", "BAR261124180239.RAW7HTE", "BAR261124180239.RAWZU7Z",
        "BAR261124180317.RAW7LTW", "BAR261124180317.RAWZU81", "BAR261124180504.RAW7HTH",
        "BAR261124180504.RAWZU83"]


def test_dedupe_pairs_same_size():
    objs = [{"Key": P + k, "Size": 100} for k in REAL]
    out = radar.dedupe(objs)
    assert len(out) == 5


def test_dedupe_keeps_same_time_different_size():
    # Carimagua 15-ene-2026: dos volúmenes DISTINTOS con el mismo sello (1038336 y 3618816 bytes)
    pc = "l2_data/2026/01/15/Carimagua/"
    objs = [{"Key": pc + "CAR260115000123.RAWNBJJ", "Size": 1038336},
            {"Key": pc + "CAR260115000123.RAWV59B", "Size": 3618816}]
    assert len(radar.dedupe(objs)) == 2


def test_cycle_offset_and_slots():
    t = radar.key_time_utc(P + "BAR261124180239.RAW7HTE")
    assert radar.cycle_offset_s(t) == 159
    assert radar.slot_name(t, 10) == "1800" and radar.slot_name(t, 30) == "1800"


def test_local_day_from_two_folders():
    class FakeS3:
        def get_paginator(self, _):
            return self

        def paginate(self, Bucket, Prefix):
            day = Prefix.split("/")[3]
            names = {"24": ["BAR261124045739.RAW1", "BAR261124050004.RAW2", "BAR261124050004.RAWZ",
                            "BAR261125045500.RAW3", "BAR261125050004.RAW4"],
                     "25": ["BAR261125050004.RAW4", "BAR261125120000.RAW5"]}.get(day, [])
            return [{"Contents": [{"Key": Prefix + n, "Size": 10} for n in names]}]

    objs, complete = radar.objs_for_local_day(FakeS3(), "Barrancabermeja", dt.date(2026, 11, 24))
    names = [os.path.basename(o["Key"])[:15] for o in objs]
    assert names == ["BAR261124050004", "BAR261125045500"]
    assert complete


def test_polar_to_grid_wraparound():
    import xarray as xr
    az = np.array([359.5, 0.5, 1.5, 180.0])
    rng = np.arange(125.0, 50000.0, 250.0)
    data = np.zeros((4, rng.size)); data[0] = 10; data[1] = 20; data[2] = 30; data[3] = 40
    ds = xr.Dataset({"DBZH": (("azimuth", "range"), data)}, coords={"azimuth": az, "range": rng})
    g_az = np.array([[359.9, 0.1, 1.4, 180.2, 90.0]])
    g_gr = np.full(g_az.shape, 10000.0)
    out, cov = radar.polar_to_grid(ds, "DBZH", 0.5, g_az, g_gr)
    assert out[0, :4].tolist() == [10, 20, 30, 40]
    assert not cov[0, 4]  # 90° está a >1° de cualquier rayo


def test_enc_dec_dbz():
    v = np.array([np.nan, -10.0, 23.5, 60.0])
    d = radar.dec_dbz(radar.enc_dbz(v))
    assert np.isnan(d[0]) and np.allclose(d[1:], v[1:])


def test_zr_marshall_palmer():
    assert radar.dbz_to_rate_mmh(30.0) == pytest.approx((1000 / 200) ** (1 / 1.6))
    assert radar.dbz_to_rate_mmh(5.0) == 0.0
    assert radar.dbz_to_rate_mmh(70.0) == pytest.approx(radar.dbz_to_rate_mmh(53.0))


def test_beam_height_reference():
    # 4/3·R, 152 km, 0.5° → ~2.59 km sobre la antena (cálculo independiente: r·sinθ + r²/(2·kₑa))
    r, th = 152e3, np.radians(0.5)
    approx = r * np.sin(th) + r**2 / (2 * 4 / 3 * 6.371e6)
    assert geo.beam_height_m(r, 0.5) == pytest.approx(approx, rel=2e-3)
    s = geo.ground_range_m(r, 0.5)
    assert geo.slant_from_ground_m(s, 0.5) == pytest.approx(r, abs=10)


def test_station_summary_duplicate_sensor_and_days():
    TZ = stations.TZ
    now = dt.datetime(2026, 11, 25, 9, 0, tzinfo=TZ)
    rows, t = [], dt.datetime(2026, 11, 22, 0, 10, tzinfo=TZ)
    while t <= dt.datetime(2026, 11, 24, 0, 0, tzinfo=TZ):  # datos hasta 23-nov 24:00 (retraso real)
        v = 1.0 if t.hour == 15 and t.day == 23 else 0.0
        rows.append({"codigosensor": "0240", "fechaobservacion": t.strftime("%Y-%m-%dT%H:%M:%S.000"), "valorobservado": str(v)})
        if t.day == 23:
            rows.append({"codigosensor": "0257", "fechaobservacion": t.strftime("%Y-%m-%dT%H:%M:%S.000"), "valorobservado": str(v)})
        t += dt.timedelta(minutes=10)
    st = {"codigoestacion": "X", "lat": 6.0, "lon": -72.9, "dist_km": 1.0}
    r = stations.summarize(st, rows, now)
    assert r["sensor_usado"] == "0240"
    assert r["acum_hoy_mm"] is None              # no hay datos de hoy
    assert r["dia_ultimo_dato"] == "2026-11-23"  # la lectura de las 00:00 del 24 cierra el 23
    assert r["acum_dia_ultimo_dato_mm"] == 6.0   # 15:00–15:50 → 6 lecturas de 1 mm
    assert r["dia_ultimo_dato_completo"]


def test_deptos_upper():
    assert "'BOYACÁ'" in stations._deptos_sql()
