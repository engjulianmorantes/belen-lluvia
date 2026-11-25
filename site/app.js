"use strict";
const TZ = "America/Bogota";
const fmtDT = new Intl.DateTimeFormat("es-CO", {timeZone: TZ, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const fmtH = new Intl.DateTimeFormat("es-CO", {timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const fmtD = new Intl.DateTimeFormat("es-CO", {timeZone: TZ, weekday: "short", day: "2-digit"});
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d).replace(".", ",");

// Clases de acumulado diario idénticas al visor SAB/IDIGER (mm)
const CLASSES = [
  {max: 0,        color: "#9aa4ad", label: "Sin lluvia (0,0)"},
  {max: 10,       color: "#3478f6", label: "Baja (0–10)"},
  {max: 30,       color: "#2eaa46", label: "Moderada (10,1–30)"},
  {max: 50,       color: "#f58c14", label: "Alta (30,1–50)"},
  {max: Infinity, color: "#8c3cbe", label: "Muy alta (>50)"},
];
const cls = (mm) => CLASSES.find(c => (mm ?? 0) <= c.max);
const DBZ = [[10,"rgb(4,233,231)"],[15,"rgb(1,159,244)"],[20,"rgb(3,0,244)"],[25,"rgb(2,253,2)"],[30,"rgb(1,197,1)"],
  [35,"rgb(0,142,0)"],[40,"rgb(253,248,2)"],[45,"rgb(229,188,0)"],[50,"rgb(253,149,0)"],[55,"rgb(253,0,0)"],[60,"rgb(212,0,0)"],[65,"rgb(188,0,0)"]];

async function getJSON(path) {
  try {
    const r = await fetch(`data/${path}?v=${Date.now()}`, {cache: "no-store"});
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}
const ago = (iso) => {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 60) return `hace ${m} min`;
  if (m < 48 * 60) return `hace ${Math.round(m / 60)} h`;
  return `hace ${Math.round(m / 1440)} d`;
};

// ------------------------------------------------------------------ mapa
let map, overlay, frames = [], accOverlay, meta, mode = "frames", idx = 0, timer = null;

function initMap(site) {
  map = L.map("map", {zoomControl: true}).setView([site.lat, site.lon], 10);
  const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom: 17, attribution: "© OpenStreetMap"});
  const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    {maxZoom: 15, attribution: "© OpenStreetMap, SRTM | © OpenTopoMap (CC-BY-SA)"});
  topo.addTo(map);
  L.control.layers({"Relieve": topo, "Calles": osm}, null, {position: "topright"}).addTo(map);
  L.circle([site.lat, site.lon], {radius: site.radio_km * 1000, color: "#0f2a3d", weight: 1, fill: false, dashArray: "4 4"}).addTo(map);
  L.circleMarker([site.lat, site.lon], {radius: 5, color: "#000", weight: 2, fillColor: "#fff", fillOpacity: 1})
    .bindTooltip(site.nombre, {permanent: true, direction: "right", offset: [6, 0]}).addTo(map);
}

function triangle(color, blink) {
  return L.divIcon({className: "", iconSize: [18, 16], iconAnchor: [9, 12],
    html: `<svg width="18" height="16" viewBox="0 0 18 16"${blink ? ' class="blink"' : ""}><path d="M9 1 17 15H1Z" fill="${color}" stroke="#000" stroke-width="1"/>${blink ? '<animate attributeName="opacity" values="1;.25;1" dur="1.2s" repeatCount="indefinite"/>' : ""}</svg>`});
}

