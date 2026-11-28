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
let map, timer = null;
const R = {index: null, dia: null, scale: "local", prod: "frames", metas: {}, comp: null, sel: new Set(), slots: [], idx: 0, overlays: {}, rings: null, userSel: false};

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
  if (R.prod === "acc") {
    L_.innerHTML = CLASSES.slice(1).map(c => `<span class="item"><i style="background:${c.color}"></i>${c.label}</span>`).join("") + `<span class="item" style="margin-left:4px">mm/día</span>`;
  } else if (R.prod === "frames") {
    L_.innerHTML = DBZ.map(([v, c]) => `<span class="item"><i style="background:${c}"></i>${v}</span>`).join("") + `<span class="item" style="margin-left:4px">dBZ</span>`;
  } else L_.innerHTML = "";
}

// ---- utilidades de radar
const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
const haversineKm = (a, b, c, d) => {
  const r = Math.PI / 180, x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(x));
};
// Los nombres de cuadro son HHMM en UTC; el día local D va de 05:00 UTC de D a 05:00 UTC de D+1.
function slotDate(dia, slot) {
  const y = +dia.slice(0, 4), m = +dia.slice(4, 6) - 1, d = +dia.slice(6, 8), hh = +slot.slice(0, 2), mm = +slot.slice(2, 4);
  return new Date(Date.UTC(y, m, d + (hh < 5 ? 1 : 0), hh, mm));
}
const radarColor = (s) => R.index?.radares?.find(r => r.sitio === s)?.color || "#333";

function layerInfo(key) {   // malla/rutas de una capa en la escala actual, o null
  const sc = R.scale;
  if (key === "compuesto" || key === "dominio") {
    const g = R.comp?.mallas?.[sc];
    if (!g) return null;
    return {bounds: g.bounds, frames: g.frames.map(f => f.slot), acc: `radar/${R.dia}/${g.acumulado_png}`,
            frame: (sl) => `radar/${R.dia}/compuesto/${sc}/frames/${sl}.png`, dominio: `radar/${R.dia}/${g.dominio_png}`};
  }
  const m = R.metas[key];
  if (!m || m.estado !== "ok" || !m.mallas?.[sc]) return null;
  if (sc === "local" && !m.geometria_belen?.cubre_belen) return null;
  const g = m.mallas[sc];
  return {bounds: g.bounds, frames: g.frames.map(f => f.slot), acc: `radar/${R.dia}/${g.acumulado_png}`,
          frame: (sl) => `radar/${R.dia}/${key}/${sc}/frames/${sl}.png`};
}

function availableLayers() {
  const names = (R.index?.radares || []).map(r => r.sitio);
  return [...names, "compuesto", "dominio"].map(k => ({key: k, info: layerInfo(k)}));
}

function layerReason(k) {
  if (k === "compuesto" || k === "dominio") return "requiere ≥ 2 radares con datos ese día en esta escala";
  const m = R.metas[k];
  if (!m) return "sin datos ese día";
  if (m.estado !== "ok") return m.estado;
  if (R.scale === "local" && !m.geometria_belen?.cubre_belen) return `no alcanza Belén (${num(m.geometria_belen?.distancia_km)} km)`;
  return "";
}

