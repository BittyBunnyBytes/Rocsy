// Trainer service worker: shows Web Push notifications (sent by the server when
// a LoRA finishes training) even when no tab is open, and focuses the Trainer on
// that job when one is tapped. Ported from H3 Machine's sw.js.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

const scopeUrl = new URL(self.registration.scope);

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "BunnyBytes Trainer", {
    body: data.body || "",
    tag: data.tag || "trainer",
    renotify: true,
    icon: `${scopeUrl.pathname}static/icons/icon-192.png`,
    badge: `${scopeUrl.pathname}static/icons/badge-96.png`,
    vibrate: [120, 60, 120],
    data: { url: data.url || scopeUrl.pathname },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || scopeUrl.pathname, self.location.origin);
  const jobId = target.searchParams.get("job");
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).pathname.startsWith(scopeUrl.pathname));
    if (open) {
      await open.focus();
      if (jobId) open.postMessage({ type: "show-job", jobId });
      return;
    }
    await self.clients.openWindow(target.href);
  })());
});
