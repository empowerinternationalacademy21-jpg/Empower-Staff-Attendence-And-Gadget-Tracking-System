"""
EIA Staff Attendance and Gadget Tracking System
Backend: Flask + built-in sqlite3 (no external ORM required)
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, make_response, send_file
from datetime import datetime, date, timedelta
from functools import wraps
import sqlite3
import os
import io

# ReportLab imports for PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import Image as RLImage
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Circle
from reportlab.graphics import renderPDF

app = Flask(__name__)
app.config['SECRET_KEY'] = 'eia-secret-key-2024'
DB_PATH = os.path.join(os.path.dirname(__file__), 'eia_system.db')

# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS staff (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL UNIQUE,
            phone       TEXT,
            department  TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id    INTEGER NOT NULL REFERENCES staff(id),
            date        TEXT    NOT NULL,
            status      TEXT    DEFAULT 'Absent',
            time_in     TEXT,
            created_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(staff_id, date)
        );

        CREATE TABLE IF NOT EXISTS tablets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tablet_id   TEXT    NOT NULL UNIQUE,
            name        TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tablet_transactions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            tablet_id            INTEGER NOT NULL REFERENCES tablets(id),
            student_name         TEXT    NOT NULL,
            student_class        TEXT,
            quantity             INTEGER DEFAULT 1,
            duration_hours       REAL    NOT NULL,
            sign_out_time        TEXT    DEFAULT (datetime('now')),
            expected_return_time TEXT,
            sign_back_time       TEXT,
            took_charger         INTEGER DEFAULT 0,
            took_earphones       INTEGER DEFAULT 0,
            status               TEXT    DEFAULT 'Borrowed',
            created_at           TEXT    DEFAULT (datetime('now'))
        );
        """)


def seed_data():
    # All staff and tablets are entered by the admin — no automatic sample data.
    pass

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'eia2024'

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'logged_in' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if (request.form.get('username') == ADMIN_USERNAME and
                request.form.get('password') == ADMIN_PASSWORD):
            session['logged_in'] = True
            flash('Welcome back, Admin!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out successfully.', 'info')
    response = make_response(redirect(url_for('index')))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']        = 'no-cache'
    response.headers['Expires']       = '0'
    return response

# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    today = date.today().isoformat()
    now   = datetime.utcnow().isoformat()

    with get_db() as conn:
        total_staff   = conn.execute("SELECT COUNT(*) FROM staff WHERE is_active=1").fetchone()[0]
        # Count only attendance records that belong to active staff members
        present_today = conn.execute(
            "SELECT COUNT(*) FROM attendance a JOIN staff s ON a.staff_id = s.id"
            " WHERE a.date=? AND a.status='Present' AND s.is_active=1",
            (today,)
        ).fetchone()[0]
        absent_today  = total_staff - present_today

        total_tablets  = conn.execute("SELECT COUNT(*) FROM tablets WHERE is_active=1").fetchone()[0]
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

    return render_template('dashboard.html',
        total_staff=total_staff,
        present_today=present_today,
        absent_today=absent_today,
        total_tablets=total_tablets,
        borrowed_tablets=borrowed_count,
        overdue_tablets=overdue,
        recently_returned=recently_returned,
        today=date.today()
    )

# ─────────────────────────────────────────────
# GATE ATTENDANCE
# ─────────────────────────────────────────────

@app.route('/gate')
def gate():
    today = date.today().isoformat()
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name"
        ).fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?", (today,)
        ).fetchall()
    today_attendance = {row['staff_id']: row for row in att_rows}
    return render_template('gate.html',
        staff_list=staff_list,
        today_attendance=today_attendance,
        today=date.today()
    )


