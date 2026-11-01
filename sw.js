const CACHE_NAME = 'tws-livesync-v16-bg-fix';
const ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/tws.jpg',
  '/app-logo.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) return caches.delete(key);
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // CRITICAL FIX FOR PWA STANDALONE BACKGROUND AUDIO:
  // Bypass Service Worker completely for audio streams, API calls, and range requests!
  // If the Service Worker intercepts range requests with fetch(e.request), 
  // Chromium terminates the audio stream when screen locks or app is backgrounded.
  if (
    url.pathname.startsWith('/api/') || 
    e.request.headers.get('range') || 
    e.request.destination === 'audio' ||
    url.pathname.endsWith('.mp4') ||
    url.pathname.endsWith('.m4a') ||
    url.pathname.endsWith('.mp3') ||
    url.pathname.endsWith('.wav')
  ) {
    return;
  }

  // Network first, fallback to cache for static shell
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
