"""
EIA Gadget Tracking System

Backend: Flask + Turso (libSQL, hosted SQLite) via db.py
Required environment variables:
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, SECRET_KEY
Admin logins are stored in the Turso "admins" table (hashed passwords).
Optional emergency login: set ADMIN_USERNAME (default "admin") and
ADMIN_PASSWORD in the environment; it works even if the table is empty.
"""
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, jsonify, make_response)
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time as _time
from functools import wraps
import hmac
import re
import secrets
import os
import io

from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from db import get_db, init_db

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or os.urandom(24).hex()
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024      # student-list uploads (15 MB)

# Create tables/columns if they don't exist (safe on existing data).
# If the database is briefly unreachable at startup, this is retried on the
# next request until it succeeds once, so the app never runs on an old schema.
_schema_ready = False
try:
    init_db()
    _schema_ready = True
except Exception as e:
    print(f"init_db failed: {e}")


@app.before_request
def _ensure_schema():
    global _schema_ready
    if _schema_ready or request.path.startswith('/static'):
        return
    try:
        init_db()
        _schema_ready = True
    except Exception as e:
        print(f"init_db retry failed: {e}")


# ─────────────────────────────────────────────
# TIME (the database stores UTC; the school works in local time)
# ─────────────────────────────────────────────

APP_TZ_NAME = os.environ.get('APP_TIMEZONE', 'Africa/Kampala')
try:
    APP_TZ = ZoneInfo(APP_TZ_NAME)
except Exception:                      # tz database missing -> Uganda is UTC+3, no DST
    APP_TZ_NAME = 'Africa/Kampala'
    APP_TZ = timezone(timedelta(hours=3))


def local_now():
    return datetime.now(APP_TZ)


def local_today():
    return local_now().date()


def to_local(value):
    """Stored UTC value (ISO text or datetime) -> aware local datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace(' ', 'T', 1) if ' ' in value else value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(APP_TZ)


def to_utc_iso(dt):
    """Aware local datetime -> naive UTC ISO text (how the database stores it)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


# ─────────────────────────────────────────────
# TEAM ACTIVITY LOG (visible to every admin)
# ─────────────────────────────────────────────

# action code -> (label, icon, colour class)
ACTION_META = {
    'login':           ('Logged in',             'fa-sign-in-alt',  'a-grey'),
    'logout':          ('Logged out',            'fa-sign-out-alt', 'a-grey'),
    'tablet_register': ('Registered tablets',    'fa-plus-circle',  'a-green'),
    'tablet_remove':   ('Removed tablet',        'fa-trash-alt',    'a-red'),
    'tablet_signout':  ('Signed out tablet',     'fa-hand-holding', 'a-amber'),
    'tablet_return':   ('Signed in tablet',      'fa-undo',         'a-teal'),
    'student_add':     ('Added student',         'fa-user-plus',    'a-green'),
    'student_remove':  ('Removed students',      'fa-user-minus',   'a-red'),
    'student_import':  ('Imported students',     'fa-file-import',  'a-blue'),
    'laptop_student_add':    ('Added gadget student',    'fa-laptop',       'a-green'),
    'laptop_student_edit':   ('Edited gadget student',   'fa-user-edit',    'a-blue'),
    'laptop_student_remove': ('Removed gadget students', 'fa-user-minus',   'a-red'),
    'laptop_import':   ('Imported gadget students', 'fa-file-import', 'a-blue'),
    'timetable_edit':  ('Edited timetable',      'fa-calendar-alt', 'a-blue'),
    'timetable_consent': ('Recorded consent',    'fa-signature',    'a-teal'),
    'timetable_pdf':   ('Timetable PDF',         'fa-file-pdf',     'a-blue'),
    'class_promote':   ('Promoted / moved students', 'fa-level-up-alt', 'a-green'),
    'class_undo':      ('Undid a class change',  'fa-undo',         'a-amber'),
    'laptop_signout':  ('Signed out gadget',     'fa-laptop',       'a-amber'),
    'laptop_return':   ('Signed in gadget',      'fa-undo',         'a-teal'),
    'laptop_blocked':  ('Gadget sign-out blocked', 'fa-ban',        'a-red'),
    'tablet_blocked':  ('Tablet sign-out blocked', 'fa-ban',        'a-red'),
    'blacklisted':     ('Added to blacklist',    'fa-user-slash',   'a-red'),
    'suspended':       ('Suspended student',     'fa-user-lock',    'a-red'),
    'suspension_lifted': ('Lifted suspension',   'fa-lock-open',    'a-teal'),
    'teacher_add':     ('Updated teachers register', 'fa-chalkboard-teacher', 'a-green'),
    'teacher_remove':  ('Removed teacher',       'fa-user-minus',   'a-red'),
    'teacher_signout': ('Teacher tablet sign-out', 'fa-hand-holding', 'a-amber'),
    'teacher_return':  ('Teacher tablets signed in', 'fa-undo',     'a-teal'),
    # kept only so older log entries still display properly
    'staff_add':       ('Added staff',           'fa-user-plus',    'a-green'),
    'staff_edit':      ('Edited staff',          'fa-user-edit',    'a-blue'),
    'staff_delete':    ('Removed staff',         'fa-user-minus',   'a-red'),
    'report_pdf':      ('Downloaded report',     'fa-file-pdf',     'a-blue'),
}
HISTORIC_ACTIONS = {'staff_add', 'staff_edit', 'staff_delete', 'report_pdf'}


def log_activity(action, details='', admin=None):
    """Record what an admin did so the whole team can see it.
    Never raises: a logging problem must not stop the real action."""
    admin = admin or session.get('admin') or 'system'
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO activity_log (admin, action, details, created_at) "
                "VALUES (?,?,?,?)",
                (admin, action, (details or '')[:500], datetime.utcnow().isoformat()))
    except Exception as e:
        print(f"activity log failed: {e}")


@app.context_processor
def _inject_activity_meta():
    return {'action_meta': ACTION_META,
            'action_filter_meta': {k: v for k, v in ACTION_META.items()
                                   if k not in HISTORIC_ACTIONS}}


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')  # optional emergency login


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 'admin' must be present so every action can be attributed to a
        # named admin account (older sessions without it must log in again).
        if 'logged_in' not in session or not session.get('admin'):
            flash('Please log in to access this page.', 'warning')
            if request.method == 'GET':
                session['login_next'] = request.full_path.rstrip('?')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def current_admin():
    """Username of the logged-in admin (recorded on tablet sign-out/return)."""
    return session.get('admin')


def _after_login_url():
    nxt = session.pop('login_next', None)
    if nxt and nxt.startswith('/') and not nxt.startswith('//') and '\\' not in nxt:
        return nxt
    return url_for('dashboard')


def _safe_equals(a, b):
    return hmac.compare_digest((a or '').encode(), (b or '').encode())


# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        # 1) Admin accounts stored in the Turso "admins" table
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT username, password_hash, full_name FROM admins "
                    "WHERE username=? AND is_active=1", (username,)
                ).fetchone()
            if row and check_password_hash(row['password_hash'], password):
                session['logged_in'] = True
                session['admin'] = row['username']
                flash(f"Welcome back, {row['full_name'] or row['username']}!", 'success')
                log_activity('login', 'Logged in', admin=row['username'])
                return redirect(_after_login_url())
        except Exception as e:
            print(f"admin login lookup failed: {e}")

        # 2) Optional emergency login from environment variables
        if (ADMIN_PASSWORD
                and _safe_equals(username, ADMIN_USERNAME)
                and _safe_equals(password, ADMIN_PASSWORD)):
            session['logged_in'] = True
            session['admin'] = ADMIN_USERNAME
            flash('Welcome back, Admin!', 'success')
            log_activity('login', 'Logged in (emergency login)', admin=ADMIN_USERNAME)
            return redirect(_after_login_url())

        flash('Invalid credentials.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    who = session.get('admin')
    if who:
        log_activity('logout', 'Logged out', admin=who)
    session.clear()
    flash('You have been logged out successfully.', 'info')
    response = make_response(redirect(url_for('index')))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        total_tablets = conn.execute(
            "SELECT COUNT(*) FROM tablets WHERE is_active=1").fetchone()[0]
        borrowed_count = conn.execute(
            "SELECT COUNT(*) FROM tablet_transactions WHERE status='Borrowed'"
        ).fetchone()[0]

        overdue = conn.execute("""
            SELECT tt.*, t.tablet_id AS tab_code
            FROM tablet_transactions tt
            JOIN tablets t ON tt.tablet_id = t.id
            WHERE tt.status='Borrowed' AND tt.expected_return_time < ?
        """, (now,)).fetchall()

        recently_returned = conn.execute("""
            SELECT tt.*, t.tablet_id AS tab_code
            FROM tablet_transactions tt
            JOIN tablets t ON tt.tablet_id = t.id
            WHERE tt.status='Returned'
            ORDER BY tt.sign_back_time DESC LIMIT 5
        """).fetchall()

        # ---- Personal gadgets ----
        lap = conn.execute(
            "SELECT (SELECT COUNT(*) FROM laptop_students WHERE is_active=1) AS students, "
            "(SELECT COUNT(DISTINCT se.student_id) FROM laptop_sessions se "
            " JOIN laptop_students ls ON ls.id = se.student_id "
            " WHERE se.status='Out' AND ls.is_active=1) AS out_now").fetchone()
        gad_overdue_rows = _gadget_overdue_rows(conn)

        # ---- Data for the dashboard charts ----
        since = (local_today() - timedelta(days=13)).isoformat()
        out_rows = conn.execute(
            "SELECT substr(sign_out_time,1,10) AS d, COUNT(*) AS c "
            "FROM tablet_transactions WHERE substr(sign_out_time,1,10) >= ? "
            "GROUP BY d", (since,)).fetchall()
        back_rows = conn.execute(
            "SELECT substr(sign_back_time,1,10) AS d, COUNT(*) AS c "
            "FROM tablet_transactions WHERE sign_back_time IS NOT NULL "
            "AND substr(sign_back_time,1,10) >= ? GROUP BY d", (since,)).fetchall()
        class_rows = conn.execute(
            "SELECT student_class AS cls, COUNT(*) AS c FROM tablet_transactions "
            "WHERE student_class IS NOT NULL AND student_class <> '' AND teacher_id IS NULL "
            "GROUP BY student_class ORDER BY c DESC LIMIT 8").fetchall()
        gad_session_rows = conn.execute(
            "SELECT sign_out_time, expected_return_time, sign_back_time, status "
            "FROM laptop_sessions WHERE substr(sign_out_time,1,10) >= ?",
            ((local_today() - timedelta(days=14)).isoformat(),)).fetchall()

    out_map = {r['d']: r['c'] for r in out_rows}
    back_map = {r['d']: r['c'] for r in back_rows}
    days = [(local_today() - timedelta(days=i)) for i in range(13, -1, -1)]
    overdue_n = len(overdue)

    # gadget sessions per (school) day: returned on time vs late / still overdue
    on_time, late = {}, {}
    for r in gad_session_rows:
        day = to_local(r['sign_out_time']).date().isoformat()
        was_late = ((r['sign_back_time'] and r['sign_back_time'] > r['expected_return_time'])
                    or (not r['sign_back_time'] and r['expected_return_time'] < now))
        (late if was_late else on_time)[day] = (late if was_late else on_time).get(day, 0) + 1

    gad_students, gad_out, gad_overdue_n = lap['students'], lap['out_now'], len(gad_overdue_rows)
    chart_data = {
        'tablet_status': {
            'labels': ['Available', 'Borrowed', 'Overdue'],
            'values': [max(total_tablets - borrowed_count, 0),
                       max(borrowed_count - overdue_n, 0),
                       overdue_n],
            'total': total_tablets,
        },
        'daily': {
            'labels': [d.strftime('%d %b') for d in days],
            'signouts': [out_map.get(d.isoformat(), 0) for d in days],
            'returns': [back_map.get(d.isoformat(), 0) for d in days],
        },
        'classes': {
            'labels': [r['cls'] for r in class_rows],
            'values': [r['c'] for r in class_rows],
        },
        'gadget_status': {
            'labels': ['In storage', 'Out (in session)', 'Overdue'],
            'values': [max(gad_students - gad_out, 0), max(gad_out - gad_overdue_n, 0),
                       gad_overdue_n],
            'total': gad_students,
        },
        'gadget_daily': {
            'labels': [d.strftime('%d %b') for d in days],
            'on_time': [on_time.get(d.isoformat(), 0) for d in days],
            'late': [late.get(d.isoformat(), 0) for d in days],
        },
    }

    try:
        with get_db() as conn:
            team_activity = conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT 8").fetchall()
    except Exception as e:
        print(f"team activity unavailable: {e}")
        team_activity = []

    return render_template('dashboard.html',
                           chart_data=chart_data,
                           team_activity=team_activity,
                           total_tablets=total_tablets,
                           borrowed_tablets=borrowed_count,
                           in_storage=max(total_tablets - borrowed_count, 0),
                           overdue_tablets=overdue,
                           recently_returned=recently_returned,
                           gad_students=gad_students,
                           gad_out=gad_out,
                           gad_in=max(gad_students - gad_out, 0),
                           gad_overdue_n=gad_overdue_n,
                           overdue_gadgets=_gadget_overdue_payload(gad_overdue_rows),
                           today=local_today())


# ─────────────────────────────────────────────
# FAIR-USE RULES
#   1. A student may borrow a tablet only ONCE per school day.
#   2. A tablet or personal gadget returned more than 20 minutes after it was
#      due puts the student on the BLACKLIST: no tablet or gadget sign-out for
#      4 days. After that the student can sign out again automatically.
# The server enforces both rules, so they cannot be bypassed from the screen.
# ─────────────────────────────────────────────

def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


LATE_GRACE_MINUTES = _env_int('BLACKLIST_LATE_MINUTES', 20)   # later than this = blacklisted
BLACKLIST_DAYS = max(1, _env_int('BLACKLIST_DAYS', 4))         # length of the sign-out ban


def person_key(name, cls):
    """Identifies a student: same name and class, ignoring case, spaces and punctuation
    ('Mukasa  Grace' / 'P.5' is the same person as 'mukasa grace' / 'P5')."""
    def flat(text):
        return re.sub(r'[\W_]+', '', (text or '').casefold())
    return f"{flat(name)}|{flat(cls)}"


def _who(name, cls):
    return f"{name} ({cls})" if cls else name


def _utc_naive(value):
    """Stored UTC text -> naive UTC datetime."""
    return to_local(value).astimezone(timezone.utc).replace(tzinfo=None)


def minutes_between(due_iso, back_iso):
    """Minutes from the due time to the return (negative = returned early)."""
    return (to_local(back_iso) - to_local(due_iso)).total_seconds() / 60


def active_blacklist(conn, key, now_iso=None):
    """The blacklist entry that currently blocks this student, or None."""
    now_iso = now_iso or datetime.utcnow().isoformat()
    return conn.execute(
        "SELECT * FROM blacklist WHERE person_key=? AND blocked_until > ? "
        "ORDER BY blocked_until DESC LIMIT 1", (key, now_iso)).fetchone()


def record_late_return(conn, *, kind, source, source_id, name, cls, device,
                       due_iso, back_iso, admin):
    """Blacklist the student when the item came back MORE than LATE_GRACE_MINUTES
    after it was due. Returns the new blacklist entry, or None (on time / already
    recorded). Safe to call twice for the same return."""
    if not due_iso:
        return None
    late = minutes_between(due_iso, back_iso)
    if late <= LATE_GRACE_MINUTES:
        return None
    until_iso = (_utc_naive(back_iso) + timedelta(days=BLACKLIST_DAYS)).isoformat()
    res = conn.execute("""
        INSERT OR IGNORE INTO blacklist
            (person_key, person_name, class_name, kind, source, source_id, device,
             due_time, returned_time, minutes_late, blocked_from, blocked_until, recorded_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (person_key(name, cls), name, cls or '', kind, source, source_id, device or '',
          due_iso, back_iso, int(late), back_iso, until_iso, admin))
    if not res.rowcount:
        return None
    log_activity('blacklisted',
                 f"{_who(name, cls)} returned a {kind.lower()} {int(late)} min late "
                 f"({device or kind}) — no sign-outs until {fmt_dt_full(until_iso)}",
                 admin=admin)
    return {'name': name, 'minutes_late': int(late), 'until': until_iso}


def blacklist_short(row):
    """One-sentence reason, for flash messages and the gadget desk."""
    return (f"{_who(row['person_name'], row['class_name'])} is on the blacklist after returning a "
            f"{row['kind'].lower()} {row['minutes_late']} minutes late, and cannot sign anything "
            f"out until {fmt_dt_full(row['blocked_until'])}.")


def overdue_item_held(conn, key):
    """(description, due_iso) of an unreturned tablet/gadget that is already past
    the allowance (so the student cannot take another one), or None."""
    cutoff = (datetime.utcnow() - timedelta(minutes=LATE_GRACE_MINUTES)).isoformat()
    for r in conn.execute(
            "SELECT tt.student_name, tt.student_class, tt.expected_return_time, "
            "t.tablet_id AS code FROM tablet_transactions tt "
            "JOIN tablets t ON t.id = tt.tablet_id "
            "WHERE tt.status='Borrowed' AND tt.expected_return_time < ?", (cutoff,)).fetchall():
        if person_key(r['student_name'], r['student_class']) == key:
            return f"tablet {r['code']}", r['expected_return_time']
    for r in conn.execute(
            "SELECT t.full_name, t.class_name, se.expected_return_time "
            "FROM laptop_sessions se JOIN laptop_students t ON t.id = se.student_id "
            "WHERE se.status='Out' AND se.expected_return_time < ?", (cutoff,)).fetchall():
        if person_key(r['full_name'], r['class_name']) == key:
            return 'their personal gadget', r['expected_return_time']
    return None


def signout_barrier(conn, name, cls):
    """Why this student may not sign out ANY tablet or gadget right now, or None.
    Returns a dict for the screen: title, message and a few detail rows."""
    key, who = person_key(name, cls), _who(name, cls)

    sus = active_suspension(conn, key)
    if sus:
        return {
            'title': 'Sign-out denied: student is suspended',
            'message': (f"{who} is suspended from using tablets and personal gadgets until "
                        f"{fmt_date(sus['ends_at'])}. Reason: {sus['category']}."),
            'details': [
                ('Student', who),
                ('Reason', sus['category']),
                ('Suspended since', fmt_date(sus['starts_at'])),
                ('Period', _weeks_label(sus['weeks'])),
                ('Can sign out again', fmt_date(sus['ends_at'])),
                ('Suspended by', sus['created_by']),
            ],
        }

    row = active_blacklist(conn, key)
    if row:
        device = f" ({row['device']})" if row['device'] else ''
        return {
            'title': 'Sign-out denied: student is on the blacklist',
            'until': row['blocked_until'],
            'message': (f"{who} returned a {row['kind'].lower()} more than {LATE_GRACE_MINUTES} minutes "
                        f"after it was due. Sign-outs are suspended for {BLACKLIST_DAYS} days."),
            'details': [
                ('Student', who),
                ('Late return', f"{row['kind']}{device}, {row['minutes_late']} min late"),
                ('Suspended since', fmt_dt_full(row['blocked_from'])),
                ('Can sign out again', fmt_dt_full(row['blocked_until'])),
            ],
        }

    held = overdue_item_held(conn, key)
    if held:
        what, due_iso = held
        mins = int(minutes_between(due_iso, to_utc_iso(local_now())))
        return {
            'title': 'Sign-out denied: an earlier item has not been returned',
            'message': (f"{who} still has {what}, which was due back at {fmt_dt_time(due_iso)} "
                        f"and is now {fmt_minutes(mins)} overdue. It must be returned first."),
            'details': [('Student', who), ('Outstanding item', what),
                        ('Was due', fmt_dt_full(due_iso))],
        }
    return None


def tablet_taken_today(conn, key, now_local=None):
    """The tablet this student has already been given today (school day), or None."""
    now_local = now_local or local_now()
    start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=APP_TZ)
    rows = conn.execute(
        "SELECT tt.student_name, tt.student_class, tt.sign_out_time, t.tablet_id AS code "
        "FROM tablet_transactions tt JOIN tablets t ON t.id = tt.tablet_id "
        "WHERE tt.sign_out_time >= ? AND tt.sign_out_time < ? ORDER BY tt.sign_out_time",
        (to_utc_iso(start), to_utc_iso(start + timedelta(days=1)))).fetchall()
    for r in rows:
        if person_key(r['student_name'], r['student_class']) == key:
            return r
    return None


def daily_limit_denial(name, cls, taken):
    who = _who(name, cls)
    return {
        'title': 'Sign-out denied: daily tablet limit reached',
        'message': (f"{who} has already borrowed a tablet today. Each student may sign out "
                    f"a tablet only once per school day. They can borrow again tomorrow."),
        'details': [
            ('Student', who),
            ('Tablet issued today', taken['code']),
            ('Issued at', fmt_dt_time(taken['sign_out_time'])),
            ('Next eligible', 'Tomorrow'),
        ],
    }


def student_flags(conn):
    """person_key -> (tag, note) for the student search lists, so an admin sees
    'BLACKLISTED' / 'TAKEN TODAY' before pressing the button."""
    flags = {}
    now_iso = datetime.utcnow().isoformat()
    now_local = local_now()
    start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=APP_TZ)
    for r in conn.execute(
            "SELECT tt.student_name, tt.student_class, t.tablet_id AS code "
            "FROM tablet_transactions tt JOIN tablets t ON t.id = tt.tablet_id "
            "WHERE tt.sign_out_time >= ? AND tt.sign_out_time < ?",
            (to_utc_iso(start), to_utc_iso(start + timedelta(days=1)))).fetchall():
        flags[person_key(r['student_name'], r['student_class'])] = (
            'TAKEN TODAY', f"Tablet {r['code']} already issued today")
    for r in conn.execute(      # blacklist wins over 'taken today'
            "SELECT person_key, blocked_until FROM blacklist WHERE blocked_until > ?",
            (now_iso,)).fetchall():
        flags[r['person_key']] = ('BLACKLISTED', f"Suspended until {fmt_dt_full(r['blocked_until'])}")
    for key, r in suspension_map(conn).items():      # a formal suspension wins over everything
        flags[key] = ('SUSPENDED', f"Suspended until {fmt_date(r['ends_at'])}")
    return flags


def _time_left(until_iso):
    secs = int((to_local(until_iso) - local_now()).total_seconds())
    if secs <= 0:
        return 'Expired'
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {mins} min'
    return f'{max(mins, 1)} min'


def _blacklist_row(r):
    now = local_now()
    start, end = to_local(r['blocked_from']), to_local(r['blocked_until'])
    span = max((end - start).total_seconds(), 1)
    done = min(max((now - start).total_seconds() / span, 0), 1)
    return {
        'id': r['id'], 'name': r['person_name'], 'class': r['class_name'] or '',
        'kind': r['kind'], 'device': r['device'] or '', 'late': r['minutes_late'],
        'due': r['due_time'], 'returned': r['returned_time'],
        'from': r['blocked_from'], 'until': r['blocked_until'],
        'left': _time_left(r['blocked_until']), 'progress': int(done * 100),
        'by': r['recorded_by'] or '',
    }


@app.route('/blacklist')
@login_required
def blacklist_page():
    """Students who returned a tablet or gadget more than 20 minutes late."""
    now_iso = datetime.utcnow().isoformat()
    with get_db() as conn:
        active = conn.execute(
            "SELECT * FROM blacklist WHERE blocked_until > ? ORDER BY blocked_until",
            (now_iso,)).fetchall()
        history = conn.execute(
            "SELECT * FROM blacklist WHERE blocked_until <= ? "
            "ORDER BY blocked_until DESC LIMIT 100", (now_iso,)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
    return render_template('blacklist.html',
                           active=[_blacklist_row(r) for r in active],
                           history=[_blacklist_row(r) for r in history],
                           total=total, grace=LATE_GRACE_MINUTES, days=BLACKLIST_DAYS)


# ─────────────────────────────────────────────
# SUSPENSIONS
# An admin formally suspends a student (picked from the school registers) for a number of
# weeks. Until the period ends the student cannot sign out ANY tablet or personal gadget.
# Every admin sees it (Suspensions page, search lists, gadget desk, activity log) and the
# server enforces it in signout_barrier(), so it applies to both kinds of device.
# The block ends by itself when the date passes; an admin can also lift it early.
# ─────────────────────────────────────────────

SUSPENSION_REASONS = [
    'Misuse of a tablet or gadget',
    'Damage to a tablet or gadget',
    'Inappropriate content or use',
    'Breaking school or device rules',
    'Repeated late returns',
    'Tampering with settings or security',
    'Other',
]
SUSPENSION_WEEKS = [1, 2, 3, 4, 6, 8, 10, 12]
MAX_SUSPENSION_WEEKS = 52


def _weeks_label(weeks):
    weeks = int(weeks or 0)
    return f"{weeks} week{'' if weeks == 1 else 's'}"


def suspension_map(conn):
    """{person_key: suspension row} for every suspension that is in force right now."""
    now_iso = datetime.utcnow().isoformat()
    out = {}
    for r in conn.execute(
            "SELECT * FROM suspensions WHERE status='Active' AND ends_at > ? "
            "ORDER BY ends_at", (now_iso,)).fetchall():
        for key in (r['person_keys'] or '').split():
            out[key] = r                      # the longest one wins (ordered by end date)
    return out


def active_suspension(conn, key):
    """The suspension that currently blocks this student, or None."""
    return suspension_map(conn).get(key)


def suspension_short(row):
    return (f"{_who(row['student_name'], row['class_name'])} is suspended from using tablets and "
            f"personal gadgets until {fmt_date(row['ends_at'])} ({row['category']}).")


def _carry_suspension(conn, kind, name, old_class, new_class):
    """A student moved class (promotion / undo): keep their suspension attached to them.
    If the same student is still listed in the OTHER register under the old class, the
    old identity stays valid too, so the suspension cannot be dodged through that list."""
    old_key, new_key = person_key(name, old_class), person_key(name, new_class)
    if old_key == new_key:
        return
    other = 'laptop_students' if kind == 'students' else 'students'
    still = conn.execute(
        f"SELECT 1 FROM {other} WHERE is_active=1 AND full_name = ? COLLATE NOCASE "
        f"AND class_name = ? COLLATE NOCASE", (name, old_class or '')).fetchone()
    for r in conn.execute("SELECT id, person_keys FROM suspensions WHERE status='Active'").fetchall():
        keys = (r['person_keys'] or '').split()
        if old_key not in keys:
            continue
        keys = [k for k in keys if k != old_key or still]
        if new_key not in keys:
            keys.append(new_key)
        conn.execute("UPDATE suspensions SET person_keys=?, class_name=? WHERE id=?",
                     (' '.join(keys), new_class or '', r['id']))


def suspendable_students(conn):
    """Every active student in either register, once each, for the picker on the form."""
    seen = {}
    for table, label in (('students', 'School register'),
                         ('laptop_students', 'Personal gadget student')):
        for r in conn.execute(
                f"SELECT full_name, class_name FROM {table} WHERE is_active=1 "
                f"ORDER BY full_name COLLATE NOCASE").fetchall():
            key = person_key(r['full_name'], r['class_name'])
            if key in seen:
                if label.startswith('Personal'):
                    seen[key]['sub'] = 'School register · personal gadget student'
                continue
            seen[key] = {'n': r['full_name'], 'c': r['class_name'] or '', 'k': key, 'sub': label}
    return list(seen.values())


def _suspension_row(r):
    now = local_now()
    start, end = to_local(r['starts_at']), to_local(r['ends_at'])
    if r['status'] == 'Lifted':
        state = 'Lifted'
    elif r['ends_at'] <= datetime.utcnow().isoformat():
        state = 'Completed'
    else:
        state = 'Active'
    span = max((end - start).total_seconds(), 1)
    done = min(max((now - start).total_seconds() / span, 0), 1)
    return {
        'id': r['id'], 'name': r['student_name'], 'class': r['class_name'] or '',
        'category': r['category'], 'reason': r['reason'], 'weeks': r['weeks'],
        'weeks_label': _weeks_label(r['weeks']),
        'starts': r['starts_at'], 'ends': r['ends_at'], 'by': r['created_by'],
        'state': state, 'left': _time_left(r['ends_at']) if state == 'Active' else '',
        'progress': int(done * 100) if state == 'Active' else 100,
        'lifted_by': r['lifted_by'] or '', 'lifted_at': r['lifted_at'],
        'lift_note': r['lift_note'] or '', 'created_at': r['created_at'],
    }


@app.route('/suspensions')
@login_required
def suspensions_page():
    """Every admin can see who is suspended, why, by whom and until when."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM suspensions ORDER BY ends_at DESC").fetchall()
    items = [_suspension_row(r) for r in rows]
    active = sorted((i for i in items if i['state'] == 'Active'), key=lambda i: i['ends'])
    history = [i for i in items if i['state'] != 'Active'][:100]
    return render_template('suspensions.html', active=active, history=history, total=len(items))


@app.route('/suspensions/new', methods=['GET', 'POST'])
@login_required
def suspension_new():
    admin = current_admin()
    form = request.form if request.method == 'POST' else {}
    error = None
    with get_db() as conn:
        students = suspendable_students(conn)
        current = suspension_map(conn)
        if request.method == 'POST':
            by_key = {s['k']: s for s in students}
            student = by_key.get((request.form.get('student_key') or '').strip())
            category = (request.form.get('category') or '').strip()
            reason = re.sub(r'[ \t]+', ' ', (request.form.get('reason') or '').strip())
            period = (request.form.get('period') or '').strip()
            raw_weeks = request.form.get('custom_weeks') if period == 'custom' else period
            try:
                weeks = int(raw_weeks)
            except (TypeError, ValueError):
                weeks = 0

            if not student:
                error = 'Choose the student from the list that appears as you type.'
            elif category not in SUSPENSION_REASONS:
                error = 'Choose a reason category.'
            elif len(reason) < 10:
                error = 'Describe what happened (at least a short sentence) so other admins understand.'
            elif len(reason) > 1000:
                error = 'The description is too long (1000 characters at most).'
            elif not 1 <= weeks <= MAX_SUSPENSION_WEEKS:
                error = f'Choose a suspension period between 1 and {MAX_SUSPENSION_WEEKS} weeks.'
            elif not request.form.get('confirm'):
                error = 'Please tick the box to confirm the suspension.'
            elif student['k'] in current:
                error = (f"{_who(student['n'], student['c'])} is already suspended until "
                         f"{fmt_date(current[student['k']]['ends_at'])}.")

            if not error:
                now_local = local_now()
                # the student may sign out again on the morning of the end date
                end_day = datetime.combine(now_local.date() + timedelta(weeks=weeks),
                                           datetime.min.time(), tzinfo=APP_TZ)
                res = conn.execute("""
                    INSERT INTO suspensions (person_keys, student_name, class_name, category,
                        reason, weeks, starts_at, ends_at, status, created_by)
                    VALUES (?,?,?,?,?,?,?,?,'Active',?)
                """, (student['k'], student['n'], student['c'], category, reason, weeks,
                      to_utc_iso(now_local), to_utc_iso(end_day), admin))
                sid = res.lastrowid
                log_activity('suspended',
                             f"{_who(student['n'], student['c'])} suspended for {_weeks_label(weeks)} "
                             f"until {fmt_date(to_utc_iso(end_day))} — {category}")
                flash(f"{student['n']} is suspended for {_weeks_label(weeks)}. They cannot sign out "
                      f"a tablet or personal gadget until {fmt_date(to_utc_iso(end_day))}.", 'success')
                return redirect(url_for('suspension_view', sid=sid))

    picker = []
    for s in students:
        item = dict(s)
        if s['k'] in current:
            item['tag'] = 'SUSPENDED'
            item['sub'] = f"Already suspended until {fmt_date(current[s['k']]['ends_at'])}"
        picker.append(item)
    return render_template('suspension_new.html', students=picker, admin=admin, form=form,
                           error=error, reasons=SUSPENSION_REASONS, weeks_options=SUSPENSION_WEEKS,
                           max_weeks=MAX_SUSPENSION_WEEKS, today_iso=local_today().isoformat())


@app.route('/suspensions/<int:sid>')
@login_required
def suspension_view(sid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM suspensions WHERE id=?", (sid,)).fetchone()
    if not row:
        flash('That suspension record was not found.', 'danger')
        return redirect(url_for('suspensions_page'))
    return render_template('suspension_view.html', s=_suspension_row(row))


@app.route('/suspensions/<int:sid>/lift', methods=['POST'])
@login_required
def suspension_lift(sid):
    admin = current_admin()
    note = (request.form.get('note') or '').strip()[:300]
    with get_db() as conn:
        row = conn.execute("SELECT * FROM suspensions WHERE id=?", (sid,)).fetchone()
        if not row:
            flash('That suspension record was not found.', 'danger')
            return redirect(url_for('suspensions_page'))
        res = conn.execute(
            "UPDATE suspensions SET status='Lifted', lifted_by=?, lifted_at=?, lift_note=? "
            "WHERE id=? AND status='Active' AND ends_at > ?",
            (admin, datetime.utcnow().isoformat(), note, sid, datetime.utcnow().isoformat()))
        if not res.rowcount:
            flash('That suspension has already ended.', 'info')
            return redirect(url_for('suspension_view', sid=sid))
    log_activity('suspension_lifted',
                 f"Lifted the suspension of {_who(row['student_name'], row['class_name'])} early"
                 + (f" — {note}" if note else ''))
    flash(f"Suspension lifted. {row['student_name']} can sign out devices again.", 'success')
    return redirect(url_for('suspensions_page'))


# ─────────────────────────────────────────────
# TABLET MANAGEMENT
# ─────────────────────────────────────────────

def tablet_status(conn, tablet_db_id):
    row = conn.execute(
        "SELECT id FROM tablet_transactions WHERE tablet_id=? AND status='Borrowed'",
        (tablet_db_id,)
    ).fetchone()
    return 'Borrowed' if row else 'Available'


@app.route('/tablets')
@login_required
def tablet_list():
    with get_db() as conn:
        tablets_raw = conn.execute("SELECT * FROM tablets WHERE is_active=1").fetchall()
        tablets = [{**dict(t), 'current_status': tablet_status(conn, t['id'])}
                   for t in tablets_raw]
    return render_template('tablet_list.html', tablets=tablets)


# All school tablets share one prefix; each tablet has its own code number.
TABLET_PREFIX = (os.environ.get('TABLET_PREFIX') or 'MFL').strip().upper()
MAX_TABLET_BATCH = 200


def parse_tablet_codes(raw):
    """Turn text like '1045, 1050, 1060-1065' into (codes, unreadable_parts).

    * numbers separated by commas, spaces or new lines
    * ranges with a dash: 1060-1065 (a leading zero is kept: 001-003)
    * the prefix may be typed too: MFL-1045 is read as 1045
    """
    text = re.sub(rf'(?i){re.escape(TABLET_PREFIX)}[\s_-]*', '', raw or '')
    text = re.sub(r'\s*[-\u2013\u2014]\s*', '-', text)
    codes, bad, seen = [], [], set()

    def add(code):
        n = int(code)
        if n not in seen:
            seen.add(n)
            codes.append(code)

    for tok in re.split(r'[,;\s]+', text):
        if not tok:
            continue
        m = re.fullmatch(r'(\d{1,6})-(\d{1,6})', tok)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi or hi - lo + 1 > MAX_TABLET_BATCH:
                bad.append(tok)
                continue
            width = len(m.group(1)) if m.group(1).startswith('0') else 0
            for n in range(lo, hi + 1):
                add(str(n).zfill(width))
        elif re.fullmatch(r'\d{1,6}', tok):
            add(tok)
        else:
            bad.append(tok)
    return codes, bad


def _tablet_form_context(conn):
    """Numbers already registered under the MFL prefix (for the live preview)."""
    rows = conn.execute(
        "SELECT tablet_id, is_active FROM tablets WHERE tablet_id LIKE ?",
        (f'{TABLET_PREFIX}-%',)).fetchall()
    used, inactive = [], []
    for r in rows:
        suffix = r['tablet_id'][len(TABLET_PREFIX) + 1:]
        if suffix.isdigit():
            (used if r['is_active'] else inactive).append(int(suffix))
    count = conn.execute("SELECT COUNT(*) FROM tablets WHERE is_active=1").fetchone()[0]
    return count, used, inactive


@app.route('/tablets/add', methods=['GET', 'POST'])
@login_required
def add_tablet():
    form_codes, form_device = '', ''

    if request.method == 'POST':
        form_codes = request.form.get('codes', '')
        form_device = (request.form.get('device_name') or '').strip()[:60]
        codes, bad = parse_tablet_codes(form_codes)

        if bad:
            flash('Could not read: ' + ', '.join(bad[:8]) +
                  '. Use numbers like 1045, or ranges like 1060-1065.', 'warning')
        if not codes:
            flash('Enter at least one tablet code number.', 'danger')
        elif len(codes) > MAX_TABLET_BATCH:
            flash(f'Please register at most {MAX_TABLET_BATCH} tablets at a time.', 'danger')
        else:
            name = form_device or 'Tablet'
            added, reactivated, duplicates = [], [], []
            try:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT id, tablet_id, is_active FROM tablets WHERE tablet_id LIKE ?",
                        (f'{TABLET_PREFIX}-%',)).fetchall()
                    existing = {}
                    for r in rows:
                        suffix = r['tablet_id'][len(TABLET_PREFIX) + 1:]
                        if suffix.isdigit():
                            existing[int(suffix)] = r

                    new_rows, back_ids = [], []
                    for code in codes:
                        ex = existing.get(int(code))
                        if ex is None:
                            new_rows.append((f'{TABLET_PREFIX}-{code}', name))
                            added.append(f'{TABLET_PREFIX}-{code}')
                        elif ex['is_active']:
                            duplicates.append(ex['tablet_id'])
                        else:   # previously removed tablet: bring it back
                            back_ids.append(ex['id'])
                            reactivated.append(ex['tablet_id'])

                    statements = []
                    if new_rows:   # one INSERT for every new tablet (atomic)
                        marks = ','.join(['(?,?)'] * len(new_rows))
                        statements.append((
                            f"INSERT INTO tablets (tablet_id, name) VALUES {marks}",
                            [v for row in new_rows for v in row]))
                    if back_ids:
                        marks = ','.join('?' * len(back_ids))
                        statements.append((
                            f"UPDATE tablets SET is_active=1, name=? WHERE id IN ({marks})",
                            [name] + back_ids))
                    if statements:
                        conn.run_batch(statements)   # one request to the database
            except Exception as e:
                print(f"tablet registration failed: {e}")
                flash('Could not save the tablets. Nothing was registered - '
                      'please try again.', 'danger')
                added = reactivated = []
                duplicates = []
                codes = []

            def preview(items):
                return ', '.join(items[:8]) + (f' and {len(items) - 8} more' if len(items) > 8 else '')

            if added:
                flash(f'{len(added)} tablet(s) registered: {preview(added)}', 'success')
            if reactivated:
                flash(f'Restored previously removed tablet(s): {preview(reactivated)}', 'info')
            if duplicates:
                flash(f'Already registered, skipped: {preview(duplicates)}', 'warning')
            if added or reactivated:
                bits = []
                if added:
                    bits.append(f'Registered {len(added)} tablet(s): {preview(added)}')
                if reactivated:
                    bits.append(f'Restored: {preview(reactivated)}')
                log_activity('tablet_register', '. '.join(bits))
                return redirect(url_for('tablet_list'))

    with get_db() as conn:
        tablet_count, used, inactive = _tablet_form_context(conn)
    return render_template('add_tablet.html',
                           prefix=TABLET_PREFIX,
                           tablet_count=tablet_count,
                           used_nums=used, inactive_nums=inactive,
                           form_codes=form_codes, form_device=form_device)


@app.route('/tablets/signout', methods=['GET', 'POST'])
@login_required
def tablet_signout():
    """The logged-in admin signs a tablet out and types the return time."""
    admin = current_admin()
    form = request.form if request.method == 'POST' else {}
    denial = None          # set when a fair-use rule stops the sign-out

    with get_db() as conn:
        if request.method == 'POST':
            tablet_db_id = request.form.get('tablet_id')
            student_name = (request.form.get('student_name') or '').strip()
            student_class = (request.form.get('student_class') or '').strip()
            return_time = (request.form.get('return_time') or '').strip()
            try:
                quantity = max(1, min(int(request.form.get('quantity', 1)), 5))
            except ValueError:
                quantity = 1

            now_local = local_now()
            ret_local, error = None, None
            try:
                hh, mm = (int(x) for x in return_time.split(':')[:2])
                ret_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except (ValueError, TypeError):
                error = 'Please enter a valid return time.'

            if not error and not (tablet_db_id and student_name and student_class):
                error = 'Please fill in the student name, class and tablet.'
            if not error and ret_local <= now_local:
                error = (f'The return time must be later than the current time '
                         f'({now_local.strftime("%I:%M %p")}).')
            if not error and tablet_status(conn, tablet_db_id) == 'Borrowed':
                error = 'That tablet is currently borrowed. Choose another.'

            # Fair-use rules: blacklist / unreturned item first, then once per day
            if not error:
                denial = signout_barrier(conn, student_name, student_class)
                if not denial:
                    taken = tablet_taken_today(conn, person_key(student_name, student_class),
                                               now_local)
                    if taken:
                        denial = daily_limit_denial(student_name, student_class, taken)
                if denial:
                    log_activity('tablet_blocked',
                                 f"Blocked: {_who(student_name, student_class)} — "
                                 f"{denial['title'].split(': ', 1)[-1]}")

            if denial:
                pass                        # the form is shown again with the denial notice
            elif error:
                flash(error, 'danger')      # the form is shown again with what was typed
            else:
                duration_hours = round((ret_local - now_local).total_seconds() / 3600, 2)
                conn.execute("""
                    INSERT INTO tablet_transactions
                        (tablet_id, student_name, student_class, quantity,
                         duration_hours, sign_out_time, expected_return_time,
                         status, signed_out_by)
                    VALUES (?,?,?,?,?,?,?,'Borrowed',?)
                """, (tablet_db_id, student_name, student_class, quantity,
                      duration_hours, to_utc_iso(now_local), to_utc_iso(ret_local), admin))

                tab = conn.execute(
                    "SELECT tablet_id FROM tablets WHERE id=?", (tablet_db_id,)).fetchone()
                due = ret_local.strftime('%I:%M %p')
                log_activity('tablet_signout',
                             f'Signed out {tab["tablet_id"]} to {student_name} '
                             f'({student_class}), due back at {due}')
                flash(f'Tablet {tab["tablet_id"]} signed out to {student_name}, '
                      f'approved by {admin}. Return by {due}.', 'success')
                return redirect(url_for('tablet_signout'))

        all_tablets = conn.execute("SELECT * FROM tablets WHERE is_active=1").fetchall()
        available = [t for t in all_tablets
                     if tablet_status(conn, t['id']) == 'Available']
        register = conn.execute(
            "SELECT full_name, class_name FROM students WHERE is_active=1 "
            "ORDER BY full_name COLLATE NOCASE").fetchall()
        flags = student_flags(conn)
    students = []
    for r in register:
        item = {'n': r['full_name'], 'c': r['class_name'] or ''}
        flag = flags.get(person_key(r['full_name'], r['class_name']))
        if flag:
            item['tag'], item['sub'] = flag
        students.append(item)
    return render_template('tablet_signout.html', tablets=available, admin=admin,
                           form=form, server_now_ms=int(_time.time() * 1000),
                           tz_name=APP_TZ_NAME, students=students, denial=denial,
                           blacklist_days=BLACKLIST_DAYS, late_minutes=LATE_GRACE_MINUTES)


@app.route('/tablets/transactions')
@login_required
def tablet_transactions():
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        active = conn.execute("""
            SELECT tt.*, t.tablet_id AS tab_code
            FROM tablet_transactions tt
            JOIN tablets t ON tt.tablet_id = t.id
            WHERE tt.status='Borrowed'
            ORDER BY tt.sign_out_time DESC
        """).fetchall()
        history = conn.execute("""
            SELECT tt.*, t.tablet_id AS tab_code
            FROM tablet_transactions tt
            JOIN tablets t ON tt.tablet_id = t.id
            WHERE tt.status='Returned'
            ORDER BY tt.sign_back_time DESC LIMIT 50
        """).fetchall()
    return render_template('tablet_transactions.html',
                           active=active, history=history, now=now)


@app.route('/tablets/return/<int:tx_id>', methods=['POST'])
@login_required
def tablet_return(tx_id):
    admin = current_admin()
    back = (url_for('overdue_tablets') if request.form.get('next') == 'overdue'
            else url_for('tablet_transactions'))
    with get_db() as conn:
        tx = conn.execute(
            "SELECT tt.student_name, tt.student_class, tt.status, tt.signed_out_by, "
            "tt.expected_return_time, tt.teacher_id, t.tablet_id AS tab_code "
            "FROM tablet_transactions tt JOIN tablets t ON tt.tablet_id = t.id "
            "WHERE tt.id=?", (tx_id,)
        ).fetchone()
        if not tx:
            flash('Transaction not found.', 'danger')
            return redirect(back)
        if tx['status'] != 'Borrowed':
            flash(f"Tablet {tx['tab_code']} was already signed back in.", 'info')
            return redirect(back)
        back_iso = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE tablet_transactions "
            "SET status='Returned', sign_back_time=?, signed_back_by=? "
            "WHERE id=? AND status='Borrowed'",
            (back_iso, admin, tx_id))
        # more than 20 minutes late -> blacklist (4 days without sign-outs).
        # Teachers are never blacklisted; their late tablets only show as overdue.
        banned = None if tx['teacher_id'] else record_late_return(
            conn, kind='Tablet', source='tablet', source_id=tx_id,
            name=tx['student_name'], cls=tx['student_class'], device=tx['tab_code'],
            due_iso=tx['expected_return_time'], back_iso=back_iso, admin=admin)
    log_activity('tablet_return',
                 f"Signed in {tx['tab_code']} from {tx['student_name']}"
                 + (f" (signed out by {tx['signed_out_by']})" if tx['signed_out_by'] else ''))
    if banned:
        flash(f"Tablet {tx['tab_code']} returned by {tx['student_name']}, signed in by {admin}. "
              f"Returned {banned['minutes_late']} minutes late: {tx['student_name']} has been added "
              f"to the blacklist and cannot sign anything out until {fmt_dt_full(banned['until'])}.",
              'warning')
    else:
        flash(f"Tablet {tx['tab_code']} returned by {tx['student_name']}, "
              f"signed in by {admin}.", 'success')
    return redirect(back)


