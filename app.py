"""
EIA Staff Attendance and Gadget Tracking System

Backend: Flask + Turso (libSQL, hosted SQLite) via db.py
Required environment variables:
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, SECRET_KEY
Admin logins are stored in the Turso "admins" table (hashed passwords).
Optional emergency login: set ADMIN_USERNAME (default "admin") and
ADMIN_PASSWORD in the environment; it works even if the table is empty.
"""
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, jsonify, make_response, send_file)
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import time as _time
from functools import wraps
import hmac
import re
import os
import io

from werkzeug.security import check_password_hash

from db import get_db, init_db

# ReportLab imports for PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, KeepTogether)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import Image as RLImage
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Circle

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or os.urandom(24).hex()

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
    'login':           ('Logged in',            'fa-sign-in-alt',  'a-grey'),
    'logout':          ('Logged out',           'fa-sign-out-alt', 'a-grey'),
    'staff_add':       ('Added staff',          'fa-user-plus',    'a-green'),
    'staff_edit':      ('Edited staff',         'fa-user-edit',    'a-blue'),
    'staff_delete':    ('Removed staff',        'fa-user-minus',   'a-red'),
    'tablet_register': ('Registered tablets',   'fa-plus-circle',  'a-green'),
    'tablet_remove':   ('Removed tablet',       'fa-trash-alt',    'a-red'),
    'tablet_signout':  ('Signed out tablet',    'fa-hand-holding', 'a-amber'),
    'tablet_return':   ('Signed in tablet',     'fa-undo',         'a-teal'),
    'report_pdf':      ('Downloaded report',    'fa-file-pdf',     'a-blue'),
}


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
    return {'action_meta': ACTION_META}


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
    today = local_today().isoformat()
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        total_staff = conn.execute(
            "SELECT COUNT(*) FROM staff WHERE is_active=1").fetchone()[0]

        # Count only attendance records that belong to active staff members
        present_today = conn.execute(
            "SELECT COUNT(*) FROM attendance a JOIN staff s ON a.staff_id = s.id"
            " WHERE a.date=? AND a.status='Present' AND s.is_active=1",
            (today,)
        ).fetchone()[0]
        absent_today = total_staff - present_today

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
            "WHERE student_class IS NOT NULL AND student_class <> '' "
            "GROUP BY student_class ORDER BY c DESC LIMIT 8").fetchall()

    out_map = {r['d']: r['c'] for r in out_rows}
    back_map = {r['d']: r['c'] for r in back_rows}
    days = [(local_today() - timedelta(days=i)) for i in range(13, -1, -1)]
    overdue_n = len(overdue)
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
        'staff': {
            'labels': ['Present', 'Absent'],
            'values': [present_today, max(absent_today, 0)],
            'total': total_staff,
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
                           total_staff=total_staff,
                           present_today=present_today,
                           absent_today=absent_today,
                           total_tablets=total_tablets,
                           borrowed_tablets=borrowed_count,
                           overdue_tablets=overdue,
                           recently_returned=recently_returned,
                           today=local_today())


# ─────────────────────────────────────────────
# GATE ATTENDANCE
# ─────────────────────────────────────────────

@app.route('/gate')
def gate():
    today = local_today().isoformat()
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name").fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?", (today,)).fetchall()
    today_attendance = {row['staff_id']: row for row in att_rows}
    return render_template('gate.html',
                           staff_list=staff_list,
                           today_attendance=today_attendance,
                           today=local_today())


@app.route('/gate/mark', methods=['POST'])
def mark_attendance():
    staff_id = request.form.get('staff_id')
    today = local_today().isoformat()
    now_time = datetime.utcnow().strftime('%H:%M:%S')
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM attendance WHERE staff_id=? AND date=?",
            (staff_id, today)
        ).fetchone()
        if existing:
            if existing['status'] == 'Present':
                conn.execute(
                    "UPDATE attendance SET status='Absent', time_in=NULL WHERE id=?",
                    (existing['id'],))
            else:
                conn.execute(
                    "UPDATE attendance SET status='Present', time_in=? WHERE id=?",
                    (now_time, existing['id']))
        else:
            conn.execute(
                "INSERT INTO attendance (staff_id, date, status, time_in) VALUES (?,?,?,?)",
                (staff_id, today, 'Present', now_time))
    return redirect(url_for('gate'))


