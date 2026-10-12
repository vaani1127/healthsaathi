// Web push for patients who opted in (loaded by the generated service worker).
// The message only says that someone opened the record; details are in the signed-in app.
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    self.registration.showNotification(data.title || "HealthSaathi", {
      body: data.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      data: { url: data.url || "/patient" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/patient";
  event.waitUntil(self.clients.openWindow(url));
});