@app.route('/tablets/overdue')
@login_required
def overdue_tablets():
    """All overdue tablets, who has them, and which admin approved the sign-out.
    The page keeps itself up to date (see templates/overdue.html)."""
    with get_db() as conn:
        rows = _overdue_rows(conn)
    return render_template('overdue.html',
                           overdue=_overdue_payload(rows),
                           server_now_ms=int(_time.time() * 1000))


@app.route('/tablets/delete/<int:tablet_id>', methods=['POST'])
@login_required
def delete_tablet(tablet_id):
    with get_db() as conn:
        tablet = conn.execute(
            "SELECT * FROM tablets WHERE id=? AND is_active=1", (tablet_id,)
        ).fetchone()
        if not tablet:
            flash('Tablet not found.', 'danger')
            return redirect(url_for('tablet_list'))

        if tablet_status(conn, tablet_id) == 'Borrowed':
            flash(
                f'Cannot remove {tablet["tablet_id"]} — it is currently borrowed. '
                f'Wait for it to be returned first.',
                'danger')
            return redirect(url_for('tablet_list'))

        # Soft delete — preserves transaction history (foreign key safe)
        conn.execute("UPDATE tablets SET is_active=0 WHERE id=?", (tablet_id,))
    log_activity('tablet_remove', f'Removed tablet {tablet["tablet_id"]} (damaged / lost)')
    flash(f'Tablet {tablet["tablet_id"]} has been removed from the system.', 'success')
    return redirect(url_for('tablet_list'))


# ─────────────────────────────────────────────
# TEACHER SIGN-OUT
#   • /teachers            the teachers register (add one or many names, deactivate)
#   • /teachers/signout    sign out ANY number of tablets to one teacher in one go
#   • /teachers/checkouts  open checkouts (sign tablets back in, one by one or all at once)
#                          and the full history: every tablet code, who took it, who approved it
#
# A teacher's tablets are stored in the SAME table as student tablets
# (tablet_transactions), one row per tablet, with teacher_id set and one shared
# checkout_id per sign-out. That is why everything that already exists keeps working
# for teachers with no extra code: a tablet that is out shows as Borrowed in the
# inventory, cannot be issued twice, appears on the Overdue page, in the live banner
# and menu badges, and cannot be removed while it is out.
#
# Teachers are NOT subject to the student fair-use rules: no daily limit, no
# blacklist, no suspension. A late teacher is simply shown as overdue.
# ─────────────────────────────────────────────

TEACHER_CLASS = 'Teacher'              # shown in the "Class" column for teacher rows
MAX_TEACHER_TABLETS = max(1, _env_int('TEACHER_MAX_TABLETS', 60))


def _code_preview(codes, limit=8):
    shown = ', '.join(codes[:limit])
    return shown + (f' and {len(codes) - limit} more' if len(codes) > limit else '')


def _tablet_number(code):
    """'MFL-1045' -> 1045 (None when the code has no number)."""
    suffix = (code or '').rsplit('-', 1)[-1]
    return int(suffix) if suffix.isdigit() else None


@app.route('/teachers')
@login_required
def teachers_page():
    now_iso = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT te.id, te.full_name, te.phone, te.created_by,
                   (SELECT COUNT(*) FROM tablet_transactions tt
                     WHERE tt.teacher_id = te.id AND tt.status='Borrowed') AS holding,
                   (SELECT COUNT(*) FROM tablet_transactions tt
                     WHERE tt.teacher_id = te.id AND tt.status='Borrowed'
                       AND tt.expected_return_time < ?) AS overdue
            FROM teachers te WHERE te.is_active=1
            ORDER BY te.full_name COLLATE NOCASE
        """, (now_iso,)).fetchall()
    return render_template('teachers.html', teachers=rows, total=len(rows))


@app.route('/teachers/add', methods=['POST'])
@login_required
def teachers_add():
    admin = current_admin()
    phone = re.sub(r'[^\d+ ()-]', '', (request.form.get('phone') or '').strip())[:25]
    names, seen = [], set()
    for part in re.split(r'[\n\r;]+', request.form.get('names') or ''):
        name = clean_person_name(part)
        if len(name) >= 3 and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    names = names[:200]
    if not names:
        flash('Type at least one teacher name (one name per line).', 'danger')
        return redirect(url_for('teachers_page'))

    added, restored, already, stmts = [], [], [], []
    with get_db() as conn:
        existing = {r['full_name'].lower(): r for r in conn.execute(
            "SELECT id, full_name, is_active FROM teachers").fetchall()}
        for name in names:
            ex = existing.get(name.lower())
            if ex is None:
                stmts.append(("INSERT INTO teachers (full_name, phone, created_by) VALUES (?,?,?)",
                              (name, phone if len(names) == 1 else '', admin)))
                added.append(name)
            elif ex['is_active']:
                already.append(name)
            else:
                stmts.append(("UPDATE teachers SET is_active=1 WHERE id=?", (ex['id'],)))
                restored.append(name)
        if stmts:
            conn.run_batch(stmts)

    if added:
        flash(f"{len(added)} teacher{'s' if len(added) != 1 else ''} added: {_code_preview(added)}.", 'success')
    if restored:
        flash(f"Restored: {_code_preview(restored)}.", 'info')
    if already:
        flash(f"Already in the register, skipped: {_code_preview(already)}.", 'warning')
    if added or restored:
        log_activity('teacher_add', f"Teachers register: added {len(added)}"
                                    + (f" ({_code_preview(added, 5)})" if added else '')
                                    + (f", restored {_code_preview(restored, 5)}" if restored else ''))
    return redirect(url_for('teachers_page'))


@app.route('/teachers/<int:tid>/remove', methods=['POST'])
@login_required
def teachers_remove(tid):
    with get_db() as conn:
        te = conn.execute("SELECT id, full_name FROM teachers WHERE id=? AND is_active=1",
                          (tid,)).fetchone()
        if not te:
            flash('Teacher not found.', 'danger')
            return redirect(url_for('teachers_page'))
        held = conn.execute(
            "SELECT COUNT(*) FROM tablet_transactions WHERE teacher_id=? AND status='Borrowed'",
            (tid,)).fetchone()[0]
        if held:
            flash(f"{te['full_name']} still has {held} tablet{'s' if held != 1 else ''} out. "
                  f"Sign them in first, then remove the teacher.", 'danger')
            return redirect(url_for('teachers_page'))
        # soft delete: the history of what this teacher borrowed stays intact
        conn.execute("UPDATE teachers SET is_active=0 WHERE id=?", (tid,))
    log_activity('teacher_remove', f"Removed {te['full_name']} from the teachers register")
    flash(f"{te['full_name']} removed from the register. Their past sign-outs are kept in the history.",
          'success')
    return redirect(url_for('teachers_page'))


@app.route('/teachers/signout', methods=['GET', 'POST'])
@login_required
def teacher_signout():
    """The logged-in admin signs out any number of tablets to one teacher."""
    admin = current_admin()
    form = request.form if request.method == 'POST' else {}

    with get_db() as conn:
        teachers = conn.execute(
            "SELECT id, full_name FROM teachers WHERE is_active=1 "
            "ORDER BY full_name COLLATE NOCASE").fetchall()
        all_tablets = conn.execute(
            "SELECT id, tablet_id, name FROM tablets WHERE is_active=1").fetchall()
        borrowed = {r['tablet_id']: r['student_name'] for r in conn.execute(
            "SELECT tablet_id, student_name FROM tablet_transactions WHERE status='Borrowed'"
        ).fetchall()}
        by_id = {t['id']: t for t in all_tablets}
        by_num = {}
        for t in all_tablets:
            n = _tablet_number(t['tablet_id'])
            if n is not None and t['tablet_id'].startswith(f'{TABLET_PREFIX}-'):
                by_num[n] = t

        if request.method == 'POST':
            now_local = local_now()
            error = None

            teacher = None
            tid = (request.form.get('teacher_id') or '').strip()
            if tid.isdigit():
                teacher = conn.execute("SELECT id, full_name FROM teachers WHERE id=? AND is_active=1",
                                       (int(tid),)).fetchone()

            ret_local, time_error = None, None
            try:
                hh, mm = (int(x) for x in (request.form.get('return_time') or '').strip().split(':')[:2])
                ret_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except (ValueError, TypeError):
                time_error = 'Please enter a valid return time.'

            # tablets: the ticked boxes plus any codes typed in the box (duplicates are ignored)
            picked, picked_set, unknown = [], set(), []
            for raw in request.form.getlist('tablet_ids'):
                if raw.isdigit() and int(raw) in by_id and int(raw) not in picked_set:
                    picked_set.add(int(raw))
                    picked.append(by_id[int(raw)])
            codes, bad = parse_tablet_codes(request.form.get('codes', ''))
            for c in codes:
                t = by_num.get(int(c))
                if t is None:
                    unknown.append(f'{TABLET_PREFIX}-{c}')
                elif t['id'] not in picked_set:
                    picked_set.add(t['id'])
                    picked.append(t)
            picked.sort(key=lambda t: _natural_key(t['tablet_id']))

            if not teacher:
                error = 'Choose the teacher from the list. (Add them under Teachers first if they are missing.)'
            elif bad:
                error = 'Could not read: ' + ', '.join(bad[:8]) + '. Use numbers like 1045 or ranges like 1060-1065.'
            elif unknown:
                error = 'Not registered or removed: ' + _code_preview(unknown) + '.'
            elif not picked:
                error = 'Choose at least one tablet (tick it, or type its code number).'
            elif len(picked) > MAX_TEACHER_TABLETS:
                error = f'At most {MAX_TEACHER_TABLETS} tablets can be signed out in one go.'
            elif time_error:
                error = time_error
            elif ret_local <= now_local:
                error = (f'The return time must be later than the current time '
                         f'({now_local.strftime("%I:%M %p")}).')
            if not error:
                busy = [f"{t['tablet_id']} (with {borrowed[t['id']]})" for t in picked if t['id'] in borrowed]
                if busy:
                    error = ('Already signed out, so not issued: ' + _code_preview(busy)
                             + '. Untick them, or sign them back in first.')

            if error:
                flash(error, 'danger')          # the form is shown again with what was typed
            else:
                checkout = secrets.token_hex(5)
                out_iso, due_iso = to_utc_iso(now_local), to_utc_iso(ret_local)
                hours = round((ret_local - now_local).total_seconds() / 3600, 2)
                # One row per tablet. The NOT EXISTS guard stops a tablet being issued twice
                # even if two admins press the button at the same moment.
                conn.run_batch([(
                    "INSERT INTO tablet_transactions (tablet_id, student_name, student_class, quantity, "
                    "duration_hours, sign_out_time, expected_return_time, status, signed_out_by, "
                    "teacher_id, checkout_id) "
                    "SELECT ?,?,?,1,?,?,?,'Borrowed',?,?,? "
                    "WHERE NOT EXISTS (SELECT 1 FROM tablet_transactions WHERE tablet_id=? AND status='Borrowed')",
                    (t['id'], teacher['full_name'], TEACHER_CLASS, hours, out_iso, due_iso, admin,
                     teacher['id'], checkout, t['id'])) for t in picked])
                issued = conn.execute(
                    "SELECT COUNT(*) FROM tablet_transactions WHERE checkout_id=?",
                    (checkout,)).fetchone()[0]
                if issued != len(picked):
                    # someone else took a tablet in the meantime: undo ours so nothing is half-issued
                    conn.execute("DELETE FROM tablet_transactions WHERE checkout_id=?", (checkout,))
                    flash('One of the tablets was signed out by someone else a moment ago. '
                          'Nothing was issued - please check the list and try again.', 'danger')
                else:
                    codes_out = [t['tablet_id'] for t in picked]
                    due = ret_local.strftime('%I:%M %p')
                    log_activity('teacher_signout',
                                 f"Signed out {len(codes_out)} tablet{'s' if len(codes_out) != 1 else ''} to "
                                 f"{teacher['full_name']}, due back at {due}: {', '.join(codes_out)}")
                    flash(f"{len(codes_out)} tablet{'s' if len(codes_out) != 1 else ''} signed out to "
                          f"{teacher['full_name']}, approved by {admin}: {_code_preview(codes_out, 12)}. "
                          f"Return by {due}.", 'success')
                    return redirect(url_for('teacher_checkouts'))

        available = []
        for t in all_tablets:
            if t['id'] in borrowed:
                continue
            available.append({'id': t['id'], 'code': t['tablet_id'], 'name': t['name'] or '',
                              'num': _tablet_number(t['tablet_id']) if t['tablet_id'].startswith(f'{TABLET_PREFIX}-') else None})
        available.sort(key=lambda t: _natural_key(t['code']))

    return render_template('teacher_signout.html', teachers=teachers, tablets=available, admin=admin,
                           form=form, picked={str(i) for i in request.form.getlist('tablet_ids')},
                           server_now_ms=int(_time.time() * 1000), tz_name=APP_TZ_NAME,
                           prefix=TABLET_PREFIX, max_tablets=MAX_TEACHER_TABLETS,
                           out_count=len(borrowed))


def _group_teacher_rows(rows):
    """One entry per sign-out (checkout), listing every tablet code in it."""
    groups, order = {}, []
    for r in rows:
        key = r['checkout_id'] or f"row{r['id']}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {'checkout': r['checkout_id'] or '', 'teacher': r['teacher'],
                               'teacher_id': r['teacher_id'], 'out': r['sign_out_time'],
                               'due': r['expected_return_time'], 'by': r['signed_out_by'] or '',
                               'items': [], 'back': None, 'back_by': '', 'late': False}
            order.append(key)
        g['items'].append({'id': r['id'], 'code': r['code'], 'back': r['sign_back_time']})
        if r['sign_back_time']:
            if g['back'] is None or r['sign_back_time'] > g['back']:
                g['back'], g['back_by'] = r['sign_back_time'], r['signed_back_by'] or ''
            if r['sign_back_time'] > r['expected_return_time']:
                g['late'] = True
    for g in groups.values():
        g['items'].sort(key=lambda i: _natural_key(i['code']))
    return [groups[k] for k in order]


@app.route('/teachers/checkouts')
@login_required
def teacher_checkouts():
    q = (request.args.get('q') or '').strip()[:60]
    now_iso = datetime.utcnow().isoformat()
    where, params = ["tt.teacher_id IS NOT NULL"], []
    if q:
        where.append("(tt.student_name LIKE ? ESCAPE '\\' OR t.tablet_id LIKE ? ESCAPE '\\')")
        pat = f"%{_like_escape(q)}%"
        params += [pat, pat]
    base = ("SELECT tt.id, tt.checkout_id, tt.teacher_id, tt.student_name AS teacher, tt.sign_out_time, "
            "tt.expected_return_time, tt.sign_back_time, tt.signed_out_by, tt.signed_back_by, "
            "t.tablet_id AS code FROM tablet_transactions tt JOIN tablets t ON t.id = tt.tablet_id "
            "WHERE " + ' AND '.join(where))
    with get_db() as conn:
        open_rows = conn.execute(base + " AND tt.status='Borrowed' "
                                 "ORDER BY tt.expected_return_time, tt.id", params).fetchall()
        done_rows = conn.execute(base + " AND tt.status='Returned' "
                                 "ORDER BY tt.sign_back_time DESC LIMIT 400", params).fetchall()

    open_groups = _group_teacher_rows(open_rows)
    now_utc = to_utc_iso(local_now())
    overdue_tablets = 0
    for g in open_groups:
        g['overdue'] = g['due'] < now_iso
        if g['overdue']:
            overdue_tablets += len(g['items'])
            g['late_text'] = fmt_minutes(int(minutes_between(g['due'], now_utc)))
        else:
            g['left_text'] = _time_left(g['due'])
    history = _group_teacher_rows(done_rows)[:100]
    return render_template('teacher_checkouts.html', open_groups=open_groups, history=history, q=q,
                           tablets_out=len(open_rows), overdue_tablets=overdue_tablets)


@app.route('/teachers/return', methods=['POST'])
@login_required
def teacher_return():
    """Sign teacher tablets back in: the ticked ones, or every tablet of one checkout."""
    admin = current_admin()
    back = redirect(url_for('teacher_checkouts', q=request.form.get('q', '')))
    ids = [int(i) for i in request.form.getlist('ids') if i.isdigit()][:500]
    checkout = (request.form.get('checkout') or '').strip()[:20]
    with get_db() as conn:
        if request.form.get('do') == 'all' and checkout:
            ids = [r['id'] for r in conn.execute(
                "SELECT id FROM tablet_transactions WHERE checkout_id=? AND teacher_id IS NOT NULL "
                "AND status='Borrowed'", (checkout,)).fetchall()]
        if not ids:
            flash('Tick at least one tablet to sign in.', 'warning')
            return back
        marks = ','.join('?' * len(ids))
        rows = conn.execute(
            f"SELECT tt.id, tt.student_name AS teacher, tt.expected_return_time, t.tablet_id AS code "
            f"FROM tablet_transactions tt JOIN tablets t ON t.id = tt.tablet_id "
            f"WHERE tt.id IN ({marks}) AND tt.teacher_id IS NOT NULL AND tt.status='Borrowed'",
            ids).fetchall()
        if not rows:
            flash('Those tablets were already signed back in.', 'info')
            return back
        back_iso = datetime.utcnow().isoformat()
        marks2 = ','.join('?' * len(rows))
        conn.execute(
            f"UPDATE tablet_transactions SET status='Returned', sign_back_time=?, signed_back_by=? "
            f"WHERE id IN ({marks2}) AND status='Borrowed' AND teacher_id IS NOT NULL",
            [back_iso, admin] + [r['id'] for r in rows])

    by_teacher = {}
    for r in rows:
        by_teacher.setdefault(r['teacher'], []).append(r['code'])
    for teacher, codes in by_teacher.items():
        codes.sort(key=_natural_key)
        log_activity('teacher_return',
                     f"Signed in {len(codes)} tablet{'s' if len(codes) != 1 else ''} from {teacher}: "
                     f"{', '.join(codes)}")
    late = sum(1 for r in rows if r['expected_return_time'] < back_iso)
    all_codes = sorted((c for cs in by_teacher.values() for c in cs), key=_natural_key)
    msg = (f"{len(rows)} tablet{'s' if len(rows) != 1 else ''} signed in from "
           f"{', '.join(by_teacher)} by {admin}: {_code_preview(all_codes, 12)}.")
    if late:
        msg += f" {late} of them came back after the due time."
    flash(msg, 'warning' if late else 'success')
    return back


# ─────────────────────────────────────────────
# GADGET TIMETABLE RULES
# (pure functions: no database, no Flask — the single source of truth for
#  "may this student take the gadget out right now?")
# ─────────────────────────────────────────────

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
WD_SHORT = [d[:3] for d in WEEKDAYS]
MIN_SLOT_MINUTES = 5


def hhmm_to_min(value):
    """'09:30' -> 570. Raises ValueError for anything that is not a real time."""
    m = re.fullmatch(r'\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*', value or '')
    if not m:
        raise ValueError(value)
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError(value)
    return h * 60 + mi


def min_to_hhmm(n):
    return f'{n // 60:02d}:{n % 60:02d}'


def fmt12(value):
    """'14:05' -> '2:05 PM'."""
    h, m = divmod(hhmm_to_min(value), 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def slot_label(slot):
    return f"{fmt12(slot['start_time'])} – {fmt12(slot['end_time'])}"


def slot_title(slot):
    """Like slot_label, with the interval's name in front when it has one ("Prep · 7:00 PM – 9:00 PM")."""
    name = (slot.get('label') or '').strip()
    return f"{name} · {slot_label(slot)}" if name else slot_label(slot)


