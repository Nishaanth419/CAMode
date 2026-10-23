const CACHE_NAME = 'ca-mode-shell-v36';
const APP_SHELL = ['/', '/css/app.css', '/js/finance.js', '/js/trace.js', '/js/app.js', '/js/pages.js', '/js/rail.js', '/manifest.webmanifest', '/icon.svg', '/favicon.ico', '/icon-192.png'];

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

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Keep account, OAuth, and live agent data online-only; only provide the
  // app shell when navigating offline.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')));
  }
});