function drawStationsOnMap(st) {
  if (!st?.estaciones) return;
  for (const e of st.estaciones) {
    // Color = acumulado del último día local con datos (los datos llegan con 1–1,5 d de retraso).
    // Sin datos: blanco, para no confundirlo con "sin lluvia" (gris).
    const mm = e.estado === "ok" ? (e.acum_hoy_mm ?? e.acum_dia_ultimo_dato_mm) : null;
    const c = mm === null || mm === undefined ? "#ffffff" : cls(mm).color;
    const raining = e.estado === "ok" && e.ultimo_10min_mm > 0 && e.edad_min <= 30;
    L.marker([e.lat, e.lon], {icon: triangle(c, raining)}).addTo(map).bindPopup(
      `<b>${esc(e.nombre)}</b><br>${esc(e.municipio)} · ${num(e.dist_km)} km<br>` +
      (e.estado === "ok"
        ? `${e.acum_hoy_mm != null ? "Hoy desde 0:00" : "Día " + esc(e.dia_ultimo_dato) + (e.dia_ultimo_dato_completo ? "" : " (parcial)")}: <b>${num(mm)} mm</b><br>Últimas 24 h: ${num(e.acum_24h_mm)} mm<br>Último dato: ${fmtDT.format(new Date(e.ultima_obs_local))} (${ago(e.ultima_obs_local)})`
        : "Sin datos recientes"));
  }
}

function setLegend() {
  const L_ = $("legend");
  if (mode === "acc") {
    L_.innerHTML = CLASSES.slice(1).map(c => `<span class="item"><i style="background:${c.color}"></i>${c.label}</span>`).join("");
  } else if (mode === "frames") {
    L_.innerHTML = DBZ.map(([v, c]) => `<span class="item"><i style="background:${c}"></i>${v}</span>`).join("") + `<span class="item" style="margin-left:4px">dBZ</span>`;
  } else L_.innerHTML = "";
}

function showFrame(i) {
  if (!frames.length) return;
  idx = (i + frames.length) % frames.length;
  overlay.setUrl(`data/${frames[idx].png}`);
  $("slider").value = idx;
  const s = meta.serie_belen?.find(x => x.t_utc === frames[idx].t_utc);
  $("frame-time").textContent = fmtDT.format(new Date(frames[idx].t_utc)) + (s?.dbz_belen != null ? ` · ${num(s.dbz_belen)} dBZ` : "");
}

function setMode(m) {
  mode = m;
  document.querySelectorAll(".seg button").forEach(b => b.classList.toggle("on", b.dataset.layer === m));
  if (overlay) overlay.setOpacity(m === "frames" ? 0.85 : 0);
  if (accOverlay) accOverlay.setOpacity(m === "acc" ? 0.8 : 0);
  $("player").style.display = m === "frames" ? "flex" : "none";
  if (m !== "frames") stop();
  setLegend();
}
function stop() { clearInterval(timer); timer = null; $("play").textContent = "▶"; }

async function loadRadar() {
  const index = await getJSON("radar/index.json");
  const day = index?.dias?.at(-1);
  if (!day) { $("radar-note").textContent = "Aún no hay días de radar procesados."; return null; }
  meta = await getJSON(`radar/${day.dia}/meta.json`);
  if (!meta) return null;
  frames = meta.frames || [];
  const b = meta.bounds;
  const blank = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
  overlay = L.imageOverlay(frames[0] ? `data/${frames[0].png}` : blank, b, {opacity: .85, interactive: false}).addTo(map);
  accOverlay = L.imageOverlay(`data/${meta.acumulado_png}`, b, {opacity: 0, interactive: false}).addTo(map);
  frames.forEach(f => { const im = new Image(); im.src = `data/${f.png}`; }); // precarga
  $("slider").max = Math.max(frames.length - 1, 0);
  showFrame(frames.length - 1);
  const d = new Date(meta.dia_local + "T12:00:00-05:00");
  $("radar-note").innerHTML = `Radar ${esc(meta.radar?.sitio ?? "")}: día <b>${fmtD.format(d)}</b> (hora local) · ${meta.volumenes_usados} volúmenes` +
    `${meta.completo ? "" : " · <b>día incompleto</b>"}. Los datos públicos del IDEAM en AWS llegan con ~1 día de retraso: esto NO es lluvia actual.`;
  return meta;
}