def clean_slot_label(raw):
    return re.sub(r'\s+', ' ', (raw or '')).strip()[:40]


def _by_start(slots):
    return sorted(slots, key=lambda s: (int(s['weekday']), hhmm_to_min(s['start_time'])))


def find_active_slot(slots, now_local):
    """The slot that is open right now (start <= now < end), or None."""
    wd = now_local.weekday()
    nm = now_local.hour * 60 + now_local.minute
    for s in _by_start(slots):
        if (int(s['weekday']) == wd
                and hhmm_to_min(s['start_time']) <= nm < hhmm_to_min(s['end_time'])):
            return s
    return None


def find_next_slot(slots, now_local):
    """Next slot that starts after 'now' -> (days_ahead, slot) or (None, None)."""
    wd = now_local.weekday()
    nm = now_local.hour * 60 + now_local.minute
    ordered = _by_start(slots)
    for ahead in range(0, 8):
        day = (wd + ahead) % 7
        for s in ordered:
            if int(s['weekday']) != day:
                continue
            if ahead == 0 and hhmm_to_min(s['start_time']) <= nm:
                continue
            return ahead, s
    return None, None


def describe_next(ahead, slot):
    if slot is None:
        return None
    day = WEEKDAYS[int(slot['weekday'])]
    when = ('today' if ahead == 0 else 'tomorrow' if ahead == 1
            else f'next {day}' if ahead == 7 else day)
    return f"{when}, {slot_label(slot)}"


def check_gadget_access(name, slots, consent_ok, is_out, now_local):
    """Decide whether this student may take their gadget out RIGHT NOW.

    There is deliberately NO admin override: outside the student's timetable
    the answer is always no. Returns a dict:
        ok (bool), code (str), message (str), slot (the open slot or None)
    """
    def deny(code, msg):
        return {'ok': False, 'code': code, 'message': msg, 'slot': None}

    if is_out:
        return deny('already_out', f'{name} already has a gadget signed out.')
    if not slots:
        return deny('no_timetable', f'{name} has no timetable yet. Create one first.')
    if not consent_ok:
        return deny('no_consent',
                    f"{name}'s timetable has not been agreed yet. Record the "
                    f"student's consent on the timetable page first.")

    active = find_active_slot(slots, now_local)
    if active:
        return {'ok': True, 'code': 'ok', 'slot': active,
                'message': f"Allowed — slot {slot_title(active)} today. "
                           f"Gadget must be back by {fmt12(active['end_time'])}."}

    wd = now_local.weekday()
    nm = now_local.hour * 60 + now_local.minute
    today = [s for s in _by_start(slots) if int(s['weekday']) == wd]
    ahead, nxt = find_next_slot(slots, now_local)
    tail = (f" Next slot: {describe_next(ahead, nxt)}." if nxt else '')
    later = [s for s in today if hhmm_to_min(s['start_time']) > nm]
    if not today:
        return deny('wrong_day', f"{name} has no gadget slot on {WEEKDAYS[wd]}." + tail)
    if later:
        return deny('not_yet',
                    f"Too early. {name}'s slot today starts at "
                    f"{fmt12(later[0]['start_time'])}." )
    return deny('slot_over', f"{name}'s slot today has ended." + tail)


def validate_slot(weekday, start, end, others):
    """Check one slot against the rules. `others` = the student's other slots.
    Returns (error_message or None, start_minutes, end_minutes)."""
    try:
        wd = int(weekday)
        if not 0 <= wd <= 6:
            raise ValueError
    except (TypeError, ValueError):
        return 'Choose a valid day.', 0, 0
    try:
        a, b = hhmm_to_min(start), hhmm_to_min(end)
    except ValueError:
        return 'Enter a valid start and end time.', 0, 0
    if b - a < MIN_SLOT_MINUTES:
        return (f'The end time must be at least {MIN_SLOT_MINUTES} minutes after '
                f'the start time on {WEEKDAYS[wd]}.'), a, b
    for o in others:
        if int(o['weekday']) != wd:
            continue
        oa, ob = hhmm_to_min(o['start_time']), hhmm_to_min(o['end_time'])
        if a < ob and oa < b:
            return (f"{WEEKDAYS[wd]} {fmt12(start)} – {fmt12(end)} overlaps another slot "
                    f"({slot_label(o)})."), a, b
    return None, a, b


# ─────────────────────────────────────────────
# TIMETABLE READER (PDF / typed text -> day + time intervals)
# Pure functions. Understands lines such as
#     Tuesday 11:30-3:20            (11:30 AM to 3:20 PM)
#     Tue prep 7-9                  (prep = evening, so 7 PM to 9 PM)
#     Mon - Fri  Prep 7:9           ("7:9" is read as 7 to 9)
#     Wednesday 8am-10am, 2pm-4pm
# or a day on its own line with the times underneath it. Whatever is read is
# only ever a SUGGESTION: the admin checks it in a preview before it is saved.
# ─────────────────────────────────────────────

_DAY_PAT = (r'(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?'
            r'|fri(?:day)?|sat(?:urday)?|sun(?:day)?)')
_DAY_RE = re.compile(r'(?<![a-z])(' + _DAY_PAT + r')(?![a-z])', re.I)
_DAY_SPAN_RE = re.compile(r'(?<![a-z])(' + _DAY_PAT + r')\s*(?:-|to|until|through)\s*('
                          + _DAY_PAT + r')(?![a-z])', re.I)
_DAY_INDEX = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

_T = r'(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?'
_RANGE_RE = re.compile(r'(?<![\d:.])' + _T + r'\s*(?:-|to|until)\s*' + _T + r'(?![\d:])', re.I)
_EVENING_RE = re.compile(r'prep|evening|night|study|homework|boarding|supper', re.I)
_LABEL_NOISE = {'from', 'at', 'and', 'to', 'on', 'every', 'each', 'the', 'time', 'times',
                'slot', 'slots', 'gadget', 'gadgets', 'allowed', '&'}
MAX_TIMETABLE_ROWS = 200


def _resolve_range(h1, m1, ap1, h2, m2, ap2, evening=False):
    """('11','30',None,'3','20',None) -> (690, 920): minutes since midnight, or None if nonsense.
    With no am/pm given: a start of 1-6 is the afternoon, 7-11 the morning (the evening
    when the interval is a prep/evening one), and the end is the first time after the start."""
    h1, h2, m1, m2 = int(h1), int(h2), int(m1 or 0), int(m2 or 0)
    ap1 = ap1[0].lower() if ap1 else None
    ap2 = ap2[0].lower() if ap2 else None
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    if (ap1 and not 1 <= h1 <= 12) or (ap2 and not 1 <= h2 <= 12):
        return None

    def with_meridiem(h, ap):
        return (h % 12) + (12 if ap == 'p' else 0)

    if ap2 and not ap1 and h1 <= 12:
        e = with_meridiem(h2, ap2)
        same = (h1 % 12) + (12 if e >= 12 else 0)
        other = (h1 % 12) + (0 if e >= 12 else 12)
        s = same if same * 60 + m1 < e * 60 + m2 else other
    else:
        if ap1:
            s = with_meridiem(h1, ap1)
        elif h1 > 12 or h1 == 0 or h1 == 12:
            s = h1
        elif 1 <= h1 <= 6 or (evening and 4 <= h1 <= 11):
            s = h1 + 12
        else:
            s = h1
        if ap2:
            e = with_meridiem(h2, ap2)
        elif h2 > 12:
            e = h2
        else:
            e = next((c for c in (h2 % 12, h2 % 12 + 12) if c * 60 + m2 > s * 60 + m1), h2)
    a, b = s * 60 + m1, e * 60 + m2
    return (a, b) if b > a else None


def _tt_label(raw):
    """Words next to a time range ("Prep", "Lessons") -> a short tidy label ('' if none)."""
    text = _DAY_RE.sub(' ', raw or '')
    words = [w for w in re.split(r'[\s,;:|/()]+', text) if w]
    while words and (words[0].lower() in _LABEL_NOISE or not re.search(r'[A-Za-z]', words[0])):
        words.pop(0)
    while words and (words[-1].lower() in _LABEL_NOISE or not re.search(r'[A-Za-z]', words[-1])):
        words.pop()
    label = ' '.join(words)
    if label.isupper() or label.islower():
        label = label.title()
    return clean_slot_label(label)


def _tt_days(line):
    """(sorted day numbers found in the line, the line with the day words blanked out)."""
    days = set()

    def span(m):
        a = _DAY_INDEX[m.group(1)[:3].lower()]
        b = _DAY_INDEX[m.group(2)[:3].lower()]
        days.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        return ' '

    rest = _DAY_SPAN_RE.sub(span, line)

    def one(m):
        days.add(_DAY_INDEX[m.group(1)[:3].lower()])
        return ' '

    rest = _DAY_RE.sub(one, rest)
    return sorted(days), rest


def parse_timetable_text(text):
    """Find day + time intervals in free text.
    Returns (rows, skipped): rows = [{'weekday', 'start', 'end', 'label'}] sorted by day/time;
    skipped = lines that had a time range but no day to attach it to."""
    rows, skipped, seen, current_days = [], [], set(), []
    for raw in (text or '').replace('\r', '\n').split('\n'):
        line = raw.replace('–', '-').replace('—', '-').replace('−', '-').replace(' ', ' ')
        line = re.sub(r'(?<![\d:.])(\d{1,2}):(\d)(?![\d:])', r'\1-\2', line)    # "7:9" -> "7-9"
        if not line.strip():
            continue
        days, rest = _tt_days(line)
        found = list(_RANGE_RE.finditer(rest))
        if days and not found:
            current_days = days                       # "Tuesday" on its own line: a heading
            continue
        if not found:
            continue
        use_days = days or current_days
        if not use_days:
            skipped.append(raw.strip()[:120])
            continue
        prev_end, labels = 0, []
        for m in found:
            labels.append(_tt_label(rest[prev_end:m.start()]))
            prev_end = m.end()
        if not labels[-1]:
            labels[-1] = _tt_label(rest[prev_end:])
        for m, label in zip(found, labels):
            g = m.groups()
            span = _resolve_range(g[0], g[1], g[2], g[3], g[4], g[5],
                                  evening=bool(_EVENING_RE.search(label)))
            if not span:
                skipped.append(raw.strip()[:120])
                continue
            for d in use_days:
                key = (d, span[0], span[1])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({'weekday': d, 'start': min_to_hhmm(span[0]),
                             'end': min_to_hhmm(span[1]), 'label': label})
    rows.sort(key=lambda r: (r['weekday'], r['start']))
    return rows, skipped


# ─────────────────────────────────────────────
# STUDENT LIST READER (PDF / text -> names + classes)
# Pure functions: used by both student registers' "Import" pages.
# ─────────────────────────────────────────────

_NUM_WORDS = r'(?:ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)'
CLASS_RE = re.compile(
    r"""^(?:
        (?:P|S|Y|G|F|PRI|SEC|SNR|YR|STD)\.?\s?-?\s?\d{1,2}\s?[A-Z]?
      | (?:PRIMARY|SENIOR|YEAR|GRADE|FORM|STANDARD|CLASS)\.?\s?-?\s?(?:\d{1,2}|""" + _NUM_WORDS + r""")\s?[A-Z]?
      | (?:BABY|MIDDLE|TOP|NURSERY|RECEPTION|KG\s?\d?|PRE-?\s?UNIT)(?:\s+CLASS)?
    )$""", re.I | re.X)