function renderLayerPicker() {
  const av = availableLayers();
  if (!R.userSel) {   // selección por defecto: compuesto si existe, si no todos los radares disponibles
    R.sel = new Set(av.find(x => x.key === "compuesto" && x.info) ? ["compuesto"] : av.filter(x => x.info && x.key !== "dominio" && x.key !== "compuesto").map(x => x.key));
  } else {
    for (const k of [...R.sel]) if (!av.find(x => x.key === k && x.info)) R.sel.delete(k);
  }
  const label = (k) => k === "compuesto" ? "Compuesto (haz más bajo)" : k === "dominio" ? "Dominio: qué radar manda" : k;
  $("layers").innerHTML = av.map(({key, info}) => {
    const sw = (key === "compuesto" || key === "dominio") ? `<i class="sw" style="background:linear-gradient(90deg,${(R.index?.radares || []).map(r => r.color).join(",")})"></i>` : `<i class="sw" style="background:${radarColor(key)}"></i>`;
    return `<label class="chk ${info ? "" : "off"}" title="${esc(info ? "" : layerReason(key))}"><input type="checkbox" data-k="${esc(key)}" ${R.sel.has(key) ? "checked" : ""} ${info ? "" : "disabled"}>${sw}${esc(label(key))}${info ? "" : ` <span class="muted">— ${esc(layerReason(key))}</span>`}</label>`;
  }).join("") + `<label class="chk"><input type="checkbox" id="rings" ${R.rings && map.hasLayer(R.rings) ? "checked" : ""}>Anillos de alcance (300 km)</label>`;
  $("layers").querySelectorAll("input[data-k]").forEach(cb => cb.addEventListener("change", () => {
    R.userSel = true;
    cb.checked ? R.sel.add(cb.dataset.k) : R.sel.delete(cb.dataset.k);
    refreshOverlays();
  }));
  $("rings").addEventListener("change", e => e.target.checked ? R.rings.addTo(map) : R.rings.remove());
}

function clearOverlays() { for (const o of Object.values(R.overlays)) o.remove(); R.overlays = {}; }

function refreshOverlays() {
  stop(); clearOverlays();
  const sel = [...R.sel].map(k => ({key: k, info: layerInfo(k)})).filter(x => x.info);
  const nImg = sel.filter(x => x.key !== "dominio").length;
  const op = nImg > 1 ? 0.55 : 0.85;   // con varias capas, semitransparentes para ver la superposición
  if (R.prod !== "none") {
    for (const {key, info} of sel) {
      if (key === "dominio") { R.overlays[key] = L.imageOverlay(`data/${info.dominio}`, info.bounds, {opacity: 0.7, interactive: false}).addTo(map); continue; }
      const url = R.prod === "acc" ? `data/${info.acc}` : BLANK;
      R.overlays[key] = L.imageOverlay(url, info.bounds, {opacity: op, interactive: false}).addTo(map);
    }
  }
  const all = new Set(sel.filter(x => x.key !== "dominio").flatMap(x => x.info.frames));
  R.slots = [...all].sort((a, b) => slotDate(R.dia, a) - slotDate(R.dia, b));
  $("slider").max = Math.max(R.slots.length - 1, 0);
  $("player").style.display = R.prod === "frames" && R.slots.length ? "flex" : "none";
  if (R.prod === "frames") {
    for (const {key, info} of sel) if (key !== "dominio") info.frames.forEach(sl => { const im = new Image(); im.src = `data/${info.frame(sl)}`; });
    showFrame(R.slots.length - 1);
  }
  setLegend();
  renderRadarNote(sel);
}

function showFrame(i) {
  if (!R.slots.length || R.prod !== "frames") return;
  R.idx = (i + R.slots.length) % R.slots.length;
  const sl = R.slots[R.idx];
  const shown = [];
  for (const [key, o] of Object.entries(R.overlays)) {
    if (key === "dominio") continue;
    const info = layerInfo(key);
    if (info.frames.includes(sl)) { o.setUrl(`data/${info.frame(sl)}`); shown.push(key); } else o.setUrl(BLANK);
  }
  $("slider").value = R.idx;
  const ser = R.metas[[...R.sel].find(k => R.metas[k]?.mallas?.local?.serie_belen)]?.mallas?.local?.serie_belen;
  const t = slotDate(R.dia, sl);
  const near = ser?.find(x => Math.abs(new Date(x.t_utc) - t) < 5 * 60e3);
  $("frame-time").textContent = fmtDT.format(t) + (shown.length < Object.keys(R.overlays).filter(k => k !== "dominio").length ? " · capa(s) sin cuadro" : "")
    + (R.scale === "local" && near?.dbz_belen != null ? ` · ${num(near.dbz_belen)} dBZ en Belén` : "");
}