# ─────────────────────────────────────────────
# STAFF MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/staff')
@login_required
def staff_list():
    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('staff_list.html', staff=staff)


@app.route('/staff/add', methods=['GET', 'POST'])
@login_required
def add_staff():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        dept = request.form.get('department')
        with get_db() as conn:
            if conn.execute("SELECT id FROM staff WHERE email=?", (email,)).fetchone():
                flash('A staff member with that email already exists.', 'danger')
                return redirect(url_for('add_staff'))
            conn.execute(
                "INSERT INTO staff (name, email, phone, department) VALUES (?,?,?,?)",
                (name, email, phone, dept))
        log_activity('staff_add', f'Added staff member {name}' + (f' ({dept})' if dept else ''))
        flash(f'{name} added successfully!', 'success')
        return redirect(url_for('staff_list'))
    return render_template('add_staff.html')


@app.route('/staff/edit/<int:staff_id>', methods=['GET', 'POST'])
@login_required
def edit_staff(staff_id):
    with get_db() as conn:
        staff = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        if not staff:
            flash('Staff not found.', 'danger')
            return redirect(url_for('staff_list'))
        if request.method == 'POST':
            conn.execute(
                "UPDATE staff SET name=?, email=?, phone=?, department=? WHERE id=?",
                (request.form.get('name'), request.form.get('email'),
                 request.form.get('phone'), request.form.get('department'), staff_id))
            log_activity('staff_edit', f"Updated details of {request.form.get('name') or staff['name']}")
            flash('Staff details updated.', 'success')
            return redirect(url_for('staff_list'))
    return render_template('edit_staff.html', staff=staff)


@app.route('/staff/delete/<int:staff_id>', methods=['POST'])
@login_required
def delete_staff(staff_id):
    with get_db() as conn:
        s = conn.execute("SELECT name FROM staff WHERE id=?", (staff_id,)).fetchone()
        if not s:
            flash('Staff not found.', 'danger')
            return redirect(url_for('staff_list'))
        # Soft-delete the staff member
        conn.execute("UPDATE staff SET is_active=0 WHERE id=?", (staff_id,))
        # Remove attendance records so they no longer affect present/absent counts
        conn.execute("DELETE FROM attendance WHERE staff_id=?", (staff_id,))
    log_activity('staff_delete', f'Removed staff member {s["name"]}')
    flash(f'{s["name"]} removed.', 'info')
    return redirect(url_for('staff_list'))


# ─────────────────────────────────────────────
# ATTENDANCE HISTORY
# ─────────────────────────────────────────────

def _parse_date_arg():
    value = request.args.get('date', local_today().isoformat())
    try:
        return date.fromisoformat(value)
    except ValueError:
        return local_today()


