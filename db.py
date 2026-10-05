"""
Turso (libSQL) database helper for the EIA system.

Gives app.py the same style it already uses with sqlite3:
    with get_db() as conn:
        conn.execute("SELECT ...", (param,)).fetchone()
        row['name']   and   row[0]
Reads credentials from environment variables:
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
"""
import os
import libsql


class Row(dict):
    """A result row usable as row['name'], row[0], and dict(row)."""

    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = tuple(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, cur):
        self._cur = cur
        self._cols = [d[0] for d in (cur.description or [])]

    def fetchone(self):
        r = self._cur.fetchone()
        return Row(self._cols, r) if r is not None else None

    def fetchall(self):
        return [Row(self._cols, r) for r in self._cur.fetchall()]


class Conn:
    def __init__(self):
        self._raw = libsql.connect(
            database=os.environ["TURSO_DATABASE_URL"],
            auth_token=os.environ["TURSO_AUTH_TOKEN"],
        )

    def execute(self, sql, params=()):
        return Result(self._raw.execute(sql, tuple(params)))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            try:
                self._raw.close()
            except Exception:
                pass
        return False


def get_db():
    return Conn()


SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    department TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    date TEXT NOT NULL,
    status TEXT DEFAULT 'Absent',
    time_in TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(staff_id, date)
);

CREATE TABLE IF NOT EXISTS tablets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tablet_id TEXT NOT NULL UNIQUE,
    name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tablet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tablet_id INTEGER NOT NULL REFERENCES tablets(id),
    student_name TEXT NOT NULL,
    student_class TEXT,
    quantity INTEGER DEFAULT 1,
    duration_hours REAL NOT NULL,
    sign_out_time TEXT DEFAULT (datetime('now')),
    expected_return_time TEXT,
    sign_back_time TEXT,
    took_charger INTEGER DEFAULT 0,
    took_earphones INTEGER DEFAULT 0,
    status TEXT DEFAULT 'Borrowed',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def init_db():
    """Creates the tables if they don't exist (safe to run on existing data)."""
    with get_db() as conn:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
