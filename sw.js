/* Service worker: makes the installed app open instantly and survive a dead
   signal in the Butler stacks.

   The one rule that matters: dining.json is NEVER served from cache while the
   network is reachable. Showing a stale menu as if it were current is the
   failure this whole project is built to avoid. Cache is a fallback only, and
   the page's own 6-hour staleness banner still fires on top of it. */
const VERSION = 'v1';
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;
const ASSETS = ['./', './index.html', './icon-180.png', './icon-192.png',
                './manifest.webmanifest'];
const DATA_URL = new URL('./dining.json', self.location).toString();

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  // Drop caches from older versions, or a deploy would never reach anyone.
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const { request } = e;
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) return;

  if (new URL(request.url).pathname.endsWith('/dining.json')) {
    e.respondWith((async () => {
      try {
        const res = await fetch(request);
        if (res.ok) {
          // Clone now, synchronously. Cloning inside the waitUntil callback
          // runs after `return res` has handed the body to the page, and a
          // body that is already being read cannot be cloned -- the put then
          // fails silently and the offline fallback is never populated.
          const copy = res.clone();
          e.waitUntil(caches.open(DATA).then(c => c.put(DATA_URL, copy)));
        }
        return res;
      } catch {
        return (await caches.match(DATA_URL))
          || new Response('{}', { status: 503, headers: { 'Content-Type': 'application/json' } });
      }
    })());
    return;
  }

  // Everything else is static and only changes on deploy.
  e.respondWith(caches.match(request).then(hit => hit || fetch(request)));
});