// ------------------------------------------------------------- tarjetas
function renderNow(st) {
  const e = st?.estaciones?.find(x => x.estado === "ok");
  if (!e) { $("now-body").innerHTML = `<span class="muted">${esc(st?.estado ?? "Sin datos de estaciones")}</span>`; return; }
  const today = e.acum_hoy_mm;
  const dayMM = today ?? e.acum_dia_ultimo_dato_mm;
  const dayLbl = today != null ? "Desde las 0:00 de hoy" : `Día ${fmtD.format(new Date(e.dia_ultimo_dato + "T12:00:00-05:00"))}${e.dia_ultimo_dato_completo ? "" : " (parcial)"}`;
  const c = cls(dayMM ?? 0);
  const fresh = e.edad_min <= 60;
  $("now-body").innerHTML = `
    <div><b>${esc(e.nombre)}</b> <span class="muted">· ${esc(e.municipio)} · ${num(e.dist_km)} km</span></div>
    <div class="row" style="margin-top:8px">
      <div class="kv"><span>${dayLbl}</span><span class="big">${dayMM == null ? "—" : num(dayMM) + " mm"}</span></div>
      <div class="kv"><span>Últimos 10 min</span><b>${num(e.ultimo_10min_mm)} mm</b></div>
      <div class="kv"><span>Última hora</span><b>${num(e.acum_1h_mm)} mm</b></div>
      <div class="kv"><span>24 h al último dato</span><b>${num(e.acum_24h_mm)} mm</b></div>
      ${dayMM == null ? "" : `<span class="badge" style="background:${c.color}">${c.label}</span>`}
    </div>
    <div class="muted" style="font-size:12px;margin-top:6px">Último dato: ${fmtDT.format(new Date(e.ultima_obs_local))} (${ago(e.ultima_obs_local)}) · sensor ${esc(e.sensor_usado)}</div>
    ${fresh ? "" : `<div class="stale">El dato más reciente de esta estación tiene ${ago(e.ultima_obs_local).replace("hace ", "")} de antigüedad. datos.gov.co publica este conjunto con actualización diaria, no en tiempo real.</div>`}`;
}

function renderStations(st) {
  const list = st?.estaciones || [];
  if (!list.length) { $("stations").innerHTML = `<p class="muted">${esc(st?.estado ?? "Sin datos")}</p>`; return; }
  $("stations").innerHTML = `<table><thead><tr><th>Estación</th><th class="num">km</th><th class="num">Último día mm</th><th class="num">24 h mm</th><th>Último dato</th></tr></thead><tbody>` +
    list.map(e => `<tr><td>${esc(e.nombre)}<br><span class="muted">${esc(e.municipio)}</span></td><td class="num">${num(e.dist_km)}</td>` +
      (e.estado === "ok"
        ? `<td class="num">${num(e.acum_hoy_mm ?? e.acum_dia_ultimo_dato_mm)}<br><span class="muted">${esc((e.acum_hoy_mm != null ? "hoy" : e.dia_ultimo_dato?.slice(5)) ?? "")}</span></td><td class="num">${num(e.acum_24h_mm)}</td><td>${ago(e.ultima_obs_local)}</td>`
        : `<td colspan="3" class="muted">sin datos en 48 h</td>`) + `</tr>`).join("") + `</tbody></table>`;
}