@app.route('/attendance/history')
@login_required
def attendance_history():
    selected_date = _parse_date_arg()
    with get_db() as conn:
        all_staff = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name").fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?",
            (selected_date.isoformat(),)).fetchall()
    records = {row['staff_id']: row for row in att_rows}
    return render_template('attendance_history.html',
                           all_staff=all_staff,
                           records=records,
                           selected_date=selected_date)


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
            took_charger = 1 if 'took_charger' in request.form else 0
            took_earphones = 1 if 'took_earphones' in request.form else 0

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

            if error:
                flash(error, 'danger')      # the form is shown again with what was typed
            else:
                duration_hours = round((ret_local - now_local).total_seconds() / 3600, 2)
                conn.execute("""
                    INSERT INTO tablet_transactions
                        (tablet_id, student_name, student_class, quantity,
                         duration_hours, sign_out_time, expected_return_time,
                         took_charger, took_earphones, status, signed_out_by)
                    VALUES (?,?,?,?,?,?,?,?,?,'Borrowed',?)
                """, (tablet_db_id, student_name, student_class, quantity,
                      duration_hours, to_utc_iso(now_local), to_utc_iso(ret_local),
                      took_charger, took_earphones, admin))

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
    return render_template('tablet_signout.html', tablets=available, admin=admin,
                           form=form, server_now_ms=int(_time.time() * 1000),
                           tz_name=APP_TZ_NAME)


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
    with get_db() as conn:
        tx = conn.execute(
            "SELECT tt.student_name, tt.status, tt.signed_out_by, t.tablet_id AS tab_code "
            "FROM tablet_transactions tt JOIN tablets t ON tt.tablet_id = t.id "
            "WHERE tt.id=?", (tx_id,)
        ).fetchone()
        if not tx:
            flash('Transaction not found.', 'danger')
            return redirect(url_for('tablet_transactions'))
        if tx['status'] != 'Borrowed':
            flash(f"Tablet {tx['tab_code']} was already signed back in.", 'info')
            return redirect(url_for('tablet_transactions'))
        conn.execute(
            "UPDATE tablet_transactions "
            "SET status='Returned', sign_back_time=?, signed_back_by=? "
            "WHERE id=? AND status='Borrowed'",
            (datetime.utcnow().isoformat(), admin, tx_id))
    log_activity('tablet_return',
                 f"Signed in {tx['tab_code']} from {tx['student_name']}"
                 + (f" (signed out by {tx['signed_out_by']})" if tx['signed_out_by'] else ''))
    flash(f"Tablet {tx['tab_code']} returned by {tx['student_name']}, "
          f"signed in by {admin}.", 'success')
    return redirect(url_for('tablet_transactions'))


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
# PDF ATTENDANCE REPORT
# ─────────────────────────────────────────────

