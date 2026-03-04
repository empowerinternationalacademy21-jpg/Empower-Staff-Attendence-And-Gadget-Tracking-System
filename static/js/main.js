/**
 * EIA System — main.js
 * Handles overdue notifications and UI enhancements
 */

// ── Auto-dismiss flash alerts after 5 seconds ──
document.querySelectorAll('.alert').forEach(alert => {
  setTimeout(() => {
    if (alert && alert.parentNode) alert.remove();
  }, 5000);
});

// ── Overdue tablet notification polling (admin only) ──
const overdueBanner = document.getElementById('overdue-banner');

function checkOverdue() {
  if (!overdueBanner) return; // Not on an admin page

  fetch('/api/overdue')
    .then(r => r.json())
    .then(data => {
      if (data.length === 0) {
        overdueBanner.classList.add('hidden');
        overdueBanner.textContent = '';
      } else {
        const msg = data.map(t =>
          `⚠️ ${t.tablet} overdue — Borrowed by ${t.student} (Expected: ${t.expected})`
        ).join('   |   ');
        overdueBanner.innerHTML = `<i class="fas fa-bell"></i>&nbsp;&nbsp;${msg}`;
        overdueBanner.classList.remove('hidden');

        // Browser notification (if permitted)
        if (Notification.permission === 'granted') {
          data.forEach(t => {
            new Notification('EIA System — Overdue Tablet', {
              body: `${t.tablet} — Borrowed by ${t.student}. Expected at ${t.expected}.`,
              icon: '/static/icon.png'
            });
          });
        }
      }
    })
    .catch(() => {/* silently fail */});
}

// Request browser notification permission on load
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

// Poll every 60 seconds
if (overdueBanner) {
  checkOverdue();
  setInterval(checkOverdue, 60000);
}
