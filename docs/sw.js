/* PWA Service Worker
   - 頁面(index.html)與設定(config.js)：網路優先，確保永遠拿到最新版介面
   - 資料(data/、api/)：一律走網路，避免看到舊行情
   - 圖示等靜態資源：快取優先 */
const SHELL = "shell-v9";
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
  const isPage = e.request.mode === "navigate" ||
                 url.pathname.endsWith("/index.html") ||
                 url.pathname.endsWith("/config.js");
  if (isPage) {                               // 頁面網路優先，離線才用快取
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy));
        return r;
      }).catch(() =>
        caches.match(e.request, { ignoreSearch: true })
          .then(hit => hit || caches.match("index.html")))
    );
    return;
  }
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true })
      .then(hit => hit || fetch(e.request))
  );
});