function renderRadarNote(sel) {
  const d = R.dia ? new Date(`${R.dia.slice(0, 4)}-${R.dia.slice(4, 6)}-${R.dia.slice(6, 8)}T12:00:00-05:00`) : null;
  const names = sel.map(x => x.key === "compuesto" ? "compuesto" : x.key).join(" + ") || "ninguna capa";
  $("radar-note").innerHTML = d ? `Día <b>${fmtD.format(d)} ${d.toLocaleDateString("es-CO", {month: "short", year: "numeric", timeZone: TZ})}</b> (hora local) · capas: ${esc(names)}. ` +
    `Los datos del IDEAM en AWS se publican en lote (≈ 1–24 h de retraso): <b>no es lluvia actual</b>.` : "Aún no hay días de radar procesados.";
}

function setProd(p) {
  R.prod = p;
  document.querySelectorAll("#prod button").forEach(b => b.classList.toggle("on", b.dataset.prod === p));
  refreshOverlays();
}

function setScale(sc) {
  R.scale = sc;
  document.querySelectorAll("#scale button").forEach(b => b.classList.toggle("on", b.dataset.scale === sc));
  if (sc === "regional" && R.index?.region_bounds) map.fitBounds(R.index.region_bounds);
  else map.setView([R.site.lat, R.site.lon], 10);
  if (sc === "regional" && R.rings && !map.hasLayer(R.rings)) R.rings.addTo(map);
  renderLayerPicker(); refreshOverlays();
}

async function loadDay(dia) {
  R.dia = dia; R.metas = {}; R.comp = null;
  const ent = R.index.dias.find(d => d.dia === dia);
  await Promise.all(Object.keys(ent?.radares || {}).map(async s => { R.metas[s] = await getJSON(`radar/${dia}/${s}/meta.json`); }));
  if (ent?.compuesto) R.comp = await getJSON(`radar/${dia}/compuesto/meta.json`);
  renderLayerPicker(); refreshOverlays(); renderRadars();
}

function drawRings() {
  R.rings = L.layerGroup();
  for (const r of R.index?.radares || []) {
    L.circle([r.lat, r.lon], {radius: (r.alcance_km || 300) * 1000, color: r.color, weight: 1.5, fill: false, dashArray: "6 4"}).addTo(R.rings);
    L.circleMarker([r.lat, r.lon], {radius: 5, color: r.color, weight: 2, fillColor: "#fff", fillOpacity: 1})
      .bindTooltip(`Radar ${r.sitio}`, {direction: "top"}).addTo(R.rings);
  }
}

