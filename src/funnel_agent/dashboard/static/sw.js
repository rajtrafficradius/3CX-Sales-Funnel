/* Booked-CRM web-push service worker — background Chrome notifications (works with the tab closed). */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'New booking', body: (e.data && e.data.text()) || '' }; }
  const title = d.title || '🎯 New Lisa booking';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    tag: d.tag || 'booking',
    renotify: true,
    requireInteraction: true,           // high-priority: stays until Alfred acts
    data: { url: d.url || '/lisa-crm' },
    icon: '/logo.png',
    badge: '/logo.png',
    vibrate: [200, 100, 200],
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/lisa-crm';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => {
    for (const c of cs) { if (c.url.includes('/lisa-crm') && 'focus' in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