@app.route('/attendance/pdf')
@login_required
def attendance_pdf():
    selected_date = _parse_date_arg()
    log_activity('report_pdf', f'Downloaded the attendance report for {selected_date.strftime("%d %b %Y")}')

    with get_db() as conn:
        all_staff = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name").fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?",
            (selected_date.isoformat(),)).fetchall()
    records = {row['staff_id']: row for row in att_rows}

    logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')

    # ── Brand colours ──
    NAVY = colors.HexColor('#1a2a6c')
    LIME_DARK = colors.HexColor('#5a9a10')
    GREEN = colors.HexColor('#16a34a')
    GREEN_BG = colors.HexColor('#dcfce7')
    RED = colors.HexColor('#dc2626')
    RED_BG = colors.HexColor('#fee2e2')
    GOLD = colors.HexColor('#d4a843')
    GREY_50 = colors.HexColor('#f9fafb')
    GREY_100 = colors.HexColor('#f3f4f6')
    GREY_200 = colors.HexColor('#e5e7eb')
    GREY_400 = colors.HexColor('#9ca3af')
    GREY_600 = colors.HexColor('#4b5563')
    WHITE = colors.white
    BLACK = colors.HexColor('#111827')

    PAGE_W, PAGE_H = A4
    MARGIN = 14 * mm
    CONTENT_W = PAGE_W - MARGIN * 2

    # ── Derived data ──
    present_list, absent_list = [], []
    for s in all_staff:
        att = records.get(s['id'])
        if att and att['status'] == 'Present':
            present_list.append((s, att))
        else:
            absent_list.append((s, att))

    total = len(all_staff)
    n_pres = len(present_list)
    n_abs = len(absent_list)
    pct = int(n_pres / total * 100) if total else 0
    gen_str = local_now().strftime('%d %b %Y • %I:%M %p')

    def time_fmt(t):
        return fmt_time(t)

    def make_avatar(initial, bg, fg=WHITE, size=6.5 * mm):
        d = Drawing(size, size)
        r = size / 2
        d.add(Circle(r, r, r, fillColor=bg, strokeColor=None))
        d.add(String(r, r - 2.2, initial.upper(),
                     fontName='Helvetica-Bold', fontSize=size * 0.42,
                     fillColor=fg, textAnchor='middle'))
        return d

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=MARGIN, leftMargin=MARGIN,
                            topMargin=10 * mm, bottomMargin=12 * mm)
    story = []

    # ── HEADER ──
    if os.path.exists(logo_path):
        logo_cell = RLImage(logo_path, width=16 * mm, height=16 * mm)
    else:
        logo_cell = Paragraph('EIA', ParagraphStyle(
            'lc', fontSize=14, fontName='Helvetica-Bold',
            textColor=WHITE, alignment=TA_CENTER))

    school_p = Paragraph(
        '<font size=15><b>Empower International Academy</b></font><br/>'
        '<font size=9 color="#9ca3af">Staff Attendance Register</font>',
        ParagraphStyle('sp', fontName='Helvetica', textColor=WHITE, leading=18))
    date_p = Paragraph(
        f'<font size=9 color="#9ca3af">Date</font><br/>'
        f'<font size=13><b>{selected_date.strftime("%A")}</b></font><br/>'
        f'<font size=10>{selected_date.strftime("%d %B %Y")}</font>',
        ParagraphStyle('dp', fontName='Helvetica', textColor=WHITE,
                       leading=15, alignment=TA_RIGHT))

    hdr = Table([[logo_cell, school_p, date_p]], colWidths=[20 * mm, 110 * mm, 52 * mm])
    hdr.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NAVY),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (0, 0), 8),
        ('LEFTPADDING', (1, 0), (1, 0), 10),
        ('RIGHTPADDING', (2, 0), (2, 0), 10),
        ('ROUNDEDCORNERS', [6]),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 5 * mm))

    # ── STAT CARDS ──
    def stat_p(val, label, val_hex):
        return Paragraph(
            f'<font size=22 color="{val_hex}"><b>{val}</b></font><br/>'
            f'<font size=8 color="{GREY_600.hexval()}">{label}</font>',
            ParagraphStyle('sc', fontName='Helvetica', alignment=TA_CENTER, leading=26))

    stats = Table([[
        stat_p(n_pres, 'PRESENT', GREEN.hexval()),
        stat_p(n_abs, 'ABSENT', RED.hexval()),
        stat_p(total, 'TOTAL STAFF', NAVY.hexval()),
        stat_p(f'{pct}%', 'ATTENDANCE RATE', colors.HexColor('#a16207').hexval()),
    ]], colWidths=[CONTENT_W / 4] * 4)
    stats.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), GREEN_BG),
        ('BACKGROUND', (1, 0), (1, 0), RED_BG),
        ('BACKGROUND', (2, 0), (2, 0), GREY_100),
        ('BACKGROUND', (3, 0), (3, 0), colors.HexColor('#fef9c3')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('INNERGRID', (0, 0), (-1, -1), 1, WHITE),
        ('BOX', (0, 0), (-1, -1), 1, GREY_200),
        ('ROUNDEDCORNERS', [5]),
    ]))
    story.append(stats)

    # Attendance progress bar
    story.append(Spacer(1, 3 * mm))
    bar_w = CONTENT_W
    bar_fill = bar_w * (pct / 100)
    bar_color = GREEN if pct >= 75 else GOLD if pct >= 50 else RED
    pbar = Drawing(bar_w, 8)
    pbar.add(Rect(0, 0, bar_w, 8, rx=4, ry=4, fillColor=GREY_200, strokeColor=None))
    if bar_fill > 0:
        pbar.add(Rect(0, 0, bar_fill, 8, rx=4, ry=4, fillColor=bar_color, strokeColor=None))
    story.append(pbar)
    story.append(Paragraph(
        f'<font size=7.5 color="{GREY_400.hexval()}">Attendance rate: {pct}% '
        f'({n_pres} of {total} staff present)</font>',
        ParagraphStyle('pb', fontName='Helvetica', alignment=TA_RIGHT, spaceAfter=2)))
    story.append(Spacer(1, 4 * mm))

    # ── PRESENT TABLE ──
    if present_list:
        sec_hdr = Table(
            [[Paragraph(f'✓ Present Staff ({n_pres})',
                        ParagraphStyle('sh', fontName='Helvetica-Bold',
                                       fontSize=10, textColor=WHITE))]],
            colWidths=[CONTENT_W])
        sec_hdr.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), GREEN),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('ROUNDEDCORNERS', [4]),
        ]))
        story.append(KeepTogether([sec_hdr]))
        story.append(Spacer(1, 1.5 * mm))

        hdr_s = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=8, textColor=WHITE)
        p_rows = [[Paragraph('#', hdr_s), Paragraph('', hdr_s), Paragraph('Name', hdr_s),
                   Paragraph('Department', hdr_s), Paragraph('Time In', hdr_s)]]
        for i, (s, att) in enumerate(present_list, 1):
            p_rows.append([
                Paragraph(str(i), ParagraphStyle('ix', fontName='Helvetica', fontSize=8,
                                                 textColor=GREY_400, alignment=TA_CENTER)),
                make_avatar(s['name'][0], NAVY),
                Paragraph(f'<b>{s["name"]}</b>',
                          ParagraphStyle('nm', fontName='Helvetica', fontSize=9,
                                         textColor=BLACK, leading=12)),
                Paragraph(s['department'] or '—',
                          ParagraphStyle('dm', fontName='Helvetica', fontSize=8.5,
                                         textColor=GREY_600, leading=12)),
                Paragraph(time_fmt(att['time_in'] if att else None),
                          ParagraphStyle('tm', fontName='Helvetica-Bold', fontSize=8.5,
                                         textColor=GREEN, alignment=TA_CENTER, leading=12)),
            ])
        row_fills = [('BACKGROUND', (0, i), (-1, i), WHITE if i % 2 == 1 else GREY_50)
                     for i in range(1, len(p_rows))]
        p_tbl = Table(p_rows, colWidths=[8 * mm, 8 * mm, 72 * mm, 55 * mm, 35 * mm],
                      repeatRows=1)
        p_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), NAVY),
            ('BACKGROUND', (4, 0), (4, 0), LIME_DARK),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (1, -1), 'CENTER'),
            ('ALIGN', (4, 0), (4, -1), 'CENTER'),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, GREY_200),
            ('BOX', (0, 0), (-1, -1), 0.8, GREY_200),
            *row_fills,
        ]))
        story.append(p_tbl)
        story.append(Spacer(1, 5 * mm))

    # ── ABSENT TABLE ──
    if absent_list:
        sec_hdr2 = Table(
            [[Paragraph(f'✗ Absent Staff ({n_abs})',
                        ParagraphStyle('sh2', fontName='Helvetica-Bold',
                                       fontSize=10, textColor=WHITE))]],
            colWidths=[CONTENT_W])
        sec_hdr2.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), RED),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('ROUNDEDCORNERS', [4]),
        ]))
        story.append(KeepTogether([sec_hdr2]))
        story.append(Spacer(1, 1.5 * mm))

        hdr_r = ParagraphStyle('thr', fontName='Helvetica-Bold', fontSize=8, textColor=WHITE)
        a_rows = [[Paragraph('#', hdr_r), Paragraph('', hdr_r), Paragraph('Name', hdr_r),
                   Paragraph('Department', hdr_r), Paragraph('Status', hdr_r)]]
        for i, (s, att) in enumerate(absent_list, 1):
            a_rows.append([
                Paragraph(str(i), ParagraphStyle('ix2', fontName='Helvetica', fontSize=8,
                                                 textColor=GREY_400, alignment=TA_CENTER)),
                make_avatar(s['name'][0], RED),
                Paragraph(f'<b>{s["name"]}</b>',
                          ParagraphStyle('nm2', fontName='Helvetica', fontSize=9,
                                         textColor=BLACK, leading=12)),
                Paragraph(s['department'] or '—',
                          ParagraphStyle('dm2', fontName='Helvetica', fontSize=8.5,
                                         textColor=GREY_600, leading=12)),
                Paragraph('Absent',
                          ParagraphStyle('st2', fontName='Helvetica-Bold', fontSize=8,
                                         textColor=RED, alignment=TA_CENTER)),
            ])
        row_fills_a = [('BACKGROUND', (0, i), (-1, i),
                        WHITE if i % 2 == 1 else colors.HexColor('#fff8f8'))
                       for i in range(1, len(a_rows))]
        a_tbl = Table(a_rows, colWidths=[8 * mm, 8 * mm, 80 * mm, 60 * mm, 26 * mm],
                      repeatRows=1)
        a_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#b91c1c')),
            ('BACKGROUND', (4, 0), (4, 0), RED),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (1, -1), 'CENTER'),
            ('ALIGN', (4, 0), (4, -1), 'CENTER'),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, GREY_200),
            ('BOX', (0, 0), (-1, -1), 0.8, GREY_200),
            *row_fills_a,
        ]))
        story.append(a_tbl)

    # ── FOOTER ──
    story.append(Spacer(1, 6 * mm))
    fl = Drawing(CONTENT_W, 1)
    fl.add(Line(0, 0, CONTENT_W, 0, strokeColor=GREY_200, strokeWidth=1))
    story.append(fl)
    story.append(Spacer(1, 3 * mm))
    ft = Table([[
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">© 2026 Empower International Academy</font>',
                  ParagraphStyle('fl', fontName='Helvetica', alignment=TA_LEFT)),
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">EIA Staff Attendance &amp; Gadget Tracking System</font>',
                  ParagraphStyle('fc', fontName='Helvetica', alignment=TA_CENTER)),
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">Generated: {gen_str}</font>',
                  ParagraphStyle('fr', fontName='Helvetica', alignment=TA_RIGHT)),
    ]], colWidths=[CONTENT_W / 3] * 3)
    ft.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(ft)

    doc.build(story)
    buffer.seek(0)
    filename = f"EIA_Attendance_{selected_date.isoformat()}.pdf"
    return send_file(buffer, mimetype='application/pdf',
                     as_attachment=False, download_name=filename)


