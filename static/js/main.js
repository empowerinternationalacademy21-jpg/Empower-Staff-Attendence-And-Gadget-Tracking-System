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

// Live count in the navigation menu (little red number)
function updateBadges(n) {
  document.querySelectorAll('[data-overdue-count]').forEach(el => {
    el.textContent = n > 99 ? '99+' : n;
    el.classList.toggle('hidden', n === 0);
  });
}

// Clicking the red banner opens the Overdue page
if (overdueBanner && overdueBanner.dataset.href) {
  overdueBanner.addEventListener('click', () => {
    window.location.href = overdueBanner.dataset.href;
  });
}

function renderBanner(overdue) {
  if (!overdue.length) {
    overdueBanner.classList.add('hidden');
    overdueBanner.textContent = '';
    return;
  }
  overdueBanner.textContent = '';
  const icon = document.createElement('i');
  icon.className = 'fas fa-bell';
  overdueBanner.appendChild(icon);
  overdueBanner.appendChild(document.createTextNode(
    '\u00A0\u00A0' + overdue.map(t =>
      `⚠️ ${t.tablet} overdue — Borrowed by ${t.student} (Expected: ${t.expected})`
    ).join('   |   ')
  ));
  overdueBanner.classList.remove('hidden');
}

function notifyNew(overdue) {
  const notified = getNotified();
  const ids = new Set(overdue.map(t => t.id));
  overdue.forEach(t => {
    if (!notified.has(t.id) && 'Notification' in window &&
        Notification.permission === 'granted') {
      new Notification('EIA System — Overdue Tablet', {
        body: `${t.tablet} — Borrowed by ${t.student}. Expected at ${t.expected}.`,
        icon: '/static/icon.png'
      });
    }
  });
  // keep only ids that are still overdue (returned tablets can be re-notified later)
  saveNotified(ids);
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
      renderBanner(data.overdue);
      updateBadges(data.overdue.length);
      notifyNew(data.overdue);
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
