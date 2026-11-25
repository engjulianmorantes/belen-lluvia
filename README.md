# Lluvia · Belén, Boyacá

App web instalable en Android (PWA), en la línea del visor de lluvias del SAB/IDIGER, para Belén (Boyacá).
Un proceso de GitHub Actions descarga y procesa los datos y los publica como archivos estáticos en GitHub Pages.

## Fuentes (todas trazables)

| Fuente | Qué aporta | Latencia real | Licencia |
|---|---|---|---|
| IDEAM – radar Barrancabermeja, `s3://s3-radaresideam` (AWS Open Data) | Reflectividad cada 5 min, acumulado diario estimado | El día se sube en lote, de noche: entre ~1 h y ~1 día | CC BY 4.0 |
| IDEAM – "Precipitación" estaciones automáticas, datos.gov.co `s54a-sgyg` | Pluviómetros cada 10 min en 40 km (Socha, Tibasosa, Sogamoso, Paipa…; no hay estación en Belén) | Carga diaria: datos hasta el día anterior (~1–1,5 días) | CC BY-SA 4.0 |
| NASA GPM IMERG Early V07 (`GPM_3IMERGHHE.07`) | Lluvia satelital cada 30 min, celda 0,1° | ~4 h | Datos NASA, uso libre con cita |
| Open-Meteo (best_match) | Pronóstico horario de 48 h a 72 h | Modelo, no es observación | CC BY 4.0 |

Cada JSON publicado guarda la URL exacta de la consulta, la hora de consulta y, en el radar, la lista de archivos IRIS usados (`data/radar/AAAAMMDD/meta.json`).

## Metodología del radar

1. **Día local** (UTC−5): se toman los volúmenes cuyo sello (UTC, en el nombre del archivo) cae dentro de [00:00, 24:00) hora local. El bucket organiza las carpetas por día local; por robustez se listan D y D+1.
2. **Duplicados**: cada volumen se publica dos veces, con extensiones `.RAW7xxx` y `.RAWZxxx`, mismo tamaño y mismo ETag. Se conserva uno por sello de tiempo.
3. **Tarea**: el radar corre 4 tareas por ciclo de 5 min (a los :x0, :x1, :x2 y :x3 aprox.; observado en nov-2026). Se prueba un volumen de cada una y se usa **solo** la que cubre toda la ventana con la menor elevación. Mezclar tareas daría elevaciones distintas entre cuadros. El resultado queda en `meta.json → tareas_por_minuto_mod5`.
4. **Barrido**: dentro de cada volumen se usa el barrido de menor elevación que cubre los 40 km alrededor de Belén. Campo `DBZH`.
5. **Remuestreo**: cada celda de la malla (0,005°) toma la compuerta más cercana en azimut (máximo 1°) y en rango oblicuo. La distancia sobre la superficie se convierte a rango oblicuo con el modelo de Tierra efectiva 4/3 (Doviak & Zrnić, 1993, ec. 2.28b y 2.28c). Se verificó con un archivo IRIS real: 200 de 200 compuertas recuperadas exactamente.
6. **Z–R** de Marshall & Palmer (1948): Z = 200·R^1,6, con R en mm/h y Z en mm⁶/m³. Por debajo de 10 dBZ se toma R = 0. Por encima de 53 dBZ se topa el valor para no convertir granizo en lluvia.
7. **Acumulado**: Σ R·Δt, con Δt = min(t₍ᵢ₊₁₎ − tᵢ, 10 min).

### Limitaciones que hay que conocer
- **Geometría**: Belén está a unos 152 km del radar. A 0,5° de elevación, el centro del haz pasa a unos 2 700 m a 2 800 m s.n.m. y el pueblo está a unos 2 750 m. El haz cruza la Cordillera Oriental antes de llegar, así que hay bloqueo parcial. La app calcula y muestra ese margen con las coordenadas reales que trae cada archivo.
- No se corrige bloqueo, atenuación (banda C) ni banda brillante, y no hay ajuste con pluviómetros. **Los mm del radar son indicativos.**
- El radar del IDEAM en AWS **no es tiempo real**. Para "lo que está pasando ahora" mira las estaciones (si tienen datos frescos), IMERG y el pronóstico.

## Puesta en marcha (una vez)

1. Crea un repositorio en GitHub, por ejemplo `belen-lluvia`, y sube esta carpeta a la rama `main`.
2. **Settings → Actions → General → Workflow permissions**: elige *Read and write*.
3. Opcional, para el satélite: crea un token en https://urs.earthdata.nasa.gov (Generate Token). Guárdalo en **Settings → Secrets and variables → Actions** como `EARTHDATA_TOKEN`. En la misma cuenta, autoriza la aplicación "NASA GESDISC DATA ARCHIVE" (Applications → Authorized Apps).
4. **Actions → "Actualizar datos y publicar" → Run workflow**, con radar activado. La primera vez tarda entre 10 y 20 min porque descarga los volúmenes de un día completo.
5. **Settings → Pages**: Source = *Deploy from a branch*, rama `gh-pages`, carpeta `/ (root)`.
6. Abre `https://<usuario>.github.io/belen-lluvia/` en Chrome Android → menú ⋮ → **Instalar app**.

Programación: estaciones, IMERG y pronóstico cada hora (minuto 17); radar cada 6 h.

## Ejecutar en local
```bash
cd pipeline
pip install -r requirements.txt
OUT_DIR=../site/data python build.py            # o --sin-radar
cd ../site && python -m http.server 8000         # http://localhost:8000
```

## Pruebas
```bash
cd pipeline && python -m pytest ../tests -q
```
Las pruebas cubren: duplicados con nombres reales del bucket, clases de tarea, corte del día local, Z–R, altura del haz contra la aproximación r·sinθ + r²/(2kₑa), y el resumen de estaciones (sensor duplicado, día del último dato).

Nota: en datos.gov.co el campo `departamento` cambió de "BOYACÁ" a "Boyacá" en nov-2026. La consulta compara con `upper()`.

## Ajustes
Todo está en `pipeline/config.py`: coordenadas y altura de Belén (verifícalas), radio, malla, parámetros Z–R, departamentos de las estaciones, días de radar que se conservan.

## Referencias
- IDEAM, *Guía de descarga y visualización de datos de radar meteorológico*. https://www.ideam.gov.co/nuestra-entidad/servicio-de-pronosticos-y-alertas/guia-de-descarga-y-visualizacion-de-datos-de-radar-meteorologico-de-ideam
- AWS Open Data Registry, *IDEAM – Colombian Radar Network*. https://registry.opendata.aws/ideam-radares/
- Doviak, R. J. & Zrnić, D. S. (1993). *Doppler Radar and Weather Observations*, 2.ª ed. Academic Press.
- Marshall, J. S. & Palmer, W. McK. (1948). The distribution of raindrops with size. *J. Meteor.*, 5, 165–166.
- xradar (lector IRIS): https://docs.openradarscience.org/projects/xradar/
- Huffman, G. J. et al., *GPM IMERG Early Precipitation L3 Half Hourly 0.1° V07*, NASA GES DISC.
- Open-Meteo Forecast API: https://open-meteo.com/en/docs
- Visor de referencia SAB/IDIGER (clases de color del acumulado): https://app.sab.gov.co/sab/lluvias.htm