_GENDER = {'M', 'F', 'MALE', 'FEMALE', 'BOY', 'GIRL'}     # (B / G are stream letters, not gender)
_HEADER_WORDS = {'NO', 'NO.', 'S/N', 'SN', 'NAME', 'NAMES', 'FULL', 'STUDENT', 'STUDENTS',
                 'PUPIL', 'PUPILS', 'CLASS', 'STREAM', 'GENDER', 'SEX', 'AGE', 'ADM',
                 'ADMISSION', 'REG', 'NUMBER', 'SURNAME', 'OTHER', 'FIRST', 'LAST', 'DOB',
                 'DATE', 'BIRTH', 'OF', 'ID', 'TERM', 'YEAR', 'S.N', '#'}
_STOP_WORDS = {'ACADEMY', 'SCHOOL', 'INTERNATIONAL', 'TERM', 'LIST', 'REGISTER', 'REPORT',
               'TOTAL', 'TOTALS', 'PAGE', 'TEACHER', 'SIGNATURE', 'DATE', 'ROLL', 'ATTENDANCE',
               'STUDENTS', 'PUPILS', 'CLASS', 'NOMINAL', 'SHEET', 'GIRLS', 'BOYS', 'NAME',
               'NAMES', 'PRIMARY', 'SECONDARY', 'NURSERY', 'KINDERGARTEN', 'EMPOWER', 'HEADTEACHER',
               'DIRECTOR', 'PREPARED', 'APPROVED', 'COPY', 'PRINTED', 'YEAR', 'STREAM',
               'MR', 'MRS', 'MS', 'MISS', 'MADAM', 'SIR', 'PRINCIPAL', 'HEAD', 'DEPUTY',
               'BURSAR', 'PATRON', 'MATRON', 'MASTER', 'MISTRESS'}
_HEADING_RE = re.compile(r'\b(class|list|register|roll|pupils|students|term|stream|nominal)\b', re.I)


def _title_word(w):
    parts = re.split(r"([-'’])", w)
    return ''.join(p[:1].upper() + p[1:].lower() if p not in "-'’" else p for p in parts)


def clean_person_name(raw):
    """Tidy spacing; fix names typed in ALL CAPS or all lower case."""
    name = re.sub(r'\s+', ' ', (raw or '')).strip(' ,;:-._')
    if name and (name.isupper() or name.islower()):
        name = ' '.join(_title_word(w) for w in name.split(' '))
    return name[:80]


def clean_class_name(raw):
    cls = re.sub(r'\s+', ' ', (raw or '')).strip()
    return cls[:30].upper() if re.fullmatch(r'(?i)[psygf]\.?\s?-?\d{1,2}\s?[a-z]?', cls) else cls[:30]


def _find_class(tokens):
    """Does the END of this token list look like a class? -> (class_text, n_tokens)."""
    for k in (3, 2, 1):
        if len(tokens) >= k:
            cand = ' '.join(tokens[-k:])
            if CLASS_RE.fullmatch(cand):
                return cand, k
    return None, 0


def _is_noise(tok):
    t = tok.strip('.,;:')
    return (not t) or t.upper() in _GENDER or any(c.isdigit() for c in t)


def _valid_name(name):
    words = name.split()
    if len(words) < 2 or sum(ch.isalpha() for ch in name) < 4:
        return False
    if any(not (ch.isalpha() or ch in " .'’-") for ch in name):
        return False
    return not any(w.strip('.').upper() in _STOP_WORDS for w in words)


def _row_from_cells(cells):
    cls, name_parts = '', []
    for i, c in enumerate(cells):
        if i == 0:
            c = re.sub(r'^\s*\d+\s*[.):\-]?\s+', '', c)
        c = c.strip()
        if not c:
            continue
        if CLASS_RE.fullmatch(c):
            cls = cls or c
            continue
        part, found = _row_from_tokens(c)       # a cell may hold "Name Class" or just a number/gender
        if found and not cls:
            cls = found
        if part:
            name_parts.append(part)
    return ' '.join(name_parts), cls


def _row_from_tokens(line):
    line = re.sub(r'^\s*\d+\s*[.):\-]?\s+', '', line.strip())
    tokens = line.split()
    cls = ''
    for _ in range(6):                      # peel gender / class / numbers off the right-hand end
        if tokens and tokens[-1].strip('.,;:').upper() in _GENDER:
            tokens = tokens[:-1]
            continue
        found, k = _find_class(tokens)
        if found and not cls:
            cls = found
            tokens = tokens[:-k]
        elif tokens and _is_noise(tokens[-1]):
            tokens = tokens[:-1]
        else:
            break
    while tokens and _is_noise(tokens[0]):  # ... and the left-hand end
        tokens = tokens[1:]
    return ' '.join(tokens), cls


def parse_student_text(text, default_class=''):
    """Read lines of text into student rows.

    Understands: numbered lists, tables (columns split by tabs, '|' or wide gaps),
    a class column, class headings ("P.5 Class List"), gender/age/ID columns,
    page headers and footers. Returns (rows, skipped_lines):
        rows = [{'name': 'Mukasa Grace', 'cls': 'P.5'}, ...]
    """
    rows, skipped = [], []
    current = clean_class_name(default_class)
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r'[\W_]+', line):
            continue
        if re.match(r'(?i)^page\s*\d', line) or re.fullmatch(r'(?i)\d+\s*(?:/|of)\s*\d+', line):
            continue
        tokens = line.replace('|', ' ').split()
        up = [t.strip('.:,#').upper() for t in tokens]
        if up and all(t in _HEADER_WORDS or not t for t in up):          # "No. Name Class Sex"
            continue
        cells = [c for c in re.split(r'\t+|\s*\|\s*|\s{2,}', line) if c.strip()]
        name, cls = _row_from_cells(cells) if len(cells) > 1 else _row_from_tokens(line)
        name = clean_person_name(name)
        if _valid_name(name):
            rows.append({'name': name, 'cls': clean_class_name(cls) or current})
            continue

        # not a student row: maybe a heading such as "CLASS: P.5" or "Primary Five Class List"
        found = None
        if _HEADING_RE.search(line):
            for i in range(len(tokens)):
                for k in (3, 2, 1):
                    cand = ' '.join(tokens[i:i + k]).strip(':,-')
                    if len(tokens[i:i + k]) == k and CLASS_RE.fullmatch(cand):
                        found = cand
                        break
                if found:
                    break
        if found:
            current = clean_class_name(found)
        else:
            skipped.append(line)
    return rows, skipped


