const CACHE_NAME = "brajwasi-v16";

const ASSETS = [
  "/",
  "/entry",
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.json"
];

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      cache.addAll(ASSETS).catch(err => console.log("Cache addAll failed", err))
    )
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.map(key => key !== CACHE_NAME && caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const url = event.request.url;

  if (!url.startsWith("http://") && !url.startsWith("https://")) return;
  if (event.request.method !== "GET") return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (
          response &&
          response.status === 200 &&
          response.type !== "opaque" &&
          url.startsWith(self.location.origin)
        ) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() =>
        caches.match(event.request).then(cached => cached || caches.match("/"))
      )
  );
});

self.addEventListener("push", event => {
  let data = {
    title: "Brajwasi Travels",
    body: "New message from admin",
    url: "/entry"
  };

  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch(e) {
    data.body = event.data ? event.data.text() : "New alert";
  }

  event.waitUntil(
    self.registration.showNotification(data.title || "Brajwasi Travels", {
      body: data.body || "New message from admin",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      vibrate: [200, 100, 200],
      tag: "brajwasi-alert-" + Date.now(),
      renotify: true,
      requireInteraction: true,
      data: { url: data.url || "/entry" }
    })
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();

  const targetUrl =
    event.notification.data && event.notification.data.url
      ? event.notification.data.url
      : "/entry";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const client of list) {
        if (client.url.includes(targetUrl) && "focus" in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(targetUrl);
    })
  );
});
