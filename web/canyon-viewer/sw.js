// Service worker: lets the viewer open with no network once it has been loaded online.
//  * The CDN libraries are pinned versions: cached once, served cache-first.
//  * The page's own files: network first (so edits show up), cache when offline.
//  * GeoPackages (.gpkg) are never cached here; the page keeps its own copy in IndexedDB.
// Bump VERSION when the file lists change.
"use strict";

const VERSION = "v10";
const SHELL_CACHE = `viewer-shell-${VERSION}`;
const CDN_CACHE = `viewer-cdn-${VERSION}`;

const SHELL = [
  "./", "style.css", "manifest.webmanifest", "gpkg.js", "gpkg-write.js", "gpkg-update.js", "forecast.js", "weather-icons.js", "share.js", "catalog.js", "catalog-ui.js", "updates-ui.js",
  "install.js", "settings.js", "filecache.js", "companion.js", "companion-store.js",
  "companion-ui.js", "settings-ui.js", "app.js",
  "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png",
];
const CDN = [
  "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css",
  "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js",
  "https://cdn.jsdelivr.net/npm/@ngageoint/geopackage@4.2.10/dist/geopackage.min.js",
  "https://cdn.jsdelivr.net/npm/@ngageoint/geopackage@4.2.10/dist/sql-wasm.wasm",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const shell = await caches.open(SHELL_CACHE);
    await shell.addAll(SHELL.map((path) => new Request(path, { cache: "reload" })));
    const cdn = await caches.open(CDN_CACHE);
    await cdn.addAll(CDN.map((url) => new Request(url, { mode: "cors" })));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== SHELL_CACHE && name !== CDN_CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.pathname.endsWith(".gpkg")) return;

  if (url.origin === self.location.origin) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      try {
        const response = await fetch(request);
        if (response.ok && SHELL.some((p) => new URL(p, self.location).href === url.href.split("?")[0])) {
          cache.put(request.mode === "navigate" ? "./" : request, response.clone());
        }
        return response;
      } catch (err) {
        const hit = await cache.match(request.mode === "navigate" ? "./" : request, { ignoreSearch: true });
        if (hit) return hit;
        throw err;
      }
    })());
  } else if (CDN.includes(request.url)) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