def extract_pdf_text(data, fallback='paste the names instead'):
    """All the text in a PDF (raises ValueError with a friendly message)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError('PDF reading is not installed on the server (add "pypdf" to requirements.txt).')
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt('')           # many PDFs are "encrypted" with an empty password
            except Exception:
                opened = 0
            if not opened:
                raise ValueError('This PDF is password-protected. Remove the password and try again.')
        pages = []
        for page in reader.pages:
            try:
                txt = page.extract_text(extraction_mode='layout')
            except Exception:
                txt = page.extract_text()
            pages.append(txt or '')
    except ValueError:
        raise
    except Exception:
        raise ValueError('That file could not be read as a PDF.')
    text = '\n'.join(pages)
    if not text.strip():
        raise ValueError('No text found in this PDF — it looks like a scanned picture. '
                         f'Use a PDF saved from Word/Excel, or {fallback}.')
    return text


# ─────────────────────────────────────────────
# STUDENT REGISTERS
#   /students          every student in the school (feeds the tablet sign-out search)
#   /gadgets/students  students who own a personal gadget (feeds the gadget desk)
# Both registers share the same add / remove / PDF-import code below.
# ─────────────────────────────────────────────

REGISTER_PAGE_SIZE = 50
IMPORT_BATCH = 150
MAX_IMPORT_ROWS = 3000

REGISTERS = {
    'students': {
        'table': 'students', 'title': 'School Students', 'noun': 'student',
        'icon': 'fa-user-graduate', 'gadgets': False,
        'page': 'students_register', 'add': 'students_add', 'remove': 'students_remove',
        'import': 'students_import', 'confirm': 'students_import_confirm',
        'promote': 'students_promote', 'promote_apply': 'students_promote_apply',
        'promote_undo': 'students_promote_undo', 'move': 'students_move',
        'log_add': 'student_add', 'log_remove': 'student_remove', 'log_import': 'student_import',
        'blurb': 'Every student in the school. On the tablet sign-out page an admin types a few '
                 'letters of a name and the full name and class fill in automatically.',
    },
    'gadget': {
        'table': 'laptop_students', 'title': 'Gadget Students', 'noun': 'student',
        'icon': 'fa-laptop', 'gadgets': True,
        'page': 'gadget_students_register', 'add': 'gadget_students_add',
        'remove': 'gadget_students_remove', 'import': 'gadget_students_import',
        'confirm': 'gadget_students_import_confirm',
        'promote': 'gadget_students_promote', 'promote_apply': 'gadget_students_promote_apply',
        'promote_undo': 'gadget_students_promote_undo', 'move': 'gadget_students_move',
        'log_add': 'laptop_student_add', 'log_remove': 'laptop_student_remove',
        'log_import': 'laptop_import',
        'blurb': 'Students who bring a personal gadget to school. Each one gets a timetable '
                 '(agreed with the student) and can only collect the gadget during those times.',
    },
}


# ─────────────────────────────────────────────
# CLASS PROMOTION RULES (pure functions)
#   next_class('P.5') -> {'label': 'P.6'}      moves up a year
#   next_class('S.6') -> {'leave': True}       final class: the student leaves the school
#   next_class('P.5 East') -> None             not recognised: the admin chooses
# ─────────────────────────────────────────────

_NUM_WORD_LIST = ['ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE',
                  'TEN', 'ELEVEN', 'TWELVE', 'THIRTEEN']

# prefix -> (highest class number, what the next stage is called or None = leaves school)
_PROMOTION_LADDERS = {
    'P': (7, 'S'), 'PRI': (7, 'SEC'), 'PRIMARY': (7, 'SENIOR'), 'STD': (7, None), 'STANDARD': (7, None),
    'S': (6, None), 'SEC': (6, None), 'SECONDARY': (6, None), 'SNR': (6, None), 'SENIOR': (6, None),
    'F': (6, None), 'FORM': (6, None),
    'Y': (13, None), 'YR': (13, None), 'YEAR': (13, None),
    'G': (12, None), 'GRADE': (12, None), 'CLASS': (12, None),
}
_SECONDARY_PREFIXES = {'S', 'SEC', 'SECONDARY', 'SNR', 'SENIOR', 'F', 'FORM'}
_CLASS_PARTS_RE = re.compile(
    r'(?i)^(P|PRI|PRIMARY|STD|STANDARD|S|SEC|SECONDARY|SNR|SENIOR|F|FORM|Y|YR|YEAR|G|GRADE|CLASS)'
    r'(\.?)(\s?-?\s?)(\d{1,2}|' + '|'.join(_NUM_WORD_LIST) + r')(\s?)([A-Z]?)$')


def _match_case(word, like):
    """Write `word` in the same style (UPPER / lower / Title) as `like`."""
    if like.isupper():
        return word.upper()
    if like.islower():
        return word.lower()
    return word.capitalize()


def is_secondary_class(label):
    m = _CLASS_PARTS_RE.match((label or '').strip())
    return bool(m and m.group(1).upper() in _SECONDARY_PREFIXES)


def next_class(label, has_secondary=True):
    """What class does a student in `label` move to next year?
    Returns {'label': 'P.6'}, {'leave': True} (final year) or None (unknown class name).
    `has_secondary`: does this school have secondary classes? If not, P.7 students leave."""
    text = re.sub(r'\s+', ' ', (label or '')).strip()
    if not text:
        return None

    # nursery stages
    m = re.fullmatch(r'(?i)(baby|middle|top)(\s+class)?', text)
    if m:
        stage = m.group(1).lower()
        if stage == 'top':
            return {'label': 'P.1'}
        nxt = 'Middle' if stage == 'baby' else 'Top'
        return {'label': _match_case(nxt, m.group(1)) + (m.group(2) or '')}
    m = re.fullmatch(r'(?i)(kg|nursery)\s?-?\s?(\d)', text)
    if m:
        n = int(m.group(2))
        if n >= 3:
            return {'label': 'P.1'}
        sep = re.search(r'\d', text)
        return {'label': text[:sep.start()] + str(n + 1)}
    if re.fullmatch(r'(?i)(reception|pre-?\s?unit)', text):
        return {'label': 'P.1'}

    m = _CLASS_PARTS_RE.match(text)
    if not m:
        return None
    prefix, dot, sep, num, gap, stream = m.groups()
    ladder = _PROMOTION_LADDERS[prefix.upper()]
    top, after = ladder
    word_form = not num.isdigit()
    n = (_NUM_WORD_LIST.index(num.upper()) + 1) if word_form else int(num)
    if n > top:
        return None
    if n == top:                                    # final class of this stage
        if after is None or (prefix.upper() in ('P', 'PRI', 'PRIMARY') and not has_secondary):
            return {'leave': True}
        new_prefix = _match_case(after, prefix)
        first = _match_case(_NUM_WORD_LIST[0], num) if word_form else '1'
        return {'label': f'{new_prefix}{dot}{sep}{first}'}    # stream letter is dropped on moving stage
    new_num = _match_case(_NUM_WORD_LIST[n], num) if word_form else str(n + 1)
    return {'label': f'{prefix}{dot}{sep}{new_num}{gap if stream else ""}{stream}'}


def school_has_secondary(class_labels):
    return any(is_secondary_class(c) for c in class_labels)


GADGET_TYPES = ('Laptop', 'Tablet')
GADGET_ICONS = {'Laptop': 'fa-laptop', 'Tablet': 'fa-tablet-alt'}


def clean_gadget_type(raw):
    """'laptop' / ' TABLET ' -> 'Laptop' / 'Tablet'; anything else -> '' (not chosen)."""
    value = (raw or '').strip().title()
    return value if value in GADGET_TYPES else ''


app.jinja_env.globals['gadget_types'] = GADGET_TYPES
app.jinja_env.globals['gadget_icons'] = GADGET_ICONS


def _natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', text or '')]


def _like_escape(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _register_find(conn, table, name, cls):
    return conn.execute(
        f"SELECT id, is_active FROM {table} "
        f"WHERE full_name = ? COLLATE NOCASE AND class_name = ? COLLATE NOCASE",
        (name, cls)).fetchone()


def _register_existing(conn, table):
    """{(name, class) lower-cased: (id, is_active)} for the whole register (one query)."""
    rows = conn.execute(f"SELECT id, full_name, class_name, is_active FROM {table}").fetchall()
    return {(r['full_name'].lower(), (r['class_name'] or '').lower()): (r['id'], r['is_active'])
            for r in rows}


def _register_add_one(conn, kind, name, cls, admin, model='', serial='', gtype='Laptop'):
    """-> 'added' | 'restored' | 'exists'"""
    table = REGISTERS[kind]['table']
    row = _register_find(conn, table, name, cls)
    if row and row['is_active']:
        return 'exists'
    if row:
        if kind == 'gadget':
            conn.execute("UPDATE laptop_students SET is_active=1, laptop_model=?, serial_no=?, "
                         "gadget_type=? WHERE id=?", (model, serial, gtype, row['id']))
        else:
            conn.execute("UPDATE students SET is_active=1 WHERE id=?", (row['id'],))
        return 'restored'
    if kind == 'gadget':
        conn.execute("INSERT INTO laptop_students (full_name, class_name, laptop_model, serial_no, "
                     "gadget_type, created_by) VALUES (?,?,?,?,?,?)",
                     (name, cls, model, serial, gtype, admin))
    else:
        conn.execute("INSERT INTO students (full_name, class_name, created_by) VALUES (?,?,?)",
                     (name, cls, admin))
    return 'added'


def _register_page(kind):
    cfg = REGISTERS[kind]
    table = cfg['table']
    q = (request.args.get('q') or '').strip()[:60]
    cls = (request.args.get('cls') or '').strip()[:30]
    gtype = clean_gadget_type(request.args.get('type')) if cfg['gadgets'] else ''
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    where, params = ["t.is_active=1"], []
    if gtype:
        where.append("t.gadget_type = ?")
        params.append(gtype)
    if q:
        where.append("t.full_name LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(q)}%")
    if cls:
        where.append("t.class_name = ? COLLATE NOCASE")
        params.append(cls)
    clause = " AND ".join(where)
    extra = (", (SELECT COUNT(*) FROM laptop_slots sl WHERE sl.student_id = t.id) AS slot_count, "
             "(SELECT COUNT(*) FROM laptop_sessions se WHERE se.student_id = t.id "
             "AND se.status='Out') AS is_out") if cfg['gadgets'] else ""

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM {table} t WHERE {clause}", params).fetchone()[0]
        pages = max(1, -(-total // REGISTER_PAGE_SIZE))
        page = min(page, pages)
        rows = conn.execute(
            f"SELECT t.*{extra} FROM {table} t WHERE {clause} "
            f"ORDER BY t.full_name COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [REGISTER_PAGE_SIZE, (page - 1) * REGISTER_PAGE_SIZE]).fetchall()
        class_rows = conn.execute(
            f"SELECT class_name, COUNT(*) AS c FROM {table} WHERE is_active=1 "
            f"GROUP BY class_name").fetchall()
        has_sec = _school_has_secondary(conn)
        last_change = _last_class_change(conn, kind)
        stats, type_counts = None, {}
        if cfg['gadgets']:
            type_counts = {r['gadget_type'] or 'Laptop': r['c'] for r in conn.execute(
                "SELECT gadget_type, COUNT(*) AS c FROM laptop_students WHERE is_active=1 "
                "GROUP BY gadget_type").fetchall()}
            stats = conn.execute("""
                SELECT
                  SUM(CASE WHEN (SELECT COUNT(*) FROM laptop_slots sl WHERE sl.student_id=t.id)=0
                           THEN 1 ELSE 0 END) AS no_timetable,
                  SUM(CASE WHEN (SELECT COUNT(*) FROM laptop_slots sl WHERE sl.student_id=t.id)>0
                            AND t.consent_at IS NULL THEN 1 ELSE 0 END) AS awaiting,
                  SUM(CASE WHEN (SELECT COUNT(*) FROM laptop_slots sl WHERE sl.student_id=t.id)>0
                            AND t.consent_at IS NOT NULL THEN 1 ELSE 0 END) AS ready
                FROM laptop_students t WHERE t.is_active=1""").fetchone()
    classes = sorted(((r['class_name'] or '(no class)', r['c']) for r in class_rows),
                     key=lambda x: _natural_key(x[0]))
    return render_template('registry.html', cfg=cfg, kind=kind, rows=rows, total=total,
                           grand_total=sum(c for _, c in classes), classes=classes,
                           q=q, cls=cls, page=page, pages=pages, stats=stats,
                           gtype=gtype, type_counts=type_counts, gadget_types=GADGET_TYPES,
                           gadget_icons=GADGET_ICONS, has_secondary=has_sec,
                           last_change=last_change)


def _register_add(kind):
    cfg = REGISTERS[kind]
    name = clean_person_name(request.form.get('name'))
    cls = clean_class_name(request.form.get('cls'))
    model = (request.form.get('laptop_model') or '').strip()[:60]
    serial = (request.form.get('serial_no') or '').strip()[:40]
    gtype = clean_gadget_type(request.form.get('gadget_type')) if cfg['gadgets'] else ''
    if len(name) < 3:
        flash("Enter the student's full name.", 'danger')
    elif not cls:
        flash('Enter the class (for example P.5).', 'danger')
    elif cfg['gadgets'] and not gtype:
        flash('Choose the type of gadget: Laptop or Tablet.', 'danger')
    else:
        admin = current_admin()
        with get_db() as conn:
            result = _register_add_one(conn, kind, name, cls, admin, model, serial, gtype or 'Laptop')
        if result == 'exists':
            flash(f'{name} ({cls}) is already in the register.', 'info')
        else:
            what = f' — {gtype}' if gtype else ''
            log_activity(cfg['log_add'], f'{"Restored" if result == "restored" else "Added"} '
                                         f'{name} ({cls}){what} in {cfg["title"]}')
            flash(f'{name} ({cls}){what} {"restored" if result == "restored" else "added"}.', 'success')
    return redirect(url_for(cfg['page']))


def _register_remove(kind):
    cfg = REGISTERS[kind]
    table = cfg['table']
    only = request.form.get('only')
    raw = [only] if only else request.form.getlist('ids')
    ids = [int(i) for i in raw if str(i).isdigit()][:500]
    back = redirect(url_for(cfg['page'], q=request.form.get('q', ''), cls=request.form.get('cls', '')))
    if not ids:
        flash('Tick at least one student to remove.', 'warning')
        return back
    marks = ','.join('?' * len(ids))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, full_name, class_name FROM {table} WHERE is_active=1 AND id IN ({marks})",
            ids).fetchall()
        blocked_ids = set()
        if cfg['gadgets'] and rows:
            blocked_ids = {r['student_id'] for r in conn.execute(
                f"SELECT DISTINCT student_id FROM laptop_sessions "
                f"WHERE status='Out' AND student_id IN ({marks})", ids).fetchall()}
        go = [r for r in rows if r['id'] not in blocked_ids]
        if go:
            m2 = ','.join('?' * len(go))
            conn.execute(f"UPDATE {table} SET is_active=0 WHERE id IN ({m2})", [r['id'] for r in go])
    if blocked_ids:
        names = ', '.join(r['full_name'] for r in rows if r['id'] in blocked_ids)
        flash(f'Not removed (gadget still signed out — sign it in first): {names}.', 'warning')
    if go:
        shown = ', '.join(f"{r['full_name']} ({r['class_name']})" for r in go[:5])
        more = f' and {len(go) - 5} more' if len(go) > 5 else ''
        log_activity(cfg['log_remove'], f'Removed {len(go)} from {cfg["title"]}: {shown}{more}')
        flash(f'Removed {len(go)} student{"s" if len(go) != 1 else ""}.', 'success')
    return back


def _register_import(kind):
    """Step 1: read the uploaded PDF (or pasted list) and show a preview. Nothing is saved yet."""
    cfg = REGISTERS[kind]
    default_cls = clean_class_name(request.form.get('default_class'))
    default_type = clean_gadget_type(request.form.get('gadget_type')) if cfg['gadgets'] else ''
    pasted = request.form.get('pasted') or ''
    upload = request.files.get('file')
    try:
        if cfg['gadgets'] and not default_type:
            raise ValueError('Choose the type of gadget for this list first: Laptop or Tablet '
                             '(you can change it for individual students in the preview).')
        if upload and upload.filename:
            data = upload.read()
            fname = upload.filename
            if fname.lower().endswith('.pdf') or data[:5] == b'%PDF-':
                text = extract_pdf_text(data)
            elif fname.lower().endswith(('.csv', '.txt')):
                text = data.decode('utf-8-sig', errors='replace')
            else:
                raise ValueError('Please upload a PDF file (a .csv or .txt list also works).')
            source = fname
        elif pasted.strip():
            text, source = pasted, 'pasted list'
        else:
            raise ValueError('Choose a PDF file, or paste a list of names.')
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for(cfg['page']))

    rows, skipped = parse_student_text(text, default_cls)
    if not rows:
        flash('No student names were found in that file. Check that it lists full names, '
              'or paste the names instead.', 'danger')
        return redirect(url_for(cfg['page']))
    truncated = len(rows) > MAX_IMPORT_ROWS
    rows = rows[:MAX_IMPORT_ROWS]

    with get_db() as conn:
        existing = _register_existing(conn, cfg['table'])
    seen, counts = set(), {'new': 0, 'restore': 0, 'exists': 0, 'dup': 0}
    for i, r in enumerate(rows):
        key = (r['name'].lower(), r['cls'].lower())
        if key in seen:
            r['status'] = 'dup'
        elif key in existing and existing[key][1]:
            r['status'] = 'exists'
        elif key in existing:
            r['status'] = 'restore'
        else:
            r['status'] = 'new'
        seen.add(key)
        counts[r['status']] += 1
        r['idx'] = i
    return render_template('registry_import.html', cfg=cfg, kind=kind, rows=rows,
                           skipped=skipped[:40], skipped_total=len(skipped), counts=counts,
                           source=source, truncated=truncated, default_type=default_type or 'Laptop',
                           gadget_types=GADGET_TYPES)


def _register_import_confirm(kind):
    """Step 2: save the rows the admin left ticked (after any edits in the preview)."""
    cfg = REGISTERS[kind]
    table = cfg['table']
    try:
        count = min(int(request.form.get('count', 0)), MAX_IMPORT_ROWS)
    except ValueError:
        count = 0
    keep = set(request.form.getlist('keep'))
    default_type = clean_gadget_type(request.form.get('default_type')) or 'Laptop'
    wanted, no_class = [], 0
    for i in range(count):
        if str(i) not in keep:
            continue
        name = clean_person_name(request.form.get(f'name_{i}'))
        cls = clean_class_name(request.form.get(f'cls_{i}'))
        if len(name) < 3:
            continue
        if not cls:
            no_class += 1
            continue
        wanted.append((name, cls, clean_gadget_type(request.form.get(f'type_{i}')) or default_type))

    admin = current_admin()
    added = restored = already = 0
    with get_db() as conn:
        existing = _register_existing(conn, table)
        inserts, restores, seen = [], [], set()
        for name, cls, gtype in wanted:
            key = (name.lower(), cls.lower())
            if key in seen:
                continue
            seen.add(key)
            if key in existing:
                if existing[key][1]:
                    already += 1
                else:
                    restores.append((existing[key][0], gtype))
            elif kind == 'gadget':
                inserts.append(("INSERT OR IGNORE INTO laptop_students (full_name, class_name, "
                                "gadget_type, created_by) VALUES (?,?,?,?)", (name, cls, gtype, admin)))
            else:
                inserts.append(("INSERT OR IGNORE INTO students (full_name, class_name, "
                                "created_by) VALUES (?,?,?)", (name, cls, admin)))
        if kind == 'gadget':
            restore_stmts = [("UPDATE laptop_students SET is_active=1, gadget_type=? WHERE id=?", (gt, rid))
                             for rid, gt in restores]
        else:
            restore_stmts = [(f"UPDATE {table} SET is_active=1 WHERE id=?", (rid,)) for rid, _ in restores]
        stmts = inserts + restore_stmts
        for i in range(0, len(stmts), IMPORT_BATCH):
            conn.run_batch(stmts[i:i + IMPORT_BATCH])
        added, restored = len(inserts), len(restores)

    if added or restored:
        log_activity(cfg['log_import'],
                     f'Imported into {cfg["title"]}: {added} added'
                     + (f', {restored} restored' if restored else '')
                     + (f', {already} already registered' if already else ''))
    parts = [f'{added} added']
    if restored:
        parts.append(f'{restored} restored')
    if already:
        parts.append(f'{already} already in the register')
    if no_class:
        parts.append(f'{no_class} skipped (no class)')
    flash('Import finished: ' + ', '.join(parts) + '.', 'success' if (added or restored) else 'info')
    return redirect(url_for(cfg['page']))


# ─────────────────────────────────────────────
# PROMOTING STUDENTS TO THE NEXT CLASS (both registers)
#   • "Promote classes": one page lists every class with the class it moves to;
#     the admin reviews it and presses ONE button to promote the whole school.
#   • "Promote" on a single student, "Promote selected", or "Move selected to class".
#   • Every change is recorded (class_changes) so the last one can be undone.
# Students in the final class leave the register (kept in the database, not deleted).
# ─────────────────────────────────────────────

PROMOTE_CHUNK = 300


def class_next_text(label, has_secondary=True):
    """'P.6' / 'leave' / '' (not recognised) — used by the register page buttons."""
    r = next_class(label, has_secondary)
    if r is None:
        return ''
    return 'leave' if r.get('leave') else r['label']


app.jinja_env.globals['class_next_text'] = class_next_text


def _school_has_secondary(conn):
    rows = conn.execute(
        "SELECT class_name FROM students WHERE is_active=1 "
        "UNION SELECT class_name FROM laptop_students WHERE is_active=1").fetchall()
    return school_has_secondary([r['class_name'] or '' for r in rows])


def _apply_class_moves(conn, kind, moves, admin, log=True):
    """Move / graduate students safely.

    moves: [{'id', 'name', 'old', 'new', 'leave'}]  (new = target class; leave = graduate)
    The register will not allow two students with the same name in the same class, so a
    move is only made when its target is free; moves that free up a spot are done first
    (in "waves"). Anything that cannot be done safely is skipped and reported — never forced.
    Returns {'moved', 'left', 'skipped': [(name, reason)], 'groups': {(old, new): n}, 'batch'}.
    """
    table = REGISTERS[kind]['table']
    skipped, groups = [], {}
    leaves = [m for m in moves if m.get('leave')]
    movers = [m for m in moves if not m.get('leave') and m.get('new')
              and m['new'].lower() != (m['old'] or '').lower()]

    if kind == 'gadget' and leaves:                     # a gadget that is out cannot leave the school
        marks = ','.join('?' * len(leaves))
        out = {r['student_id'] for r in conn.execute(
            f"SELECT DISTINCT student_id FROM laptop_sessions WHERE status='Out' "
            f"AND student_id IN ({marks})", [m['id'] for m in leaves]).fetchall()}
        for m in leaves:
            if m['id'] in out:
                skipped.append((m['name'], 'gadget is still signed out — sign it in first'))
        leaves = [m for m in leaves if m['id'] not in out]

    occupied = set(_register_existing(conn, table))
    waves, pending = [], movers
    while pending:
        wave, rest, claimed = [], [], set()
        for m in pending:
            key = (m['name'].lower(), m['new'].lower())
            if key in occupied or key in claimed:
                rest.append(m)
            else:
                wave.append(m)
                claimed.add(key)
        if not wave:
            break
        for m in wave:
            occupied.discard((m['name'].lower(), (m['old'] or '').lower()))
            occupied.add((m['name'].lower(), m['new'].lower()))
        waves.append(wave)
        pending = rest
    for m in pending:
        skipped.append((m['name'], f"a student with the same name is already in {m['new']} "
                                   f"(move them again after that student has moved)"))

    batch = secrets.token_hex(5)
    if log:                                             # record first, so a partial failure can still be undone
        now = datetime.utcnow().isoformat()
        rows = [(m['id'], m['name'], m['old'], m['new'], 1) for w in waves for m in w]
        rows += [(m['id'], m['name'], m['old'], m['old'], 0) for m in leaves]
        stmts = [("INSERT INTO class_changes (register, batch, student_id, student_name, old_class, "
                  "new_class, new_active, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (kind, batch, sid, nm, old, new, act, admin, now)) for sid, nm, old, new, act in rows]
        for i in range(0, len(stmts), IMPORT_BATCH):
            conn.run_batch(stmts[i:i + IMPORT_BATCH])

    for wave in waves:
        by_target = {}
        for m in wave:
            by_target.setdefault(m['new'], []).append(m['id'])
            groups[(m['old'], m['new'])] = groups.get((m['old'], m['new']), 0) + 1
        stmts = []
        for target, ids in by_target.items():
            for i in range(0, len(ids), PROMOTE_CHUNK):
                part = ids[i:i + PROMOTE_CHUNK]
                stmts.append((f"UPDATE {table} SET class_name=? WHERE id IN ({','.join('?' * len(part))})",
                              (target, *part)))
        for i in range(0, len(stmts), IMPORT_BATCH):
            conn.run_batch(stmts[i:i + IMPORT_BATCH])
    for wave in waves:
        for m in wave:
            _carry_suspension(conn, kind, m['name'], m['old'], m['new'])
    if leaves:
        ids = [m['id'] for m in leaves]
        stmts = [(f"UPDATE {table} SET is_active=0 WHERE id IN ({','.join('?' * len(ids[i:i + PROMOTE_CHUNK]))})",
                  tuple(ids[i:i + PROMOTE_CHUNK])) for i in range(0, len(ids), PROMOTE_CHUNK)]
        conn.run_batch(stmts)
        for m in leaves:
            groups[(m['old'], None)] = groups.get((m['old'], None), 0) + 1
    return {'moved': sum(len(w) for w in waves), 'left': len(leaves), 'skipped': skipped,
            'groups': groups, 'batch': batch}


def _describe_groups(groups):
    parts = [f"{old} → {new if new else 'left school'} ({n})"
             for (old, new), n in sorted(groups.items(), key=lambda kv: _natural_key(kv[0][0] or ''))]
    text = ', '.join(parts[:8])
    return text + (f' … and {len(parts) - 8} more groups' if len(parts) > 8 else '')


def _flash_class_result(res, verb='Promoted'):
    if res['moved'] or res['left']:
        bits = []
        if res['moved']:
            bits.append(f"{res['moved']} student{'s' if res['moved'] != 1 else ''} {verb.lower()}")
        if res['left']:
            bits.append(f"{res['left']} left the school")
        flash(' · '.join(bits) + '.  You can undo this from the Promote page.', 'success')
    if res['skipped']:
        names = '; '.join(f'{n}: {why}' for n, why in res['skipped'][:6])
        more = f' …and {len(res["skipped"]) - 6} more' if len(res['skipped']) > 6 else ''
        flash(f"Skipped {len(res['skipped'])} — {names}{more}.", 'warning')
    if not (res['moved'] or res['left'] or res['skipped']):
        flash('Nothing to change.', 'info')


def _last_class_change(conn, kind):
    last = conn.execute("SELECT batch FROM class_changes WHERE register=? AND undone=0 "
                        "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    if not last:
        return None
    info = conn.execute(
        "SELECT COUNT(*) AS n, MIN(changed_by) AS who, MIN(changed_at) AS at, "
        "SUM(CASE WHEN new_active=0 THEN 1 ELSE 0 END) AS gone "
        "FROM class_changes WHERE register=? AND batch=? AND undone=0",
        (kind, last['batch'])).fetchone()
    return {'batch': last['batch'], 'n': info['n'], 'who': info['who'] or 'an admin',
            'at': info['at'], 'gone': info['gone'] or 0}


def _promote_page(kind):
    cfg = REGISTERS[kind]
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT class_name, COUNT(*) AS c FROM {cfg['table']} WHERE is_active=1 "
            f"GROUP BY class_name").fetchall()
        has_sec = _school_has_secondary(conn)
        last = _last_class_change(conn, kind)
    plans = []
    for r in sorted(rows, key=lambda r: _natural_key(r['class_name'] or '')):
        label = r['class_name'] or ''
        nxt = next_class(label, has_sec) if label else None
        if nxt is None:
            action, target, note = 'keep', '', 'Not recognised — choose where they go, or leave them.'
        elif nxt.get('leave'):
            action, target, note = 'leave', '', 'Final class — leaves the school.'
        else:
            action, target, note = 'promote', nxt['label'], ''
        plans.append({'old': label, 'count': r['c'], 'action': action, 'target': target, 'note': note})
    return render_template('promote.html', cfg=cfg, kind=kind, plans=plans, last=last,
                           total=sum(p['count'] for p in plans))


def _promote_apply(kind):
    cfg = REGISTERS[kind]
    try:
        count = min(int(request.form.get('count', 0)), 300)
    except ValueError:
        count = 0
    plan = {}
    for i in range(count):
        old = (request.form.get(f'old_{i}') or '').strip()
        action = request.form.get(f'action_{i}', 'keep')
        target = clean_class_name(request.form.get(f'target_{i}'))
        if action == 'promote' and target:
            plan[old.lower()] = ('promote', target)
        elif action == 'leave':
            plan[old.lower()] = ('leave', '')
    if not plan:
        flash('Nothing was selected to promote.', 'info')
        return redirect(url_for(cfg['promote']))
    admin = current_admin()
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, full_name, class_name FROM {cfg['table']} "
                            f"WHERE is_active=1").fetchall()
        moves = []
        for r in rows:                                   # decided from each student's CURRENT class,
            act = plan.get((r['class_name'] or '').lower())    # so P.5->P.6 and P.6->P.7 never double-promote
            if act:
                moves.append({'id': r['id'], 'name': r['full_name'], 'old': r['class_name'] or '',
                              'new': act[1] or None, 'leave': act[0] == 'leave'})
        res = _apply_class_moves(conn, kind, moves, admin)
    if res['moved'] or res['left']:
        log_activity('class_promote', f"Promoted {cfg['title']}: {_describe_groups(res['groups'])}")
    _flash_class_result(res)
    return redirect(url_for(cfg['page']))


def _register_move(kind):
    """Promote / move ONE student, the ticked students, or move them to a chosen class."""
    cfg = REGISTERS[kind]
    only = request.form.get('only')
    raw = [only] if only else request.form.getlist('ids')
    ids = [int(i) for i in raw if str(i).isdigit()][:500]
    back = redirect(url_for(cfg['page'], q=request.form.get('q', ''), cls=request.form.get('cls', '')))
    if not ids:
        flash('Tick at least one student first.', 'warning')
        return back
    mode = 'to' if request.form.get('do') == 'to' else 'next'
    target = clean_class_name(request.form.get('target'))
    if mode == 'to' and not target:
        flash('Type the class to move them to, for example P.6.', 'danger')
        return back
    admin = current_admin()
    marks = ','.join('?' * len(ids))
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, full_name, class_name FROM {cfg['table']} "
                            f"WHERE is_active=1 AND id IN ({marks})", ids).fetchall()
        has_sec = _school_has_secondary(conn)
        moves, unknown = [], []
        for r in rows:
            old = r['class_name'] or ''
            if mode == 'to':
                moves.append({'id': r['id'], 'name': r['full_name'], 'old': old, 'new': target})
                continue
            nxt = next_class(old, has_sec)
            if nxt is None:
                unknown.append((r['full_name'], f"next class of '{old or 'no class'}' is not known — use “Move to class”"))
            elif nxt.get('leave'):
                moves.append({'id': r['id'], 'name': r['full_name'], 'old': old, 'new': None, 'leave': True})
            else:
                moves.append({'id': r['id'], 'name': r['full_name'], 'old': old, 'new': nxt['label']})
        res = _apply_class_moves(conn, kind, moves, admin)
    res['skipped'] = unknown + res['skipped']
    if res['moved'] or res['left']:
        log_activity('class_promote', f"{'Moved' if mode == 'to' else 'Promoted'} in {cfg['title']}: "
                                      f"{_describe_groups(res['groups'])}")
    _flash_class_result(res, 'moved' if mode == 'to' else 'promoted')
    return back


def _register_undo(kind):
    cfg = REGISTERS[kind]
    table = cfg['table']
    admin = current_admin()
    with get_db() as conn:
        last = _last_class_change(conn, kind)
        if not last or request.form.get('batch') != last['batch']:
            flash('There is nothing to undo (it may already have been undone).', 'info')
            return redirect(url_for(cfg['promote']))
        changes = conn.execute("SELECT * FROM class_changes WHERE register=? AND batch=? AND undone=0",
                               (kind, last['batch'])).fetchall()
        marks = ','.join('?' * len(changes))
        now_rows = {r['id']: r for r in conn.execute(
            f"SELECT id, full_name, class_name, is_active FROM {table} WHERE id IN ({marks})",
            [c['student_id'] for c in changes]).fetchall()}
        back_moves, reactivate, skipped = [], [], []
        for c in changes:
            cur = now_rows.get(c['student_id'])
            name = c['student_name'] or 'student'
            if not cur:
                skipped.append((name, 'no longer in the register'))
            elif (bool(cur['is_active']) != bool(c['new_active'])
                  or (cur['class_name'] or '').lower() != (c['new_class'] or '').lower()):
                skipped.append((name, 'was changed again since, so left as it is'))
            elif not c['new_active']:
                reactivate.append(c['student_id'])
            else:
                back_moves.append({'id': c['student_id'], 'name': cur['full_name'],
                                   'old': cur['class_name'] or '', 'new': c['old_class']})
        res = _apply_class_moves(conn, kind, back_moves, admin, log=False)
        for i in range(0, len(reactivate), PROMOTE_CHUNK):
            part = reactivate[i:i + PROMOTE_CHUNK]
            conn.execute(f"UPDATE {table} SET is_active=1 WHERE id IN ({','.join('?' * len(part))})", part)
        conn.execute("UPDATE class_changes SET undone=1 WHERE register=? AND batch=?", (kind, last['batch']))
    res['skipped'] = skipped + res['skipped']
    restored = res['moved'] + len(reactivate)
    log_activity('class_undo', f"Undid the last class change in {cfg['title']} ({restored} student"
                               f"{'s' if restored != 1 else ''} restored)")
    flash(f"Undone — {restored} student{'s' if restored != 1 else ''} put back where they were.", 'success')
    if res['skipped']:
        flash('Not restored: ' + '; '.join(f'{n} ({w})' for n, w in res['skipped'][:6]) + '.', 'warning')
    return redirect(url_for(cfg['promote']))


# — school students (tablet sign-out search) —
@app.route('/students')
@login_required
def students_register():
    return _register_page('students')


@app.route('/students/add', methods=['POST'])
@login_required
def students_add():
    return _register_add('students')


@app.route('/students/remove', methods=['POST'])
@login_required
def students_remove():
    return _register_remove('students')


@app.route('/students/import', methods=['POST'])
@login_required
def students_import():
    return _register_import('students')


@app.route('/students/import/confirm', methods=['POST'])
@login_required
def students_import_confirm():
    return _register_import_confirm('students')


# — students with personal gadgets —
@app.route('/gadgets/students')
@login_required
def gadget_students_register():
    return _register_page('gadget')


@app.route('/gadgets/students/add', methods=['POST'])
@login_required
def gadget_students_add():
    return _register_add('gadget')


@app.route('/gadgets/students/remove', methods=['POST'])
@login_required
def gadget_students_remove():
    return _register_remove('gadget')


@app.route('/gadgets/students/import', methods=['POST'])
@login_required
def gadget_students_import():
    return _register_import('gadget')


@app.route('/gadgets/students/import/confirm', methods=['POST'])
@login_required
def gadget_students_import_confirm():
    return _register_import_confirm('gadget')


@app.route('/students/promote')
@login_required
def students_promote():
    return _promote_page('students')


@app.route('/students/promote/apply', methods=['POST'])
@login_required
def students_promote_apply():
    return _promote_apply('students')


@app.route('/students/promote/undo', methods=['POST'])
@login_required
def students_promote_undo():
    return _register_undo('students')


@app.route('/students/move', methods=['POST'])
@login_required
def students_move():
    return _register_move('students')


@app.route('/gadgets/students/promote')
@login_required
def gadget_students_promote():
    return _promote_page('gadget')


@app.route('/gadgets/students/promote/apply', methods=['POST'])
@login_required
def gadget_students_promote_apply():
    return _promote_apply('gadget')


@app.route('/gadgets/students/promote/undo', methods=['POST'])
@login_required
def gadget_students_promote_undo():
    return _register_undo('gadget')


@app.route('/gadgets/students/move', methods=['POST'])
@login_required
def gadget_students_move():
    return _register_move('gadget')


@app.route('/laptops', defaults={'rest': ''}, methods=['GET', 'POST'])
@app.route('/laptops/<path:rest>', methods=['GET', 'POST'])
def _old_laptop_address(rest):
    """These pages used to live under /laptops (now /gadgets): keep old bookmarks working."""
    target = '/gadgets' + ('/' + rest if rest else '')
    if request.query_string:
        target += '?' + request.query_string.decode('utf-8', 'ignore')
    return redirect(target, code=308)


@app.errorhandler(413)
def _file_too_large(_e):
    flash('That file is too large (the limit is 15 MB).', 'danger')
    return redirect(request.referrer or url_for('dashboard'))


# ─────────────────────────────────────────────
# PERSONAL GADGETS
# Students may take their gadget out ONLY during their timetable. Even an admin
# cannot sign one out at any other time (see check_gadget_access above).
# ─────────────────────────────────────────────

def _gadget_slots(conn, student_id):
    return conn.execute(
        "SELECT * FROM laptop_slots WHERE student_id=? ORDER BY weekday, start_time",
        (student_id,)).fetchall()


def _gadget_student(conn, student_id):
    return conn.execute(
        "SELECT * FROM laptop_students WHERE id=? AND is_active=1", (student_id,)).fetchone()


def _gadget_is_out(conn, student_id):
    return conn.execute(
        "SELECT id FROM laptop_sessions WHERE student_id=? AND status='Out'",
        (student_id,)).fetchone() is not None


def _reset_consent(conn, student_id):
    """Any change to a timetable must be agreed by the student again."""
    conn.execute("UPDATE laptop_students SET consent_by=NULL, consent_at=NULL WHERE id=?",
                 (student_id,))


def _gadget_type(row):
    try:
        return row['gadget_type'] or 'Laptop'
    except (KeyError, IndexError):
        return 'Laptop'


def _gadget_device(row):
    """'Laptop · HP ProBook 450 · S/N 123' (type first, details only if known)."""
    parts = [_gadget_type(row), (row['laptop_model'] or '').strip()]
    if (row['serial_no'] or '').strip():
        parts.append('S/N ' + row['serial_no'].strip())
    return ' · '.join(p for p in parts if p)


def _gadget_session_payload(r):
    return {
        'id': r['id'],
        'student_id': r['student_id'],
        'student': r['full_name'],
        'class': r['class_name'] or '',
        'gadget': _gadget_device(r),
        'slot': (f"{fmt12(r['slot_start'])} – {fmt12(r['slot_end'])}"
                 if r['slot_start'] and r['slot_end'] else '—'),
        'out': fmt_dt(r['sign_out_time']),
        'by': r['signed_out_by'] or '',
        'expected': fmt_dt_time(r['expected_return_time']),
        'due_ms': _to_ms(r['expected_return_time']),
    }


_GADGET_SESSION_SELECT = """
    SELECT se.*, t.full_name, t.class_name, t.laptop_model, t.serial_no, t.gadget_type
    FROM laptop_sessions se JOIN laptop_students t ON t.id = se.student_id
