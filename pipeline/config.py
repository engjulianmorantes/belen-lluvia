"""Configuración central. Todo valor físico lleva unidad en el nombre o en el comentario."""
import os

# --- Punto de interés -------------------------------------------------------
# Belén (Boyacá), casco urbano. Coordenadas aproximadas del parque principal:
# VERIFICAR contra IGAC/Google antes de publicar si se requiere precisión < 500 m.
SITE_NAME = "Belén, Boyacá"
SITE_LAT_DEG = 5.9894
SITE_LON_DEG = -72.9117
SITE_ALT_M = 2750.0  # m s.n.m. (aprox.)

# Radio de la ventana de análisis alrededor de Belén
AOI_RADIUS_KM = 40.0
# Malla de salida (lat/lon regular). 0.005° ≈ 0.55 km en latitud.
GRID_STEP_DEG = 0.005

# --- Radar IDEAM (AWS Open Data) ---------------------------------------------
RADAR_BUCKET = "s3-radaresideam"
RADAR_PREFIX = "l2_data"
# Radares a procesar (nombre = carpeta en el bucket). lat/lon solo se usan hasta que el pipeline
# lee la posición real del archivo IRIS (queda en data/radar/estado_radares.json).
RADARS = {
    "Barrancabermeja": {"lat": 6.93276, "lon": -73.76251, "color": "#e4572e",
                        "coord_fuente": "archivo IRIS (25-nov-2026)"},
    "Carimagua": {"lat": 4.57, "lon": -71.33, "color": "#17a398",
                  "coord_fuente": "APROXIMADA, sin verificar (se reemplaza al leer un archivo)"},
    "Guaviare": {"lat": 2.53375, "lon": -72.62847, "color": "#7b5ea7",
                 "coord_fuente": "archivo IRIS vía documentación xradar"},
}
RADAR_NOMINAL_RANGE_KM = 300.0      # alcance nominal, guía IDEAM
# Malla regional que contiene los 300 km de los tres radares (S, W), (N, E) en grados
REGION_BOUNDS = ((-0.25, -76.50), (9.75, -68.55))
REGION_STEP_DEG = 0.025             # ≈ 2.8 km
REGION_FRAME_STEP_MIN = 30          # un cuadro regional cada 30 min (peso de publicación)
RADAR_KEEP_EXTRA_DAYS = []          # días históricos fijos (AAAAMMDD) que no se borran
RADAR_FECHA = os.environ.get("RADAR_FECHA", "").strip()  # AAAA-MM-DD: procesar ese día local
RADAR_MAX_LOOKBACK_DAYS = 4         # hasta cuántos días atrás buscar datos
RADAR_FRAME_STEP_MIN = 10           # 1 PNG cada N min (la acumulación usa TODOS los volúmenes)
RADAR_MAX_DT_MIN = 10.0             # tope del intervalo de integración entre volúmenes, min
RADAR_MIN_DBZ = 10.0                # umbral de lluvia, dBZ
RADAR_HAIL_CAP_DBZ = 53.0           # tope para no convertir granizo en lluvia, dBZ
# Relación Z-R de Marshall & Palmer (1948): Z = a·R^b, Z en mm^6/m^3, R en mm/h
ZR_A = 200.0
ZR_B = 1.6
RADAR_KEEP_DAYS = 7                 # días de radar que se conservan publicados

# --- Estaciones IDEAM (datos.gov.co, Socrata) --------------------------------
STATIONS_DATASET = "s54a-sgyg"      # "Precipitación" – IDEAM, cada 10 min, CC BY-SA 4.0
STATIONS_DEPTOS = ["BOYACÁ", "SANTANDER", "CASANARE"]
STATIONS_RADIUS_KM = 40.0
STATIONS_LOOKBACK_DAYS = 10   # consultas agregadas más largas tienden a dar 5xx en Socrata
# El conjunto no declara zona horaria; se asume hora legal colombiana.
STATIONS_TZ_OFFSET_H = -5

# --- GPM IMERG Early (NASA GES DISC) ------------------------------------------
# Requiere token Earthdata (gratuito) en la variable de entorno EARTHDATA_TOKEN.
IMERG_COLLECTION = "GPM_3IMERGHHE.07"
IMERG_LOOKBACK_H = 24
EARTHDATA_TOKEN = os.environ.get("EARTHDATA_TOKEN", "").strip()

# --- Pronóstico (Open-Meteo) --------------------------------------------------
FORECAST_DAYS = 3
FORECAST_PAST_DAYS = 1

# --- Salida -------------------------------------------------------------------
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(os.path.dirname(__file__), "..", "site", "data"))
USER_AGENT = "belen-lluvia/1.0 (+https://github.com/)"
