"""Geometría del haz de radar y de la malla de salida.

Propagación del haz con el modelo de Tierra efectiva 4/3 (Doviak & Zrnić,
"Doppler Radar and Weather Observations", 2.ª ed., 1993, ec. 2.28b y 2.28c):

    h(r, θ) = sqrt(r² + (kₑa)² + 2·r·kₑa·sin θ) − kₑa          [m sobre la antena]
    s(r, θ) = kₑa · asin( r·cos θ / (kₑa + h) )                  [m sobre la superficie]

r: distancia oblicua (slant range) [m], θ: elevación [rad], a: radio terrestre [m], kₑ = 4/3.
"""
import math
import numpy as np
from pyproj import Geod

EARTH_RADIUS_M = 6_371_000.0
KE = 4.0 / 3.0
_GEOD = Geod(ellps="WGS84")


def beam_height_m(slant_range_m, elev_deg):
    """Altura del centro del haz sobre la antena, m."""
    r = np.asarray(slant_range_m, dtype=float)
    kea = KE * EARTH_RADIUS_M
    th = math.radians(elev_deg)
    return np.sqrt(r**2 + kea**2 + 2.0 * r * kea * math.sin(th)) - kea


def ground_range_m(slant_range_m, elev_deg):
    """Distancia sobre la superficie terrestre, m."""
    r = np.asarray(slant_range_m, dtype=float)
    kea = KE * EARTH_RADIUS_M
    h = beam_height_m(r, elev_deg)
    return kea * np.arcsin(r * math.cos(math.radians(elev_deg)) / (kea + h))


def slant_from_ground_m(ground_m, elev_deg, r_max_m=400_000.0):
    """Inversa numérica de ground_range_m (monótona en el rango de interés)."""
    r_tab = np.linspace(0.0, r_max_m, 40_001)  # paso 10 m
    s_tab = ground_range_m(r_tab, elev_deg)
    return np.interp(ground_m, s_tab, r_tab, left=np.nan, right=np.nan)


def geodesic(lat1, lon1, lat2, lon2):
    """Azimut (°, desde el norte, horario) y distancia geodésica (m) de 1 → 2. Vectorizado."""
    lat1b, lon1b = np.broadcast_arrays(np.asarray(lat1, float), np.asarray(lon1, float))
    lat2b, lon2b = np.broadcast_arrays(np.asarray(lat2, float), np.asarray(lon2, float))
    lat1b, lat2b = np.broadcast_arrays(lat1b, lat2b)
    lon1b, lon2b = np.broadcast_arrays(lon1b, lon2b)
    az, _, dist = _GEOD.inv(lon1b, lat1b, lon2b, lat2b)
    return np.mod(az, 360.0), dist


def make_grid(lat0, lon0, radius_km, step_deg):
    """Malla regular norte-arriba. Devuelve (lats[ny], lons[nx], bounds[[S,W],[N,E]], mask_dentro_del_radio)."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat0)))
    n_lat = int(math.ceil(dlat / step_deg))
    n_lon = int(math.ceil(dlon / step_deg))
    # centros de celda, fila 0 = norte (orden de imagen)
    lats = lat0 + step_deg * np.arange(n_lat, -n_lat - 1, -1)
    lons = lon0 + step_deg * np.arange(-n_lon, n_lon + 1)
    half = step_deg / 2.0
    bounds = [[float(lats[-1] - half), float(lons[0] - half)],
              [float(lats[0] + half), float(lons[-1] + half)]]
    LAT, LON = np.meshgrid(lats, lons, indexing="ij")
    _, d = geodesic(lat0, lon0, LAT, LON)
    inside = d <= radius_km * 1000.0
    return lats, lons, bounds, inside
