// Service worker: la interfaz funciona sin red; los datos se piden siempre a la red
// y, si no hay conexión, se sirve la última copia guardada.
const SHELL = "shell-v2";
const DATA = "data-v1";
const SHELL_FILES = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.pathname.includes("/data/")) {
    const key = url.origin + url.pathname; // sin ?v=
    e.respondWith(fetch(e.request).then(r => {
      if (r.ok) { const cp = r.clone(); caches.open(DATA).then(c => c.put(key, cp)); }
      return r;
    }).catch(() => caches.match(key)));
    return;
  }
  // Interfaz: responde con la copia guardada y la actualiza en segundo plano
  // (stale-while-revalidate), para que una nueva versión publicada llegue en la siguiente apertura.
  if (url.origin !== location.origin && url.hostname !== "unpkg.com") return; // teselas: red directa
  e.respondWith(caches.open(SHELL).then(c => c.match(e.request).then(hit => {
    const net = fetch(e.request).then(r => { if (r.ok) c.put(e.request, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  })));
});
