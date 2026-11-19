// Dhadkan recorder service worker: makes the recorder installable and opens it without a network.
// Network first, cache as the fallback, so a new build is always used when the server answers.
// Never touches /api or /ws: clips that cannot be sent wait in the page's IndexedDB queue instead.
const CACHE = "dhadkan-recorder-v1";
const SHELL = ["/?view=record", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/favicon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api") || url.pathname.startsWith("/ws")) return;
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (r.ok && (url.pathname.startsWith("/assets/") || SHELL.includes(url.pathname + url.search))) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      })
      .catch(() =>
        caches
          .match(e.request)
          .then((hit) => hit || (e.request.mode === "navigate" ? caches.match("/?view=record") : undefined))
          .then((hit) => hit || new Response("offline", { status: 503 })),
      ),
  );
});
