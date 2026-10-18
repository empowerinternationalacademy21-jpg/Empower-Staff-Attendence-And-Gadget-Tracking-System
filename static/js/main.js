/**
 * EIA System — main.js
 * Handles overdue notifications and UI enhancements
 */

// ── Auto-dismiss flash alerts after 5 seconds ──
// (the live overdue list on the dashboard must stay visible, so it is skipped)
document.querySelectorAll('.alert:not(.overdue-list)').forEach(alert => {
  setTimeout(() => {
    if (alert && alert.parentNode) alert.remove();
  }, 5000);
});

// ── Live overdue / tablet status polling (admin pages only) ──
const overdueBanner = document.getElementById('overdue-banner');
const POLL_MS = 10000;            // how often to check (10 seconds)

// Remember which overdue tablets we already sent a desktop notification for,
// so the same tablet doesn't notify again on every poll or page change.
function getNotified() {
  try { return new Set(JSON.parse(sessionStorage.getItem('eiaNotified') || '[]')); }
  catch (e) { return new Set(); }
}
function saveNotified(set) {
  try { sessionStorage.setItem('eiaNotified', JSON.stringify([...set])); } catch (e) {}
}

// Live counts in the navigation menu (little red numbers)
function setBadges(selector, n) {
  document.querySelectorAll(selector).forEach(el => {
    el.textContent = n > 99 ? '99+' : n;
    el.classList.toggle('hidden', n === 0);
  });
}
function updateBadges(tablets, gadgets) {
  setBadges('[data-overdue-count]', tablets);
  setBadges('[data-gadget-overdue-count]', gadgets);
}

// Clicking the red banner opens the matching Overdue page
// (tablets first; gadgets when only gadgets are overdue)
let bannerTarget = overdueBanner ? overdueBanner.dataset.href : '';
if (overdueBanner) {
  overdueBanner.addEventListener('click', () => {
    if (bannerTarget) window.location.href = bannerTarget;
  });
}

function renderBanner(tablets, gadgets) {
  if (!tablets.length && !gadgets.length) {
    overdueBanner.classList.add('hidden');
    overdueBanner.textContent = '';
    return;
  }
  const lines = tablets.map(t =>
      `⚠️ ${t.tablet} overdue — Borrowed by ${t.student} (Expected: ${t.expected})`)
    .concat(gadgets.map(t =>
      `💻 ${t.student} (${t['class']}) gadget overdue — was due ${t.expected}`));
  overdueBanner.textContent = '';
  const icon = document.createElement('i');
  icon.className = 'fas fa-bell';
  overdueBanner.appendChild(icon);
  overdueBanner.appendChild(document.createTextNode('\u00A0\u00A0' + lines.join('   |   ')));
  overdueBanner.classList.remove('hidden');
  bannerTarget = tablets.length ? overdueBanner.dataset.href
                                : (overdueBanner.dataset.hrefGadgets || overdueBanner.dataset.href);
}

// One desktop notification per newly-overdue item (tablet ids and gadget ids are kept apart)
function notifyNew(tablets, gadgets) {
  const notified = getNotified();
  const items = tablets.map(t => ({
      key: 'T' + t.id, title: 'EIA System — Overdue Tablet',
      body: `${t.tablet} — Borrowed by ${t.student}. Expected at ${t.expected}.` }))
    .concat(gadgets.map(t => ({
      key: 'L' + t.id, title: 'EIA System — Overdue Gadget',
      body: `${t.student} (${t['class']}) — gadget was due at ${t.expected}.` })));
  items.forEach(it => {
    if (!notified.has(it.key) && 'Notification' in window &&
        Notification.permission === 'granted') {
      new Notification(it.title, { body: it.body, icon: '/static/icon.png' });
    }
  });
  // keep only what is still overdue (returned items can be re-notified later)
  saveNotified(new Set(items.map(it => it.key)));
}

let pollTimer = null;

function checkStatus() {
  if (!overdueBanner) return;      // not on an admin page

  fetch('/api/tablet-status', { cache: 'no-store', credentials: 'same-origin' })
    .then(r => {
      if (r.status === 401) {      // session ended — stop polling
        clearInterval(pollTimer);
        return null;
      }
      return r.json();
    })
    .then(data => {
      if (!data) return;
      const gadgets = data.gadget_overdue || [];
      renderBanner(data.overdue, gadgets);
      updateBadges(data.overdue.length, gadgets.length);
      notifyNew(data.overdue, gadgets);
      // Let any page (e.g. the dashboard) update itself with the fresh numbers
      document.dispatchEvent(new CustomEvent('tablet-status', { detail: data }));
    })
    .catch(() => {/* network blip — try again on the next tick */});
}

// Request browser notification permission on load
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

if (overdueBanner) {
  checkStatus();
  pollTimer = setInterval(checkStatus, POLL_MS);
  // Refresh immediately when the admin comes back to this tab
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkStatus();
  });
}