# ─────────────────────────────────────────────
# 12-MONTH ATTENDANCE CALENDAR VIEW
# ─────────────────────────────────────────────

@app.route('/attendance/monthly')
@login_required
def attendance_monthly():
    """Shows a 12-month summary of attendance records."""
    today = local_today()
    # Go back 11 full months + current month
    start_date = (today.replace(day=1) - timedelta(days=335)).replace(day=1)

    with get_db() as conn:
        total_staff = conn.execute(
            "SELECT COUNT(*) FROM staff WHERE is_active=1").fetchone()[0]
        rows = conn.execute("""
            SELECT date, COUNT(*) as present_count
            FROM attendance
            WHERE status='Present' AND date >= ?
            GROUP BY date
            ORDER BY date DESC
        """, (start_date.isoformat(),)).fetchall()

    # date_str -> present_count
    daily = {r['date']: r['present_count'] for r in rows}

    # Build month groups
    months = []
    current = today.replace(day=1)
    for _ in range(12):
        months.append(current)
        current = (current - timedelta(days=1)).replace(day=1)
    months.reverse()

    return render_template('attendance_monthly.html',
                           months=months,
                           daily=daily,
                           total_staff=total_staff,
                           today=today)


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

@app.route('/api/overdue')
def api_overdue():
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT tt.id, tt.student_name, tt.expected_return_time, t.tablet_id AS tab_code
            FROM tablet_transactions tt
            JOIN tablets t ON tt.tablet_id = t.id
            WHERE tt.status='Borrowed' AND tt.expected_return_time < ?
        """, (now,)).fetchall()

    data = [{
        'id': r['id'],
        'tablet': r['tab_code'],
        'student': r['student_name'],
        'expected': fmt_dt_time(r['expected_return_time'])
    } for r in rows]
    return jsonify(data)


# ─────────────────────────────────────────────
# TEMPLATE FILTERS
# ─────────────────────────────────────────────

@app.template_filter('fmt_time')
def fmt_time(value):
    """Attendance time-in (stored as UTC HH:MM:SS) -> local 12-hour time."""
    if not value:
        return '—'
    try:
        t = datetime.strptime(value, '%H:%M:%S').time()
        utc_dt = datetime.combine(datetime.now(timezone.utc).date(), t, tzinfo=timezone.utc)
        return utc_dt.astimezone(APP_TZ).strftime('%I:%M %p')
    except Exception:
        return value


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