"""


def _gadget_overdue_rows(conn):
    now = datetime.utcnow().isoformat()
    return conn.execute(
        _GADGET_SESSION_SELECT +
        " WHERE se.status='Out' AND se.expected_return_time < ? "
        "ORDER BY se.expected_return_time", (now,)).fetchall()


def _gadget_overdue_payload(rows):
    return [_gadget_session_payload(r) for r in rows]


@app.route('/gadgets')
@login_required
def gadget_desk():
    now_local = local_now()
    now_utc = datetime.utcnow().isoformat()
    with get_db() as conn:
        students = conn.execute(
            "SELECT * FROM laptop_students WHERE is_active=1 "
            "ORDER BY full_name COLLATE NOCASE").fetchall()
        slot_rows = conn.execute(
            "SELECT sl.* FROM laptop_slots sl JOIN laptop_students t ON t.id = sl.student_id "
            "WHERE t.is_active=1 ORDER BY sl.weekday, sl.start_time").fetchall()
        out_rows = conn.execute(
            _GADGET_SESSION_SELECT + " WHERE se.status='Out' ORDER BY se.expected_return_time"
        ).fetchall()
        recent = conn.execute(
            _GADGET_SESSION_SELECT + " WHERE se.status='Returned' "
            "ORDER BY se.sign_back_time DESC LIMIT 10").fetchall()
        banned_rows = conn.execute(
            "SELECT * FROM blacklist WHERE blocked_until > ? ORDER BY blocked_until",
            (now_utc,)).fetchall()
        suspended = suspension_map(conn)
    banned = {r['person_key']: r for r in banned_rows}

    slots_by = {}
    for s in slot_rows:
        slots_by.setdefault(s['student_id'], []).append(s)
    out_ids = {r['student_id'] for r in out_rows}

    people, allowed_now = [], []
    for st in students:
        slots = slots_by.get(st['id'], [])
        verdict = check_gadget_access(st['full_name'], slots, bool(st['consent_at']),
                                      st['id'] in out_ids, now_local)
        pkey = person_key(st['full_name'], st['class_name'])
        ban = banned.get(pkey)
        sus = suspended.get(pkey)
        people.append({
            'id': st['id'], 'n': st['full_name'], 'c': st['class_name'] or '',
            'gadget': _gadget_device(st), 'type': _gadget_type(st),
            'consent': bool(st['consent_at']),
            'out': st['id'] in out_ids,
            'blocked': suspension_short(sus) if sus else (blacklist_short(ban) if ban else ''),
            'suspended': bool(sus),
            'slots': [[int(s['weekday']), s['start_time'], s['end_time']] for s in slots],
        })
        if verdict['ok'] and not ban and not sus:
            allowed_now.append({'id': st['id'], 'n': st['full_name'], 'c': st['class_name'] or '',
                                'until': fmt12(verdict['slot']['end_time'])})

    out_payload = _gadget_session_payload
    out_list = [dict(out_payload(r), overdue=(r['expected_return_time'] < now_utc)) for r in out_rows]
    recent_list = []
    for r in recent:
        late = bool(r['sign_back_time'] and r['sign_back_time'] > r['expected_return_time'])
        recent_list.append({
            'student': r['full_name'], 'class': r['class_name'] or '',
            'slot': (f"{fmt12(r['slot_start'])} – {fmt12(r['slot_end'])}" if r['slot_start'] else '—'),
            'out': fmt_dt(r['sign_out_time']), 'back': fmt_dt(r['sign_back_time']),
            'late': late, 'by': r['signed_out_by'] or '', 'back_by': r['signed_back_by'] or ''})

    pre = request.args.get('student', '')
    return render_template(
        'gadget_desk.html', people=people, out_list=out_list, recent=recent_list,
        allowed_now=allowed_now, overdue_n=sum(1 for o in out_list if o['overdue']),
        preselect=int(pre) if pre.isdigit() else None, admin=current_admin(),
        server_now_ms=int(_time.time() * 1000), tz_name=APP_TZ_NAME)


@app.route('/gadgets/signout', methods=['POST'])
@login_required
def gadget_signout():
    admin = current_admin()
    sid = request.form.get('student_id', '')
    back = redirect(url_for('gadget_desk', student=sid if sid.isdigit() else None))
    if not sid.isdigit():
        flash('Choose a student first.', 'danger')
        return back
    now_local = local_now()
    with get_db() as conn:
        st = _gadget_student(conn, int(sid))
        if not st:
            flash('That student is not in the gadget register.', 'danger')
            return redirect(url_for('gadget_desk'))
        barrier = signout_barrier(conn, st['full_name'], st['class_name'])
        if barrier:
            log_activity('laptop_blocked',
                         f"Blocked: {_who(st['full_name'], st['class_name'])} — "
                         f"{barrier['title'].split(': ', 1)[-1]}")
            again = (f" They can sign out again on {fmt_dt_full(barrier['until'])}."
                     if barrier.get('until') else '')
            flash('Sign-out denied. ' + barrier['message'] + again, 'danger')
            return back
        verdict = check_gadget_access(
            st['full_name'], _gadget_slots(conn, st['id']), bool(st['consent_at']),
            _gadget_is_out(conn, st['id']), now_local)
        if not verdict['ok']:
            log_activity('laptop_blocked',
                         f"Blocked: {st['full_name']} ({st['class_name']}) — {verdict['message']}")
            flash('Not allowed. ' + verdict['message'], 'danger')
            return back
        slot = verdict['slot']
        end_h, end_m = divmod(hhmm_to_min(slot['end_time']), 60)
        due_local = now_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        # the NOT EXISTS makes a double click / two admins at once safe
        res = conn.execute("""
            INSERT INTO laptop_sessions (student_id, slot_start, slot_end, sign_out_time,
                                         expected_return_time, signed_out_by, status)
            SELECT ?,?,?,?,?,?,'Out'
            WHERE NOT EXISTS (SELECT 1 FROM laptop_sessions WHERE student_id=? AND status='Out')
        """, (st['id'], slot['start_time'], slot['end_time'], to_utc_iso(now_local),
              to_utc_iso(due_local), admin, st['id']))
        if not res.rowcount:
            flash(f"{st['full_name']} already has a gadget signed out.", 'info')
            return back
    due = fmt12(slot['end_time'])
    log_activity('laptop_signout', f"Signed out {_gadget_type(st).lower()} to {st['full_name']} "
                                   f"({st['class_name']}), due back at {due}")
    flash(f"{_gadget_type(st)} signed out to {st['full_name']}. Slot {slot_title(slot)} — due back by {due}. "
          f"Approved by {admin}.", 'success')
    return redirect(url_for('gadget_desk'))


@app.route('/gadgets/return/<int:session_id>', methods=['POST'])
@login_required
def gadget_return(session_id):
    admin = current_admin()
    back = redirect(url_for('gadget_overdue_page') if request.form.get('next') == 'overdue'
                    else url_for('gadget_desk'))
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        se = conn.execute(
            "SELECT se.status, se.expected_return_time, t.full_name, t.class_name, "
            "t.gadget_type, t.laptop_model, t.serial_no "
            "FROM laptop_sessions se JOIN laptop_students t ON t.id = se.student_id "
            "WHERE se.id=?", (session_id,)).fetchone()
        if not se:
            flash('Gadget session not found.', 'danger')
            return back
        if se['status'] != 'Out':
            flash(f"{se['full_name']}'s {_gadget_type(se).lower()} was already signed in.", 'info')
            return back
        conn.execute("UPDATE laptop_sessions SET status='Returned', sign_back_time=?, "
                     "signed_back_by=? WHERE id=? AND status='Out'", (now, admin, session_id))
        # more than 20 minutes late -> blacklist (4 days without sign-outs)
        banned = record_late_return(
            conn, kind='Gadget', source='gadget', source_id=session_id,
            name=se['full_name'], cls=se['class_name'], device=_gadget_device(se),
            due_iso=se['expected_return_time'], back_iso=now, admin=admin)
    late_min = int((datetime.fromisoformat(now) -
                    datetime.fromisoformat(se['expected_return_time'])).total_seconds() // 60)
    note = 'on time' if late_min <= 0 else f'{late_min} min late'
    log_activity('laptop_return', f"Signed in {_gadget_type(se).lower()} from {se['full_name']} "
                                  f"({se['class_name']}) — {note}")
    if banned:
        flash(f"{se['full_name']}'s {_gadget_type(se).lower()} signed in by {admin} ({note}). "
              f"More than {LATE_GRACE_MINUTES} minutes late: {se['full_name']} has been added to the "
              f"blacklist and cannot sign anything out until {fmt_dt_full(banned['until'])}.",
              'warning')
    else:
        flash(f"{se['full_name']}'s {_gadget_type(se).lower()} signed in by {admin} ({note}).",
              'success' if late_min <= 0 else 'warning')
    return back


@app.route('/gadgets/overdue')
@login_required
def gadget_overdue_page():
    with get_db() as conn:
        rows = _gadget_overdue_rows(conn)
    return render_template('gadget_overdue.html', overdue=_gadget_overdue_payload(rows),
                           server_now_ms=int(_time.time() * 1000))


# — timetable editor (admin-created, with the student's consent) —

def _timetable_back(sid):
    return redirect(url_for('gadget_timetable', sid=sid))


@app.route('/gadgets/students/<int:sid>/timetable')
@login_required
def gadget_timetable(sid):
    now_local = local_now()
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        slots = _gadget_slots(conn, sid)
        is_out = _gadget_is_out(conn, sid)
        pdf = _timetable_pdf_info(conn, sid)
    verdict = check_gadget_access(st['full_name'], slots, bool(st['consent_at']), is_out, now_local)
    days = [[] for _ in range(7)]
    total_min = 0
    for s in slots:
        days[int(s['weekday'])].append(s)
        total_min += hhmm_to_min(s['end_time']) - hhmm_to_min(s['start_time'])
    return render_template('gadget_timetable.html', st=st, slots=slots, days=days, verdict=verdict,
                           total_hours=round(total_min / 60, 1), today_wd=now_local.weekday(),
                           consent_at=st['consent_at'], is_out=is_out, pdf=pdf)


@app.route('/gadgets/students/<int:sid>/timetable/add', methods=['POST'])
@login_required
def gadget_timetable_add(sid):
    days = sorted({d for d in request.form.getlist('days') if d.isdigit()})
    start = (request.form.get('start') or '').strip()
    end = (request.form.get('end') or '').strip()
    label = clean_slot_label(request.form.get('label'))
    if not days:
        flash('Tick at least one day.', 'danger')
        return _timetable_back(sid)
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        existing = list(_gadget_slots(conn, sid))
        planned, errors = [], []
        for d in days:
            err, a, b = validate_slot(d, start, end, existing + planned)
            if err:
                errors.append(err)
            else:
                planned.append({'weekday': int(d), 'start_time': min_to_hhmm(a),
                                'end_time': min_to_hhmm(b), 'label': label})
        if errors:
            for e in errors[:3]:
                flash(e, 'danger')
            flash('Nothing was saved. Fix the problem above and try again.', 'warning')
            return _timetable_back(sid)
        conn.run_batch([("INSERT INTO laptop_slots (student_id, weekday, start_time, end_time, label) "
                         "VALUES (?,?,?,?,?)",
                         (sid, p['weekday'], p['start_time'], p['end_time'], p['label']))
                        for p in planned])
        _reset_consent(conn, sid)
    names = ', '.join(WD_SHORT[p['weekday']] for p in planned)
    log_activity('timetable_edit', f"Added slot {fmt12(start)}–{fmt12(end)}"
                                   f"{' ' + label if label else ''} ({names}) to "
                                   f"{st['full_name']}'s timetable")
    flash(f'Added {fmt12(start)} – {fmt12(end)} on {names}. Record the student\'s consent '
          f'again before gadgets can be signed out.', 'success')
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/timetable/slot/<int:slot_id>', methods=['POST'])
@login_required
def gadget_timetable_edit(sid, slot_id):
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        slots = list(_gadget_slots(conn, sid))
        mine = next((s for s in slots if s['id'] == slot_id), None)
        if not st or not mine:
            flash('Slot not found.', 'danger')
            return _timetable_back(sid)
        wd = request.form.get('weekday')
        start = (request.form.get('start') or '').strip()
        end = (request.form.get('end') or '').strip()
        err, a, b = validate_slot(wd, start, end, [s for s in slots if s['id'] != slot_id])
        if err:
            flash(err, 'danger')
            return _timetable_back(sid)
        conn.execute("UPDATE laptop_slots SET weekday=?, start_time=?, end_time=?, label=? WHERE id=?",
                     (int(wd), min_to_hhmm(a), min_to_hhmm(b),
                      clean_slot_label(request.form.get('label')), slot_id))
        _reset_consent(conn, sid)
    log_activity('timetable_edit',
                 f"Changed {st['full_name']}'s slot {WD_SHORT[int(mine['weekday'])]} "
                 f"{slot_label(mine)} → {WD_SHORT[int(wd)]} {fmt12(start)} – {fmt12(end)}")
    flash('Slot updated. Record the student\'s consent again before gadgets can be signed out.',
          'success')
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/timetable/slot/<int:slot_id>/delete', methods=['POST'])
@login_required
def gadget_timetable_delete(sid, slot_id):
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        mine = next((s for s in _gadget_slots(conn, sid) if s['id'] == slot_id), None)
        if not st or not mine:
            flash('Slot not found.', 'danger')
            return _timetable_back(sid)
        conn.execute("DELETE FROM laptop_slots WHERE id=? AND student_id=?", (slot_id, sid))
        _reset_consent(conn, sid)
    log_activity('timetable_edit', f"Removed {WD_SHORT[int(mine['weekday'])]} {slot_label(mine)} "
                                   f"from {st['full_name']}'s timetable")
    flash('Slot removed. Record the student\'s consent again before gadgets can be signed out.',
          'success')
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/timetable/consent', methods=['POST'])
@login_required
def gadget_timetable_consent(sid):
    admin = current_admin()
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        if not _gadget_slots(conn, sid):
            flash('Add at least one slot before recording consent.', 'danger')
            return _timetable_back(sid)
        if 'agree' not in request.form:
            flash("Tick the box to confirm the student agreed to this timetable.", 'danger')
            return _timetable_back(sid)
        conn.execute("UPDATE laptop_students SET consent_by=?, consent_at=? WHERE id=?",
                     (admin, datetime.utcnow().isoformat(), sid))
    log_activity('timetable_consent', f"Recorded {st['full_name']}'s agreement to their gadget timetable")
    flash(f"Consent recorded for {st['full_name']}. Their gadget can now be signed out during the slots.",
          'success')
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/edit', methods=['POST'])
@login_required
def gadget_student_edit(sid):
    name = clean_person_name(request.form.get('name'))
    cls = clean_class_name(request.form.get('cls'))
    model = (request.form.get('laptop_model') or '').strip()[:60]
    serial = (request.form.get('serial_no') or '').strip()[:40]
    gtype = clean_gadget_type(request.form.get('gadget_type'))
    if len(name) < 3 or not cls:
        flash('Enter the full name and the class.', 'danger')
        return _timetable_back(sid)
    if not gtype:
        flash('Choose the type of gadget: Laptop or Tablet.', 'danger')
        return _timetable_back(sid)
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        clash = conn.execute(
            "SELECT id FROM laptop_students WHERE full_name = ? COLLATE NOCASE "
            "AND class_name = ? COLLATE NOCASE AND id <> ?", (name, cls, sid)).fetchone()
        if clash:
            flash('Another student with that name and class is already registered.', 'danger')
            return _timetable_back(sid)
        conn.execute("UPDATE laptop_students SET full_name=?, class_name=?, laptop_model=?, "
                     "serial_no=?, gadget_type=? WHERE id=?", (name, cls, model, serial, gtype, sid))
    log_activity('laptop_student_edit', f'Updated details of {name} ({cls}) — {gtype}')
    flash('Details saved.', 'success')
    return _timetable_back(sid)


# — the student's timetable PDF, and reading intervals from it —
# The PDF is kept in the database (the web host's disk is wiped on every deploy).
# Reading intervals out of it is only a shortcut: the admin always checks a preview
# and can edit every row before anything is saved, and the student's consent is
# asked for again afterwards like after any other timetable change.

TIMETABLE_PDF_MAX = 3 * 1024 * 1024          # a timetable PDF is normally well under 1 MB


def _timetable_pdf_info(conn, sid):
    """Details of the saved timetable PDF (never the file itself), or None."""
    return conn.execute(
        "SELECT student_id, filename, size, uploaded_by, uploaded_at "
        "FROM laptop_timetable_files WHERE student_id=?", (sid,)).fetchone()


def _tt_preview(st, rows, skipped, source, existing, replace=False):
    return render_template('gadget_timetable_read.html', st=st, rows=rows, skipped=skipped[:30],
                           skipped_total=len(skipped), source=source, existing=existing,
                           replace=replace, weekdays=WEEKDAYS)


def _tt_preview_from_text(sid, st, text, source):
    """Step 1: find the intervals in `text` and show them for checking. Nothing is saved."""
    found, skipped = parse_timetable_text(text)
    if not found:
        flash(f'No days and times were found in {source}. Type the intervals in the box instead, '
              f'one per line — for example "Tue 11:30-3:20" and "Tue prep 7-9".', 'warning')
        return _timetable_back(sid)
    found = found[:MAX_TIMETABLE_ROWS]
    with get_db() as conn:
        existing = list(_gadget_slots(conn, sid))
    rows, ok = [], []
    for i, r in enumerate(found):
        row = {'idx': i, 'keep': True, 'weekday': r['weekday'], 'start': r['start'],
               'end': r['end'], 'label': r['label'], 'problem': '', 'clash': False}
        err, a, b = validate_slot(r['weekday'], r['start'], r['end'], ok)
        row['problem'] = err or ''
        if not err:
            ok.append({'weekday': r['weekday'], 'start_time': r['start'], 'end_time': r['end']})
            row['clash'] = bool(validate_slot(r['weekday'], r['start'], r['end'], existing)[0])
        else:
            row['keep'] = False
        rows.append(row)
    return _tt_preview(st, rows, skipped, source, existing)


def _tt_rows_from_form():
    try:
        count = min(int(request.form.get('count', 0)), MAX_TIMETABLE_ROWS)
    except ValueError:
        count = 0
    keep = set(request.form.getlist('keep'))
    return [{'idx': i, 'keep': str(i) in keep,
             'weekday': request.form.get(f'day_{i}', ''),
             'start': (request.form.get(f'start_{i}') or '').strip(),
             'end': (request.form.get(f'end_{i}') or '').strip(),
             'label': clean_slot_label(request.form.get(f'label_{i}')),
             'problem': '', 'clash': False} for i in range(count)]


@app.route('/gadgets/students/<int:sid>/timetable/pdf')
@login_required
def gadget_timetable_pdf(sid):
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        row = conn.execute("SELECT filename, data FROM laptop_timetable_files WHERE student_id=?",
                           (sid,)).fetchone() if st else None
    if not row:
        flash('No timetable PDF is attached for this student.', 'warning')
        return _timetable_back(sid)
    resp = make_response(bytes(row['data']))
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename="{secure_filename(row["filename"]) or "timetable.pdf"}"'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


@app.route('/gadgets/students/<int:sid>/timetable/pdf', methods=['POST'])
@login_required
def gadget_timetable_pdf_upload(sid):
    upload = request.files.get('file')
    if not upload or not upload.filename:
        flash("Choose the student's timetable PDF first.", 'danger')
        return _timetable_back(sid)
    data = upload.read()
    if data[:5] != b'%PDF-':
        flash('That file is not a PDF. Save the timetable as a PDF and try again.', 'danger')
        return _timetable_back(sid)
    if len(data) > TIMETABLE_PDF_MAX:
        flash(f'That PDF is too large ({len(data) / 1048576:.1f} MB). Please keep it under '
              f'{TIMETABLE_PDF_MAX // 1048576} MB.', 'danger')
        return _timetable_back(sid)
    name = secure_filename(upload.filename) or 'timetable.pdf'
    if not name.lower().endswith('.pdf'):
        name += '.pdf'
    name = name[-100:]
    admin = current_admin()
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        conn.execute("""
            INSERT INTO laptop_timetable_files (student_id, filename, size, data, uploaded_by, uploaded_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(student_id) DO UPDATE SET filename=excluded.filename, size=excluded.size,
                data=excluded.data, uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at
        """, (sid, name, len(data), data, admin, datetime.utcnow().isoformat()))
    log_activity('timetable_pdf', f"Attached timetable PDF ({name}) to {st['full_name']} ({st['class_name']})")
    flash(f"Timetable PDF attached to {st['full_name']}.", 'success')
    if 'read' in request.form:
        try:
            text = extract_pdf_text(data, fallback='type the intervals in the box instead')
        except ValueError as e:
            flash(str(e), 'warning')
            return _timetable_back(sid)
        return _tt_preview_from_text(sid, st, text, name)
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/timetable/pdf/delete', methods=['POST'])
@login_required
def gadget_timetable_pdf_delete(sid):
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        conn.execute("DELETE FROM laptop_timetable_files WHERE student_id=?", (sid,))
    log_activity('timetable_pdf', f"Removed the timetable PDF of {st['full_name']} ({st['class_name']})")
    flash('Timetable PDF removed. The intervals already saved are unchanged.', 'success')
    return _timetable_back(sid)


@app.route('/gadgets/students/<int:sid>/timetable/read', methods=['POST'])
@login_required
def gadget_timetable_read(sid):
    pasted = (request.form.get('pasted') or '').strip()
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        saved = None if pasted else conn.execute(
            "SELECT filename, data FROM laptop_timetable_files WHERE student_id=?", (sid,)).fetchone()
    if pasted:
        text, source = pasted[:20000], 'what you typed'
    elif saved:
        try:
            text = extract_pdf_text(bytes(saved['data']),
                                    fallback='type the intervals in the box instead')
        except ValueError as e:
            flash(str(e), 'danger')
            return _timetable_back(sid)
        source = saved['filename']
    else:
        flash('Attach the timetable PDF first, or type the intervals in the box.', 'danger')
        return _timetable_back(sid)
    return _tt_preview_from_text(sid, st, text, source)


@app.route('/gadgets/students/<int:sid>/timetable/read/apply', methods=['POST'])
@login_required
def gadget_timetable_read_apply(sid):
    """Step 2: save the intervals the admin left ticked (after any edits in the preview)."""
    rows = _tt_rows_from_form()
    replace = 'replace' in request.form
    if not rows:
        return _timetable_back(sid)
    with get_db() as conn:
        st = _gadget_student(conn, sid)
        if not st:
            flash('Student not found.', 'danger')
            return redirect(url_for('gadget_students_register'))
        existing = list(_gadget_slots(conn, sid))
        planned, bad = [], 0
        for r in rows:
            if not r['keep']:
                continue
            err, a, b = validate_slot(r['weekday'], r['start'], r['end'],
                                      ([] if replace else existing) + planned)
            r['problem'] = err or ''
            if err:
                bad += 1
            else:
                planned.append({'weekday': int(r['weekday']), 'start_time': min_to_hhmm(a),
                                'end_time': min_to_hhmm(b), 'label': r['label']})
        if not planned and not bad:
            flash('Tick at least one interval to save.', 'danger')
            return _tt_preview(st, rows, [], 'your selection', existing, replace)
        if bad:
            flash('Nothing was saved. Fix the rows marked in red (or untick them) and try again.',
                  'danger')
            return _tt_preview(st, rows, [], 'your selection', existing, replace)
        stmts = [("DELETE FROM laptop_slots WHERE student_id=?", (sid,))] if replace else []
        stmts += [("INSERT INTO laptop_slots (student_id, weekday, start_time, end_time, label) "
                   "VALUES (?,?,?,?,?)",
                   (sid, p['weekday'], p['start_time'], p['end_time'], p['label']))
                  for p in planned]
        conn.run_batch(stmts)
        _reset_consent(conn, sid)
    log_activity('timetable_edit',
                 f"{'Replaced the timetable of' if replace else 'Added intervals to'} "
                 f"{st['full_name']} ({st['class_name']}): {len(planned)} interval"
                 f"{'' if len(planned) == 1 else 's'} from the timetable reader")
    flash(f"{len(planned)} interval{'' if len(planned) == 1 else 's'} saved. Record the student's "
          f"consent again before gadgets can be signed out.", 'success')
    return _timetable_back(sid)


# ─────────────────────────────────────────────
# TEAM ACTIVITY (every admin sees what every other admin did)
# ─────────────────────────────────────────────

ACTIVITY_PER_PAGE = 40


@app.route('/activity')
@login_required
def activity():
    who = (request.args.get('admin') or '').strip()
    what = (request.args.get('action') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    where, params = [], []
    if who:
        where.append("admin = ?")
        params.append(who)
    if what in ACTION_META:
        where.append("action = ?")
        params.append(what)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    midnight = datetime.combine(local_today(), datetime.min.time(), tzinfo=APP_TZ)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM activity_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [ACTIVITY_PER_PAGE + 1, (page - 1) * ACTIVITY_PER_PAGE]).fetchall()
        if request.args.get('partial'):
            return render_template('_activity_rows.html', rows=rows[:ACTIVITY_PER_PAGE])
        names = {r['username'] for r in conn.execute(
            "SELECT username FROM admins WHERE is_active=1").fetchall()}
        names |= {r['admin'] for r in conn.execute(
            "SELECT DISTINCT admin FROM activity_log").fetchall()}
        today_counts = conn.execute(
            "SELECT admin, COUNT(*) AS c FROM activity_log WHERE created_at >= ? "
            "AND action NOT IN ('login','logout') GROUP BY admin ORDER BY c DESC",
            (to_utc_iso(midnight),)).fetchall()

    return render_template('activity.html',
                           rows=rows[:ACTIVITY_PER_PAGE],
                           has_more=len(rows) > ACTIVITY_PER_PAGE, page=page,
                           admins=sorted(names), f_admin=who, f_action=what,
                           today_counts=today_counts)


# ─────────────────────────────────────────────
# API: Overdue JSON
# ─────────────────────────────────────────────

def _api_admin_ok():
    """The JSON APIs contain student names: logged-in admins only."""
    return bool(session.get('logged_in') and session.get('admin'))


def _overdue_rows(conn):
    now = datetime.utcnow().isoformat()
    return conn.execute("""
        SELECT tt.id, tt.student_name, tt.student_class, tt.expected_return_time,
               tt.signed_out_by, tt.quantity, tt.sign_out_time,
               tt.teacher_id, tt.checkout_id,
               t.tablet_id AS tab_code
        FROM tablet_transactions tt
        JOIN tablets t ON tt.tablet_id = t.id
        WHERE tt.status='Borrowed' AND tt.expected_return_time < ?
        ORDER BY tt.expected_return_time
    """, (now,)).fetchall()


def _overdue_payload(rows):
    return [{
        'id': r['id'],
        'tablet': r['tab_code'],
        'student': r['student_name'],
        'class': r['student_class'] or '',
        'by': r['signed_out_by'] or '',
        'expected': fmt_dt_time(r['expected_return_time']),
        'out': fmt_dt(r['sign_out_time']),
        'qty': r['quantity'] or 1,
        'due_ms': _to_ms(r['expected_return_time']),
        'teacher': bool(r['teacher_id']),
        'checkout': r['checkout_id'] or '',
    } for r in rows]


def _to_ms(value):
    """Stored UTC time -> milliseconds since 1970 (None if unreadable)."""
    try:
        return int(to_local(value).timestamp() * 1000)
    except Exception:
        return None


@app.route('/api/overdue')
def api_overdue():
    if not _api_admin_ok():
        return jsonify({'error': 'login required'}), 401
    with get_db() as conn:
        rows = _overdue_rows(conn)
    return jsonify(_overdue_payload(rows))


@app.route('/api/tablet-status')
def api_tablet_status():
    """Live tablet + gadget numbers and overdue lists. Polled every few seconds by
    main.js so the banner, menu badges and dashboard update without a page reload."""
    if not _api_admin_ok():
        return jsonify({'error': 'login required'}), 401
    with get_db() as conn:
        c = conn.execute(
            "SELECT (SELECT COUNT(*) FROM tablets WHERE is_active=1) AS total, "
            "(SELECT COUNT(*) FROM tablet_transactions WHERE status='Borrowed') AS borrowed, "
            "(SELECT COUNT(*) FROM laptop_students WHERE is_active=1) AS gad_students, "
            "(SELECT COUNT(DISTINCT se.student_id) FROM laptop_sessions se "
            " JOIN laptop_students ls ON ls.id = se.student_id "
            " WHERE se.status='Out' AND ls.is_active=1) AS gad_out"
        ).fetchone()
        rows = _overdue_rows(conn)
        gad_rows = _gadget_overdue_rows(conn)
    total, borrowed, overdue_n = c['total'], c['borrowed'], len(rows)
    gad_students, gad_out, gad_overdue_n = c['gad_students'], c['gad_out'], len(gad_rows)
    resp = jsonify({
        'counts': {
            'total': total,
            'in_storage': max(total - borrowed, 0),
            'borrowed': borrowed,
            'overdue': overdue_n,
        },
        # same maths as the dashboard's Tablet Status chart
        'chart': [max(total - borrowed, 0), max(borrowed - overdue_n, 0), overdue_n],
        'overdue': _overdue_payload(rows),
        'gadgets': {
            'students': gad_students,
            'out': gad_out,
            'in_storage': max(gad_students - gad_out, 0),
            'overdue': gad_overdue_n,
        },
        'gadget_chart': [max(gad_students - gad_out, 0), max(gad_out - gad_overdue_n, 0),
                         gad_overdue_n],
        'gadget_overdue': _gadget_overdue_payload(gad_rows),
        'server_ms': int(_time.time() * 1000),
    })
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ─────────────────────────────────────────────
# TEMPLATE FILTERS
# ─────────────────────────────────────────────

@app.template_filter('fmt12')
def _fmt12_filter(value):
    try:
        return fmt12(value)
    except Exception:
        return value or ''


@app.template_filter('fmt_dt')
def fmt_dt(value):
    if not value:
        return '—'
    try:
        return to_local(value).strftime('%d %b %H:%M')
    except Exception:
        return value


@app.template_filter('fmt_dt_time')
def fmt_dt_time(value):
    if not value:
        return '—'
    try:
        return to_local(value).strftime('%I:%M %p')
    except Exception:
        return value


@app.template_filter('fmt_dt_full')
def fmt_dt_full(value):
    if not value:
        return '—'
    try:
        return to_local(value).strftime('%a %d %b %Y, %I:%M %p')
    except Exception:
        return value


@app.template_filter('fmt_date')
def fmt_date(value):
    if not value:
        return '—'
    try:
        return to_local(value).strftime('%a %d %b %Y')
    except Exception:
        return value


@app.template_filter('ago')
def ago(value):
    """'just now', '5 min ago', '3 h ago', or the date for older entries."""
    if not value:
        return ''
    try:
        secs = (datetime.now(timezone.utc) - to_local(value).astimezone(timezone.utc)).total_seconds()
        if secs < 60:
            return 'just now'
        if secs < 3600:
            return f'{int(secs // 60)} min ago'
        if secs < 86400:
            return f'{int(secs // 3600)} h ago'
        return to_local(value).strftime('%d %b')
    except Exception:
        return ''


@app.template_filter('initials')
def initials(name):
    """'A.Enid' -> 'AE', 'Mr.Osojo' -> 'MO', 'Academicminister' -> 'AC'."""
    parts = [p for p in re.split(r'[^A-Za-z]+', name or '') if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (parts[0][:2] if parts else '?').upper()


@app.template_filter('fmt_minutes')
def fmt_minutes(minutes):
    """35 -> '35 min', 95 -> '1 h 35 min'."""
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return '—'
    h, m = divmod(total, 60)
    if h and m:
        return f'{h} h {m} min'
    return f'{h} h' if h else f'{m} min'


@app.template_filter('fmt_duration')
def fmt_duration(hours):
    """2.5 -> '2 h 30 min'."""
    try:
        total = int(round(float(hours) * 60))
    except (TypeError, ValueError):
        return '—'
    h, m = divmod(total, 60)
    if h and m:
        return f'{h} h {m} min'
    return f'{h} h' if h else f'{m} min'


# ─────────────────────────────────────────────
# ENTRY POINT (local development only; Render uses gunicorn)
# ─────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5000)