function renderForecast(fc) {
  if (!fc) { $("fc-chart").innerHTML = `<p class="muted">Sin pronóstico.</p>`; return; }
  const now = Date.now();
  const h = fc.horario.map(x => ({...x, ms: new Date(x.t + ":00-05:00").getTime()}))
    .filter(x => x.ms >= now - 3600e3 && x.ms < now + 48 * 3600e3);
  const W = 900, H = 185, P = {l: 30, r: 30, t: 24, b: 24};
  const maxmm = Math.max(2, ...h.map(x => x.mm ?? 0));
  const bw = (W - P.l - P.r) / Math.max(h.length, 1);
  const y = (mm) => H - P.b - (mm / maxmm) * (H - P.t - P.b);
  const yp = (p) => H - P.b - (p / 100) * (H - P.t - P.b);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Precipitación horaria pronosticada">`;
  for (const g of [0, maxmm / 2, maxmm]) s += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(g)}" y2="${y(g)}" stroke="#e3e8ec"/><text x="${P.l - 4}" y="${y(g) + 3}" text-anchor="end">${num(g, 1)}</text>`;
  s += `<text x="${P.l}" y="10">mm/h</text><text x="${W - P.r}" y="10" text-anchor="end">%</text>`;
  h.forEach((x, i) => {
    const X = P.l + i * bw;
    s += `<rect x="${X + 1}" y="${y(x.mm ?? 0)}" width="${Math.max(bw - 2, 1)}" height="${H - P.b - y(x.mm ?? 0)}" fill="#3478f6"><title>${fmtDT.format(new Date(x.ms))}: ${num(x.mm)} mm, ${x.prob ?? "—"} %</title></rect>`;
    const hr = Number(fmtH.format(new Date(x.ms)).slice(0, 2));
    if (hr % 6 === 0) s += `<text x="${X}" y="${H - 8}">${hr === 0 ? fmtD.format(new Date(x.ms)) : hr + "h"}</text>`;
  });
  const pts = h.map((x, i) => `${P.l + i * bw + bw / 2},${yp(x.prob ?? 0)}`).join(" ");
  s += `<polyline points="${pts}" fill="none" stroke="#f58c14" stroke-width="1.5"/>`;
  for (const p of [0, 50, 100]) s += `<text x="${W - P.r + 4}" y="${yp(p) + 3}">${p}</text>`;
  s += `</svg><div class="note">Barras: precipitación horaria (mm). Línea naranja: probabilidad de precipitación (%). Modelo numérico vía Open-Meteo; no es observación. La probabilidad sale de un ensamble de modelos y la lámina de un modelo determinístico (best_match): pueden no coincidir (p. ej. 0 mm con 90 %).</div>`;
  $("fc-chart").innerHTML = s;
  $("fc-days").innerHTML = fc.diario.map(d => `<div>${fmtD.format(new Date(d.d + "T12:00:00-05:00"))}<b>${num(d.mm)} mm</b>${d.prob ?? "—"} %</div>`).join("");
}

function renderImerg(im) {
  if (!im) { $("imerg").innerHTML = `<p class="muted">Sin datos.</p>`; return; }
  if (im.estado !== "ok") { $("imerg").innerHTML = `<p class="muted">${esc(im.estado)}</p><p class="note">${esc(im.fuente?.nota)}</p>`; return; }
  $("imerg").innerHTML = `<div class="row" style="display:flex;gap:18px">
      <div class="kv"><span>Acumulado 24 h (celda 0,1°)</span><b>${num(im.acum_24h_mm)} mm</b></div>
      <div class="kv"><span>Último paso</span><b>${im.ultimo_t_utc ? fmtDT.format(new Date(im.ultimo_t_utc)) : "—"}</b></div>
      <div class="kv"><span>Medias horas en 24 h</span><b>${im.n_medias_horas_24h} / 48</b></div></div>
    <p class="note">${esc(im.fuente.nota)}</p>`;
}