@app.route('/gate/mark', methods=['POST'])
def mark_attendance():
    staff_id = request.form.get('staff_id')
    today    = date.today().isoformat()
    now_time = datetime.utcnow().strftime('%H:%M:%S')

    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM attendance WHERE staff_id=? AND date=?", (staff_id, today)
        ).fetchone()

        if existing:
            if existing['status'] == 'Present':
                conn.execute(
                    "UPDATE attendance SET status='Absent', time_in=NULL WHERE id=?",
                    (existing['id'],)
                )
            else:
                conn.execute(
                    "UPDATE attendance SET status='Present', time_in=? WHERE id=?",
                    (now_time, existing['id'])
                )
        else:
            conn.execute(
                "INSERT INTO attendance (staff_id, date, status, time_in) VALUES (?,?,?,?)",
                (staff_id, today, 'Present', now_time)
            )
    return redirect(url_for('gate'))

# ─────────────────────────────────────────────
# STAFF MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/staff')
@login_required
def staff_list():
    with get_db() as conn:
        staff = conn.execute("SELECT * FROM staff WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('staff_list.html', staff=staff)


@app.route('/staff/add', methods=['GET', 'POST'])
@login_required
def add_staff():
    if request.method == 'POST':
        name  = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        dept  = request.form.get('department')
        with get_db() as conn:
            if conn.execute("SELECT id FROM staff WHERE email=?", (email,)).fetchone():
                flash('A staff member with that email already exists.', 'danger')
                return redirect(url_for('add_staff'))
            conn.execute(
                "INSERT INTO staff (name, email, phone, department) VALUES (?,?,?,?)",
                (name, email, phone, dept)
            )
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
                 request.form.get('phone'), request.form.get('department'), staff_id)
            )
            flash('Staff details updated.', 'success')
            return redirect(url_for('staff_list'))
    return render_template('edit_staff.html', staff=staff)


@app.route('/staff/delete/<int:staff_id>', methods=['POST'])
@login_required
def delete_staff(staff_id):
    with get_db() as conn:
        s = conn.execute("SELECT name FROM staff WHERE id=?", (staff_id,)).fetchone()
        # Soft-delete the staff member
        conn.execute("UPDATE staff SET is_active=0 WHERE id=?", (staff_id,))
        # Remove any attendance records for this staff so they no longer appear
        # in present/absent counts or dashboards. This keeps the attendance
        # aggregates accurate for active staff.
        conn.execute("DELETE FROM attendance WHERE staff_id=?", (staff_id,))
    flash(f'{s["name"]} removed.', 'info')
    return redirect(url_for('staff_list'))

# ─────────────────────────────────────────────
# ATTENDANCE HISTORY
# ─────────────────────────────────────────────

@app.route('/attendance/history')
@login_required
def attendance_history():
    selected_date_str = request.args.get('date', date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    with get_db() as conn:
        all_staff = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name"
        ).fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?", (selected_date.isoformat(),)
        ).fetchall()

    records = {row['staff_id']: row for row in att_rows}
    return render_template('attendance_history.html',
        all_staff=all_staff,
        records=records,
        selected_date=selected_date
    )

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
        tablets = [{**dict(t), 'current_status': tablet_status(conn, t['id'])} for t in tablets_raw]
    return render_template('tablet_list.html', tablets=tablets)


