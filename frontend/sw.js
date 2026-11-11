const CACHE_NAME = 'ca-mode-shell-v55';
const APP_SHELL = ['/', '/css/app.css', '/js/demo.js', '/js/finance.js', '/js/trace.js', '/js/ui.js', '/js/app.js', '/js/pages.js', '/js/rail.js', '/manifest.webmanifest', '/icon.svg', '/favicon.ico', '/icon-192.png'];
const SHELL_PATHS = new Set(APP_SHELL);

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys
      .filter((key) => key !== CACHE_NAME)
      .map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

// index.html asks for shell assets with a cache-buster (/js/app.js?v=42) while the cache holds them by path, so
// offline lookups ignore the query. A successful fetch refreshes the cached copy, keyed by path.
function shellAsset(request) {
  return fetch(request)
    .then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(new URL(request.url).pathname, copy)).catch(() => {});
      }
      return response;
    })
    .catch(() => caches.match(request, { ignoreSearch: true })
      .then((cached) => cached || Response.error()));
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Keep account, OAuth, and live agent data online-only; only provide the
  // app shell when navigating offline.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')));
    return;
  }
  if (SHELL_PATHS.has(url.pathname) && url.pathname !== '/') event.respondWith(shellAsset(request));
});
