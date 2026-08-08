/* PWA Service Worker：App 殼快取優先、資料一律走網路（避免看到舊行情）。
   config.js 走「網路優先」讓登入設定更新能即時生效。 */
const SHELL = "shell-v2";
const ASSETS = ["./", "index.html", "manifest.json", "icon-192.png", "icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.includes("/data/") || url.pathname.includes("/api/")) {
    return;                                   // 資料永遠走網路
  }
  if (url.pathname.endsWith("/config.js")) {  // 設定檔網路優先
    e.respondWith(fetch(e.request).catch(() =>
      caches.match(e.request, { ignoreSearch: true })));
    return;
  }
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true })
      .then(hit => hit || fetch(e.request))
  );
});