function renderGeom(m) {
  if (!m?.geometria_belen) { $("radar-geom").innerHTML = `<p class="muted">Sin día de radar procesado.</p>`; return; }
  const g = m.geometria_belen;
  const warn = g.margen_sobre_sitio_m < 1000;
  $("radar-geom").innerHTML = `<table><tbody>
    <tr><td>Radar</td><td class="num">${esc(m.radar.sitio)} (${num(m.radar.lat, 4)}, ${num(m.radar.lon, 4)}, ${num(m.radar.alt_m, 0)} m)</td></tr>
    <tr><td>Distancia a Belén</td><td class="num">${num(g.distancia_km)} km</td></tr>
    <tr><td>Elevación del barrido usado</td><td class="num">${num(g.elevacion_deg, 2)}°</td></tr>
    <tr><td>Altura del centro del haz sobre Belén</td><td class="num">${num(g.altura_centro_haz_msnm, 0)} m s.n.m.</td></tr>
    <tr><td>Altura del sitio</td><td class="num">${num(g.altura_sitio_msnm, 0)} m s.n.m.</td></tr>
    <tr><td>Margen haz − terreno</td><td class="num"><b class="${warn ? "err" : "ok"}">${num(g.margen_sobre_sitio_m, 0)} m</b></td></tr>
    <tr><td>Acumulado radar en Belén (${esc(m.dia_local)})</td><td class="num">${num(m.acumulado_belen_mm)} mm</td></tr>
    <tr><td>Z–R</td><td class="num">Z = ${m.zr.a}·R<sup>${m.zr.b}</sup> (${esc(m.zr.ref)}), umbral ${m.zr.umbral_dbz} dBZ, tope ${m.zr.tope_granizo_dbz} dBZ</td></tr>
    </tbody></table>
    <p class="note">${esc(g.modelo)}. ${warn ? "El haz pasa muy cerca del terreno: hay bloqueo parcial y la lámina estimada sobre Belén es indicativa, no medida. " : `El haz muestrea a ~${num(g.altura_centro_haz_msnm / 1000, 1)} km s.n.m.: puede quedar por encima de la lluvia de nubes bajas o dentro de la capa de fusión, y el trayecto de 140 km cruza la cordillera; tiende a subestimar la lluvia orográfica. `}Sin corrección de bloqueo, atenuación ni ajuste con pluviómetros.</p>`;
}

function renderSources(estado, parts) {
  const rows = [];
  for (const [key, obj] of parts) {
    const p = estado?.pasos?.[key];
    const f = obj?.fuente || {};
    const st = !p ? "" : !p.ok ? `<span class="err">● error: ${esc(p.error)}</span>`
      : (obj?.estado && obj.estado !== "ok") ? `<span class="muted">● ${esc(obj.estado)}</span>` : '<span class="ok">● ok</span>';
    rows.push(`<div class="src"><b>${esc(f.nombre || key)}</b> ${st}<br>
      ${f.licencia ? `Licencia: ${esc(f.licencia)} · ` : ""}${obj?.consultado_utc ? `Consultado: ${fmtDT.format(new Date(obj.consultado_utc))}` : ""}${obj?.procesado_utc ? `Procesado: ${fmtDT.format(new Date(obj.procesado_utc))}` : ""}<br>
      ${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.url)}</a>` : ""}
      ${f.consulta_series ? `<br>Consulta exacta: <a href="${esc(f.consulta_series)}" target="_blank" rel="noopener">datos.gov.co (SoQL)</a>` : ""}
      ${f.archivos ? `<br>${f.archivos.length} archivos IRIS en ${esc(f.bucket)} (lista completa en meta.json)` : ""}
      ${f.nota ? `<br><span class="muted">${esc(f.nota)}</span>` : ""}</div>`);
  }
  $("sources").innerHTML = rows.join("");
}

// ------------------------------------------------------------------ main
async function main() {
  const estado = await getJSON("estado.json");
  const site = estado?.sitio || {nombre: "Belén, Boyacá", lat: 5.9894, lon: -72.9117, radio_km: 40};
  $("updated").textContent = estado ? `Actualizado ${fmtDT.format(new Date(estado.generado_utc))} (${ago(estado.generado_utc)})` : "Sin datos publicados aún";
  if (!map) initMap(site);
  const [st, fc, im] = await Promise.all([getJSON("estaciones.json"), getJSON("pronostico.json"), getJSON("imerg.json")]);
  drawStationsOnMap(st);
  renderNow(st); renderStations(st); renderForecast(fc); renderImerg(im);
  const m = await loadRadar();
  renderGeom(m);
  renderSources(estado, [["estaciones", st], ["imerg", im], ["pronostico", fc], ["radar", m]]);
  setMode(frames.length ? "frames" : "none");
}

document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.layer)));
$("slider").addEventListener("input", e => { stop(); showFrame(Number(e.target.value)); });
$("play").addEventListener("click", () => {
  if (timer) return stop();
  $("play").textContent = "❚❚";
  timer = setInterval(() => showFrame(idx + 1), 450);
});
$("reload").addEventListener("click", () => location.reload());
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
main();