@app.route('/tablets/add', methods=['GET', 'POST'])
@login_required
def add_tablet():
    if request.method == 'POST':
        prefix   = request.form.get('prefix', 'TAB').strip().upper()
        device   = request.form.get('device_name', '').strip()
        quantity = max(1, min(int(request.form.get('quantity', 1)), 100))

        with get_db() as conn:
            # Check ALL rows (active + inactive) to avoid UNIQUE constraint on tablet_id
            rows = conn.execute(
                "SELECT tablet_id, is_active FROM tablets WHERE tablet_id LIKE ?",
                (f'{prefix}-%',)
            ).fetchall()

            active_nums   = set()   # numbers currently in use (block these)
            inactive_tids = {}      # tid -> True for soft-deleted rows we can reactivate

            for r in rows:
                parts = r['tablet_id'].split('-')
                if len(parts) >= 2:
                    try:
                        num = int(parts[-1])
                        if r['is_active']:
                            active_nums.add(num)
                        else:
                            inactive_tids[r['tablet_id']] = num
                    except ValueError:
                        pass

            # Fill gaps from 1 — skip numbers already active
            added   = []
            counter = 1
            while len(added) < quantity:
                if counter > 9999:
                    break
                if counter not in active_nums:
                    tid  = f"{prefix}-{str(counter).zfill(2)}"
                    name = f"{device} {str(counter).zfill(2)}" if device else tid
                    if tid in inactive_tids:
                        # Reactivate the soft-deleted row instead of inserting
                        conn.execute(
                            "UPDATE tablets SET is_active=1, name=? WHERE tablet_id=?",
                            (name, tid)
                        )
                    else:
                        conn.execute(
                            "INSERT INTO tablets (tablet_id, name) VALUES (?,?)",
                            (tid, name)
                        )
                    added.append(tid)
                    active_nums.add(counter)
                counter += 1

        if added:
            flash(
                f"{len(added)} tablet(s) registered: {added[0]}"
                + (f" → {added[-1]}" if len(added) > 1 else ""),
                'success'
            )
        return redirect(url_for('tablet_list'))

    with get_db() as conn:
        tablet_count = conn.execute("SELECT COUNT(*) FROM tablets WHERE is_active=1").fetchone()[0]
        active_rows  = conn.execute("SELECT tablet_id FROM tablets WHERE is_active=1").fetchall()

    # Build { "TAB": [1,3,5], "IPD": [2] } so the JS preview knows gaps per prefix
    used_nums_by_prefix = {}
    for r in active_rows:
        parts = r['tablet_id'].split('-')
        if len(parts) >= 2:
            try:
                prefix = '-'.join(parts[:-1]).upper()
                num    = int(parts[-1])
                used_nums_by_prefix.setdefault(prefix, []).append(num)
            except ValueError:
                pass

    return render_template('add_tablet.html',
                           tablet_count=tablet_count,
                           used_nums_by_prefix=used_nums_by_prefix)


@app.route('/tablets/signout', methods=['GET', 'POST'])
def tablet_signout():
    with get_db() as conn:
        if request.method == 'POST':
            tablet_db_id   = request.form.get('tablet_id')
            student_name   = request.form.get('student_name')
            student_class  = request.form.get('student_class')
            duration_hours = float(request.form.get('duration_hours', 1))
            quantity       = int(request.form.get('quantity', 1))
            took_charger   = 1 if 'took_charger'   in request.form else 0
            took_earphones = 1 if 'took_earphones' in request.form else 0

            if tablet_status(conn, tablet_db_id) == 'Borrowed':
                flash('That tablet is currently borrowed. Choose another.', 'warning')
                return redirect(url_for('tablet_signout'))

            now      = datetime.utcnow()
            expected = (now + timedelta(hours=duration_hours)).isoformat()

            conn.execute("""
                INSERT INTO tablet_transactions
                  (tablet_id, student_name, student_class, quantity,
                   duration_hours, sign_out_time, expected_return_time,
                   took_charger, took_earphones, status)
                VALUES (?,?,?,?,?,?,?,?,?,'Borrowed')
            """, (tablet_db_id, student_name, student_class, quantity,
                  duration_hours, now.isoformat(), expected,
                  took_charger, took_earphones))

            tab = conn.execute("SELECT tablet_id FROM tablets WHERE id=?", (tablet_db_id,)).fetchone()
            flash(
                f'Tablet {tab["tablet_id"]} signed out to {student_name}. '
                f'Expected return: {(now + timedelta(hours=duration_hours)).strftime("%I:%M %p")}',
                'success'
            )
            return redirect(url_for('tablet_signout'))

        all_tablets = conn.execute("SELECT * FROM tablets WHERE is_active=1").fetchall()
        available   = [t for t in all_tablets if tablet_status(conn, t['id']) == 'Available']
    return render_template('tablet_signout.html', tablets=available)


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
    return render_template('tablet_transactions.html', active=active, history=history, now=now)