async function loadRadar(site) {
  R.site = site;
  R.index = await getJSON("radar/index.json");
  if (!R.index?.dias?.length) { renderRadarNote([]); renderRadars(); return; }
  drawRings();
  $("day").innerHTML = R.index.dias.map(d => {
    const n = Object.values(d.radares).filter(r => r.estado === "ok").length;
    return `<option value="${d.dia}">${d.dia.slice(0, 4)}-${d.dia.slice(4, 6)}-${d.dia.slice(6, 8)} · ${n} radar${n === 1 ? "" : "es"}${d.compuesto ? " · compuesto" : ""}</option>`;
  }).join("");
  const last = R.index.dias.at(-1).dia;
  $("day").value = last;
  await loadDay(last);
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

function renderRadars() {
  const rad = R.index?.radares || [];
  if (!rad.length) { $("radar-geom").innerHTML = `<p class="muted">Sin datos de radar procesados.</p>`; return; }
  const row = (lbl, f) => `<tr><td>${lbl}</td>${rad.map(r => `<td class="num">${f(r, R.metas[r.sitio])}</td>`).join("")}</tr>`;
  const dist = (r, m) => m?.geometria_belen?.distancia_km ?? haversineKm(r.lat, r.lon, R.site.lat, R.site.lon);
  const reach = (r, m) => {
    const d = dist(r, m);
    if (m?.geometria_belen) return m.geometria_belen.cubre_belen ? '<b class="ok">sí</b>' : '<b class="err">no</b>';
    return d > (r.alcance_km || 300) ? `<b class="err">no</b> (&gt; ${num(r.alcance_km || 300, 0)} km)` : "—";
  };
  const comp = R.comp?.mallas?.local;
  $("radar-geom").innerHTML = `<div class="scroll-x"><table><thead><tr><th></th>${rad.map(r => `<th class="num"><i class="sw" style="background:${r.color}"></i>${esc(r.sitio)}</th>`).join("")}</tr></thead><tbody>
    ${row("Estado el día elegido", (r, m) => m ? esc(m.estado) : "sin datos")}
    ${row("Último día en el bucket", r => esc(r.ultimo_dia_visto ?? "—"))}
    ${row("Posición", r => `${num(r.lat, 4)}, ${num(r.lon, 4)}<br><span class="muted">${esc(r.coordenadas)}</span>`)}
    ${row("Distancia a Belén", (r, m) => `${num(dist(r, m))} km`)}
    ${row("¿Alcanza Belén?", reach)}
    ${row("Tarea IRIS / elevación", (r, m) => m?.tarea ? `${esc(m.tarea)} / ${num(m.radar.elevacion_deg, 2)}°` : "—")}
    ${row("Centro del haz sobre Belén", (r, m) => m?.geometria_belen?.altura_centro_haz_msnm != null ? `${num(m.geometria_belen.altura_centro_haz_msnm, 0)} m s.n.m.` : "—")}
    ${row("Margen haz − sitio", (r, m) => m?.geometria_belen?.margen_sobre_sitio_m != null ? `${num(m.geometria_belen.margen_sobre_sitio_m, 0)} m` : "—")}
    ${row("Volúmenes usados", (r, m) => m?.volumenes_usados != null ? `${m.volumenes_usados}${m.completo ? "" : " (incompleto)"}` : "—")}
    ${row("Acumulado en Belén", (r, m) => m?.mallas?.local?.acumulado_belen_mm != null ? `${num(m.mallas.local.acumulado_belen_mm)} mm` : "—")}
    </tbody></table></div>
    ${comp ? `<p class="note"><b>Compuesto en Belén:</b> ${num(comp.acumulado_belen_mm)} mm (manda ${esc(comp.radar_en_belen)}, haz más bajo). Reparto de la ventana: ${Object.entries(comp.fraccion_por_radar).map(([k, v]) => `${esc(k)} ${num(v * 100, 0)} %`).join(", ")}.</p>` : ""}
    <p class="note">Altura del haz: Tierra efectiva 4/3 (Doviak &amp; Zrnić 1993, ec. 2.28b). Z = 200·R<sup>1,6</sup> (Marshall &amp; Palmer 1948), umbral 10 dBZ, tope 53 dBZ. Compuesto: en cada celda manda el radar que la cubre con el haz más bajo. A más de ~4 km de altura el radar puede quedar por encima de la lluvia de nubes bajas o dentro de la capa de fusión: tiende a subestimar la lluvia orográfica. Sin corrección de bloqueo, atenuación ni ajuste con pluviómetros.</p>`;
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
  await loadRadar(site);
  const metas = Object.values(R.metas).filter(m => m?.fuente);
  const rsrc = metas.length ? {fuente: {...metas[0].fuente, nombre: "IDEAM – Red de radares (AWS Open Data): " + metas.map(m => m.sitio).join(", "),
    archivos: metas.flatMap(m => m.fuente.archivos)}, procesado_utc: metas.map(m => m.procesado_utc).sort().at(-1)} : null;
  renderSources(estado, [["estaciones", st], ["imerg", im], ["pronostico", fc], ["radar", rsrc]]);
}

document.querySelectorAll("#prod button").forEach(b => b.addEventListener("click", () => setProd(b.dataset.prod)));
document.querySelectorAll("#scale button").forEach(b => b.addEventListener("click", () => setScale(b.dataset.scale)));
$("day").addEventListener("change", e => loadDay(e.target.value));
$("slider").addEventListener("input", e => { stop(); showFrame(Number(e.target.value)); });
$("play").addEventListener("click", () => {
  if (timer) return stop();
  $("play").textContent = "❚❚";
  timer = setInterval(() => showFrame(R.idx + 1), 450);
});
$("reload").addEventListener("click", () => location.reload());
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
main();
