// Rocsy notification service worker (scope "/"): shows Web Push
// notifications from every studio (sent by the gateway's hub) even with no
// tab open, and opens the right studio when one is tapped. No fetch handler:
// it never touches page loads.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Rocsy", {
    body: data.body || "",
    tag: data.tag || "bunnybytes",
    renotify: true,
    icon: data.icon || "/static/icons/rocsy-v2-192.png",
    badge: data.badge || "/static/icons/rocsy-v2-badge-96.png",
    vibrate: [120, 60, 120],
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    // Prefer a tab already in that studio, then any Rocsy tab.
    const studio = target.pathname.split("/")[1];
    const open = windows.find((client) => studio && new URL(client.url).pathname.split("/")[1] === studio) || windows[0];
    if (open) {
      await open.focus();
      if (open.url !== target.href && "navigate" in open) await open.navigate(target.href).catch(() => {});
      return;
    }
    await self.clients.openWindow(target.href);
  })());
});