@app.route('/tablets/return/<int:tx_id>', methods=['POST'])
@login_required
def tablet_return(tx_id):
    with get_db() as conn:
        tx = conn.execute(
            "SELECT student_name FROM tablet_transactions WHERE id=?", (tx_id,)
        ).fetchone()
        conn.execute(
            "UPDATE tablet_transactions SET status='Returned', sign_back_time=? WHERE id=?",
            (datetime.utcnow().isoformat(), tx_id)
        )
    flash(f'Tablet returned by {tx["student_name"]}.', 'success')
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
                'danger'
            )
            return redirect(url_for('tablet_list'))

        # Soft delete — preserves transaction history (foreign key safe)
        conn.execute(
            "UPDATE tablets SET is_active=0 WHERE id=?", (tablet_id,)
        )

    flash(
        f'Tablet {tablet["tablet_id"]} has been removed from the system.',
        'success'
    )
    return redirect(url_for('tablet_list'))
# ─────────────────────────────────────────────
# PDF ATTENDANCE REPORT
# ─────────────────────────────────────────────

@app.route('/attendance/pdf')
@login_required
def attendance_pdf():
    selected_date_str = request.args.get('date', date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()

    with get_db() as conn:
        all_staff = conn.execute(
            "SELECT * FROM staff WHERE is_active=1 ORDER BY name"
        ).fetchall()
        att_rows = conn.execute(
            "SELECT * FROM attendance WHERE date=?", (selected_date.isoformat(),)
        ).fetchall()

    records = {row['staff_id']: row for row in att_rows}
    logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')

    # ── Brand colours ────────────────────────────────────────────────────────
    NAVY       = colors.HexColor('#1a2a6c')
    LIME_DARK  = colors.HexColor('#5a9a10')
    GREEN      = colors.HexColor('#16a34a')
    GREEN_BG   = colors.HexColor('#dcfce7')
    RED        = colors.HexColor('#dc2626')
    RED_BG     = colors.HexColor('#fee2e2')
    GOLD       = colors.HexColor('#d4a843')
    GREY_50    = colors.HexColor('#f9fafb')
    GREY_100   = colors.HexColor('#f3f4f6')
    GREY_200   = colors.HexColor('#e5e7eb')
    GREY_400   = colors.HexColor('#9ca3af')
    GREY_600   = colors.HexColor('#4b5563')
    WHITE      = colors.white
    BLACK      = colors.HexColor('#111827')

    PAGE_W, PAGE_H = A4
    MARGIN = 14 * mm

    # ── Derived data ─────────────────────────────────────────────────────────
    present_list, absent_list = [], []
    for s in all_staff:
        att = records.get(s['id'])
        if att and att['status'] == 'Present':
            present_list.append((s, att))
        else:
            absent_list.append((s, att))

    total   = len(all_staff)
    n_pres  = len(present_list)
    n_abs   = len(absent_list)
    pct     = int(n_pres / total * 100) if total else 0
    gen_str = datetime.now().strftime('%d %b %Y  •  %I:%M %p')

    def time_fmt(t):
        if not t: return '—'
        try: return datetime.strptime(t, '%H:%M:%S').strftime('%I:%M %p')
        except: return t

    def make_avatar(initial, bg, fg=WHITE, size=6.5*mm):
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
                            topMargin=10*mm, bottomMargin=12*mm)
    story = []

    # ── HEADER ───────────────────────────────────────────────────────────────
    if os.path.exists(logo_path):
        logo_cell = RLImage(logo_path, width=16*mm, height=16*mm)
    else:
        logo_cell = Paragraph('EIA', ParagraphStyle('lc', fontSize=14,
                              fontName='Helvetica-Bold', textColor=WHITE, alignment=TA_CENTER))

    school_p = Paragraph(
        '<font size=15><b>Empower International Academy</b></font><br/>'
        '<font size=9 color="#9ca3af">Staff Attendance Register</font>',
        ParagraphStyle('sp', fontName='Helvetica', textColor=WHITE, leading=18)
    )
    date_p = Paragraph(
        f'<font size=9 color="#9ca3af">Date</font><br/>'
        f'<font size=13><b>{selected_date.strftime("%A")}</b></font><br/>'
        f'<font size=10>{selected_date.strftime("%d %B %Y")}</font>',
        ParagraphStyle('dp', fontName='Helvetica', textColor=WHITE, leading=15, alignment=TA_RIGHT)
    )
    hdr = Table([[logo_cell, school_p, date_p]], colWidths=[20*mm, 110*mm, 52*mm])
    hdr.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), NAVY),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING',   (0,0), (0,0),   8),
        ('LEFTPADDING',   (1,0), (1,0),   10),
        ('RIGHTPADDING',  (2,0), (2,0),   10),
        ('ROUNDEDCORNERS',[6]),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 5*mm))

    # ── STAT CARDS ────────────────────────────────────────────────────────────
    def stat_p(val, label, val_hex, bg):
        return Paragraph(
            f'<font size=22 color="{val_hex}"><b>{val}</b></font><br/>'
            f'<font size=8 color="{GREY_600.hexval()}">{label}</font>',
            ParagraphStyle('sc', fontName='Helvetica', alignment=TA_CENTER, leading=26)
        )

    stats = Table([[
        stat_p(n_pres, 'PRESENT',      GREEN.hexval(),                  GREEN_BG),
        stat_p(n_abs,  'ABSENT',       RED.hexval(),                    RED_BG),
        stat_p(total,  'TOTAL STAFF',  NAVY.hexval(),                   GREY_100),
        stat_p(f'{pct}%', 'ATTENDANCE RATE', colors.HexColor('#a16207').hexval(), colors.HexColor('#fef9c3')),
    ]], colWidths=[(PAGE_W - MARGIN*2)/4]*4)
    stats.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (0,0), GREEN_BG),
        ('BACKGROUND',    (1,0), (1,0), RED_BG),
        ('BACKGROUND',    (2,0), (2,0), GREY_100),
        ('BACKGROUND',    (3,0), (3,0), colors.HexColor('#fef9c3')),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('INNERGRID',     (0,0), (-1,-1), 1, WHITE),
        ('BOX',           (0,0), (-1,-1), 1, GREY_200),
        ('ROUNDEDCORNERS',[5]),
    ]))
    story.append(stats)

    # Attendance progress bar
    story.append(Spacer(1, 3*mm))
    bar_w = PAGE_W - MARGIN*2
    bar_fill = bar_w * (pct / 100)
    bar_color = GREEN if pct >= 75 else GOLD if pct >= 50 else RED
    pbar = Drawing(bar_w, 8)
    pbar.add(Rect(0, 0, bar_w, 8, rx=4, ry=4, fillColor=GREY_200, strokeColor=None))
    if bar_fill > 0:
        pbar.add(Rect(0, 0, bar_fill, 8, rx=4, ry=4, fillColor=bar_color, strokeColor=None))
    story.append(pbar)
    story.append(Paragraph(
        f'<font size=7.5 color="{GREY_400.hexval()}">Attendance rate: {pct}%  ({n_pres} of {total} staff present)</font>',
        ParagraphStyle('pb', fontName='Helvetica', alignment=TA_RIGHT, spaceAfter=2)
    ))
    story.append(Spacer(1, 4*mm))

    # ── PRESENT TABLE ─────────────────────────────────────────────────────────
    if present_list:
        sec_hdr = Table(
            [[Paragraph('✓  Present Staff  (' + str(n_pres) + ')',
                        ParagraphStyle('sh', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE))]],
            colWidths=[PAGE_W - MARGIN*2]
        )
        sec_hdr.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), GREEN),
            ('TOPPADDING',    (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
            ('LEFTPADDING',   (0,0), (-1,-1), 10),
            ('ROUNDEDCORNERS',[4]),
        ]))
        story.append(KeepTogether([sec_hdr]))
        story.append(Spacer(1, 1.5*mm))

        hdr_s = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=8, textColor=WHITE)
        p_rows = [[Paragraph('#', hdr_s), Paragraph('', hdr_s),
                   Paragraph('Name', hdr_s), Paragraph('Department', hdr_s), Paragraph('Time In', hdr_s)]]

        for i, (s, att) in enumerate(present_list, 1):
            row_bg = WHITE if i % 2 == 1 else GREY_50
            p_rows.append([
                Paragraph(str(i), ParagraphStyle('ix', fontName='Helvetica', fontSize=8,
                          textColor=GREY_400, alignment=TA_CENTER)),
                make_avatar(s['name'][0], NAVY),
                Paragraph(f'<b>{s["name"]}</b>',
                          ParagraphStyle('nm', fontName='Helvetica', fontSize=9, textColor=BLACK, leading=12)),
                Paragraph(s['department'] or '—',
                          ParagraphStyle('dm', fontName='Helvetica', fontSize=8.5, textColor=GREY_600, leading=12)),
                Paragraph(time_fmt(att['time_in'] if att else None),
                          ParagraphStyle('tm', fontName='Helvetica-Bold', fontSize=8.5,
                                         textColor=GREEN, alignment=TA_CENTER, leading=12)),
            ])

        row_fills = [('BACKGROUND', (0, i), (-1, i), WHITE if i % 2 == 1 else GREY_50)
                     for i in range(1, len(p_rows))]
        p_tbl = Table(p_rows, colWidths=[8*mm, 8*mm, 72*mm, 55*mm, 35*mm], repeatRows=1)
        p_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), NAVY),
            ('BACKGROUND',    (4,0), (4,0),  LIME_DARK),
            ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',      (0,0), (-1,0), 8),
            ('TOPPADDING',    (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',         (0,0), (1,-1),  'CENTER'),
            ('ALIGN',         (4,0), (4,-1),  'CENTER'),
            ('LINEBELOW',     (0,0), (-1,-1), 0.5, GREY_200),
            ('BOX',           (0,0), (-1,-1), 0.8, GREY_200),
            *row_fills,
        ]))
        story.append(p_tbl)
        story.append(Spacer(1, 5*mm))

    # ── ABSENT TABLE ──────────────────────────────────────────────────────────
    if absent_list:
        sec_hdr2 = Table(
            [[Paragraph('✗  Absent Staff  (' + str(n_abs) + ')',
                        ParagraphStyle('sh2', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE))]],
            colWidths=[PAGE_W - MARGIN*2]
        )
        sec_hdr2.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), RED),
            ('TOPPADDING',    (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
            ('LEFTPADDING',   (0,0), (-1,-1), 10),
            ('ROUNDEDCORNERS',[4]),
        ]))
        story.append(KeepTogether([sec_hdr2]))
        story.append(Spacer(1, 1.5*mm))

        hdr_r = ParagraphStyle('thr', fontName='Helvetica-Bold', fontSize=8, textColor=WHITE)
        a_rows = [[Paragraph('#', hdr_r), Paragraph('', hdr_r),
                   Paragraph('Name', hdr_r), Paragraph('Department', hdr_r), Paragraph('Status', hdr_r)]]

        for i, (s, att) in enumerate(absent_list, 1):
            a_rows.append([
                Paragraph(str(i), ParagraphStyle('ix2', fontName='Helvetica', fontSize=8,
                          textColor=GREY_400, alignment=TA_CENTER)),
                make_avatar(s['name'][0], RED),
                Paragraph(f'<b>{s["name"]}</b>',
                          ParagraphStyle('nm2', fontName='Helvetica', fontSize=9, textColor=BLACK, leading=12)),
                Paragraph(s['department'] or '—',
                          ParagraphStyle('dm2', fontName='Helvetica', fontSize=8.5, textColor=GREY_600, leading=12)),
                Paragraph('Absent',
                          ParagraphStyle('st2', fontName='Helvetica-Bold', fontSize=8,
                                         textColor=RED, alignment=TA_CENTER)),
            ])

        row_fills_a = [('BACKGROUND', (0, i), (-1, i), WHITE if i % 2 == 1 else colors.HexColor('#fff8f8'))
                       for i in range(1, len(a_rows))]
        a_tbl = Table(a_rows, colWidths=[8*mm, 8*mm, 80*mm, 60*mm, 26*mm], repeatRows=1)
        a_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#b91c1c')),
            ('BACKGROUND',    (4,0), (4,0),  RED),
            ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',      (0,0), (-1,0), 8),
            ('TOPPADDING',    (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',         (0,0), (1,-1),  'CENTER'),
            ('ALIGN',         (4,0), (4,-1),  'CENTER'),
            ('LINEBELOW',     (0,0), (-1,-1), 0.5, GREY_200),
            ('BOX',           (0,0), (-1,-1), 0.8, GREY_200),
            *row_fills_a,
        ]))
        story.append(a_tbl)

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    fl = Drawing(PAGE_W - MARGIN*2, 1)
    fl.add(Line(0, 0, PAGE_W - MARGIN*2, 0, strokeColor=GREY_200, strokeWidth=1))
    story.append(fl)
    story.append(Spacer(1, 3*mm))

    ft = Table([[
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">© 2026 Empower International Academy</font>',
                  ParagraphStyle('fl', fontName='Helvetica', alignment=TA_LEFT)),
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">EIA Staff Attendance &amp; Gadget Tracking System</font>',
                  ParagraphStyle('fc', fontName='Helvetica', alignment=TA_CENTER)),
        Paragraph(f'<font size=7.5 color="{GREY_400.hexval()}">Generated: {gen_str}</font>',
                  ParagraphStyle('fr', fontName='Helvetica', alignment=TA_RIGHT)),
    ]], colWidths=[(PAGE_W - MARGIN*2)/3]*3)
    ft.setStyle(TableStyle([
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
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
    today = date.today()
    # Go back 11 full months + current month
    start_date = (today.replace(day=1) - timedelta(days=335)).replace(day=1)

    with get_db() as conn:
        total_staff = conn.execute("SELECT COUNT(*) FROM staff WHERE is_active=1").fetchone()[0]
        rows = conn.execute("""
            SELECT date, COUNT(*) as present_count
            FROM attendance
            WHERE status='Present' AND date >= ?
            GROUP BY date
            ORDER BY date DESC
        """, (start_date.isoformat(),)).fetchall()

    # Build dict: date_str -> present_count
    daily = {r['date']: r['present_count'] for r in rows}

    # Build month groups
    months = []
    current = today.replace(day=1)
    for _ in range(12):
        months.append(current)
        # go to prev month
        current = (current - timedelta(days=1)).replace(day=1)
    months.reverse()

    return render_template('attendance_monthly.html',
        months=months,
        daily=daily,
        total_staff=total_staff,
        today=today
    )

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
        'expected': datetime.fromisoformat(r['expected_return_time']).strftime('%I:%M %p')
    } for r in rows]
    return jsonify(data)

# ─────────────────────────────────────────────
# TEMPLATE FILTERS
# ─────────────────────────────────────────────

@app.template_filter('fmt_time')
def fmt_time(value):
    if not value: return '—'
    try:
        return datetime.strptime(value, '%H:%M:%S').strftime('%I:%M %p')
    except Exception:
        return value


@app.template_filter('fmt_dt')
def fmt_dt(value):
    if not value: return '—'
    try:
        return datetime.fromisoformat(value).strftime('%d %b %H:%M')
    except Exception:
        return value


@app.template_filter('fmt_dt_time')
def fmt_dt_time(value):
    if not value: return '—'
    try:
        return datetime.fromisoformat(value).strftime('%I:%M %p')
    except Exception:
        return value

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    seed_data()
    app.run(debug=True, port=5000)