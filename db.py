"""
Turso (libSQL) database helper for the EIA system.

Talks to Turso over plain HTTPS (the "Hrana over HTTP" pipeline API) using
the `requests` library. This replaces the native `libsql` Python package,
which crashed/hung inside gunicorn workers on Render
("failed to join thread: Resource deadlock avoided" -> WORKER TIMEOUT -> 502).

Same usage style app.py already has:
    with get_db() as conn:
        conn.execute("SELECT ...", (param,)).fetchone()
        row['name']   and   row[0]   and   dict(row)

Environment variables:
    TURSO_DATABASE_URL   e.g. libsql://mydb-myorg.turso.io
    TURSO_AUTH_TOKEN     database token

Note: each statement is committed on its own (autocommit).
"""
import base64
import os

import requests

REQUEST_TIMEOUT = 15  # seconds; a slow database can never hang a worker

_session = requests.Session()


def _http_url():
    url = os.environ["TURSO_DATABASE_URL"].strip()
    for prefix, repl in (("libsql://", "https://"), ("wss://", "https://"),
                         ("ws://", "http://")):
        if url.startswith(prefix):
            url = repl + url[len(prefix):]
            break
    return url.rstrip("/") + "/v2/pipeline"


def _encode(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _decode(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")


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
    def __init__(self, cols, rows, lastrowid=None, rowcount=0):
        self._cols = cols
        self._rows = rows
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return Row(self._cols, r)

    def fetchall(self):
        rows = self._rows[self._i:]
        self._i = len(self._rows)
        return [Row(self._cols, r) for r in rows]


class Conn:
    @staticmethod
    def _post(requests_list):
        resp = _session.post(
            _http_url(),
            json={"requests": requests_list + [{"type": "close"}]},
            headers={"Authorization": "Bearer " + os.environ["TURSO_AUTH_TOKEN"]},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["results"]

    @staticmethod
    def _stmt(sql, params=()):
        return {"type": "execute",
                "stmt": {"sql": sql, "args": [_encode(p) for p in tuple(params)]}}

    def execute(self, sql, params=()):
        first = self._post([self._stmt(sql, params)])[0]
        if first.get("type") == "error":
            raise RuntimeError("Turso error: " + first["error"].get("message", "unknown"))
        res = first["response"]["result"]
        cols = [c.get("name") for c in res.get("cols", [])]
        rows = [[_decode(c) for c in r] for r in res.get("rows", [])]
        lid = res.get("last_insert_rowid")
        return Result(cols, rows,
                      int(lid) if lid not in (None, "") else None,
                      res.get("affected_row_count", 0))

    def run_batch(self, statements):
        """Send several statements in ONE request (fast: one round trip).
        `statements` is a list of (sql, params). Each statement is atomic on
        its own; an error in any statement is raised."""
        results = self._post([self._stmt(sql, params) for sql, params in statements])
        for r in results:
            if r.get("type") == "error":
                raise RuntimeError("Turso error: " + r["error"].get("message", "unknown"))

    def commit(self):  # statements are already committed individually
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
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
    signed_out_by TEXT,
    signed_back_by TEXT,
    took_charger INTEGER DEFAULT 0,
    took_earphones INTEGER DEFAULT 0,
    status TEXT DEFAULT 'Borrowed',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


# Columns added after the first release. Added automatically to existing
# databases (existing rows are kept; the new columns are simply empty for them).
MIGRATIONS = [
    ("tablet_transactions", "signed_out_by", "TEXT"),
    ("tablet_transactions", "signed_back_by", "TEXT"),
]


def init_db():
    """Creates missing tables and columns (safe to run on existing data)."""
    with get_db() as conn:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for table, column, decl in MIGRATIONS:
            existing = [r["name"] for r in
                        conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
