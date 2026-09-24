import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
import math

# ---------------------------------------------------------------------------
# Connection layer
# ---------------------------------------------------------------------------
# If DATABASE_URL is set (Railway/Render Postgres add-on sets this automatically),
# the bot and the admin panel each connect to that ONE remote PostgreSQL database
# over the network — so they can run as two fully independent processes/services
# and still always see the same data.
#
# If DATABASE_URL is NOT set, we fall back to a local SQLite file (DB_PATH) — handy
# for local development, but then bot.py and admin_app.py MUST run in the same
# process/machine/disk to share data (see app.py).
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "taxi.db")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    IntegrityError = psycopg2.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

# Tables that have a SERIAL/AUTOINCREMENT "id" column and whose INSERTs rely on
# getting that id back afterwards (cur.lastrowid in the sqlite world).
_TABLES_WITH_ID = {"users", "rides", "cities", "support_messages"}
_INSERT_TABLE_RE = re.compile(r"^\s*insert\s+into\s+(\w+)", re.IGNORECASE)


class _PGCursor:
    """Makes a psycopg2 cursor behave like a sqlite3 cursor: '?' placeholders
    and a .lastrowid attribute populated automatically after INSERTs."""

    def __init__(self, cursor):
        self._cur = cursor
        self.lastrowid = None

    def execute(self, sql, params=()):
        pg_sql = sql.replace("?", "%s")
        m = _INSERT_TABLE_RE.match(pg_sql)
        auto_returning = bool(
            m and m.group(1).lower() in _TABLES_WITH_ID and "returning" not in pg_sql.lower()
        )
        if auto_returning:
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
        self._cur.execute(pg_sql, params)
        if auto_returning:
            row = self._cur.fetchone()
            self.lastrowid = row["id"] if row else None
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PGConn:
    """Makes a psycopg2 connection behave like a sqlite3 connection."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self):
        return _PGCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_conn():
    if USE_POSTGRES:
        return _PGConn(psycopg2.connect(DATABASE_URL))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_cursor():
    """Guarantees the connection is always closed and any half-finished write is
    rolled back on error, instead of leaking the connection (the #1 cause of
    'PostgreSQL too many connections' crashes under load on Railway/Render).
    Use for any function that writes data or needs an atomic check-then-act step:

        with db_cursor() as (conn, cur):
            cur.execute(...)
            conn.commit()
            return cur.rowcount > 0
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield conn, cur
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db():
    if USE_POSTGRES:
        _init_db_postgres()
    else:
        _init_db_sqlite()


def _init_db_postgres():
    """Postgres schema + migrations. Uses ADD COLUMN IF NOT EXISTS, so unlike the
    SQLite path there's no need to introspect existing columns first."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            role TEXT DEFAULT NULL,
            is_online INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rides (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL,
            from_location TEXT,
            from_lat REAL,
            from_lon REAL,
            to_location TEXT,
            to_lat REAL,
            to_lon REAL,
            ride_time TEXT,
            price TEXT,
            status TEXT DEFAULT 'open',
            driver_id INTEGER,
            rating INTEGER,
            review TEXT,
            created_at TEXT,
            service_type TEXT DEFAULT 'taxi',
            route TEXT DEFAULT 'local',
            item_note TEXT,
            FOREIGN KEY (client_id) REFERENCES users(id),
            FOREIGN KEY (driver_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tariffs (
            route TEXT PRIMARY KEY,
            base_price TEXT,
            price_per_km TEXT,
            note TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            text TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,        -- 'commission' | 'topup' | 'withdrawal' | 'adjustment'
            amount REAL NOT NULL,      -- signed: commission/withdrawal negative, topup/adjustment can be +/-
            ride_id INTEGER,
            status TEXT DEFAULT 'completed',  -- 'pending' | 'completed' | 'rejected'
            note TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_offers (
            id SERIAL PRIMARY KEY,
            ride_id INTEGER NOT NULL,
            driver_id INTEGER NOT NULL,
            price TEXT NOT NULL,
            status TEXT DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected' | 'cancelled'
            created_at TEXT,
            FOREIGN KEY (ride_id) REFERENCES rides(id),
            FOREIGN KEY (driver_id) REFERENCES users(id)
        )
    """)
    conn.commit()

    alters = [
        ("users", "is_online", "INTEGER DEFAULT 0"),
        ("users", "is_blocked", "INTEGER DEFAULT 0"),
        ("users", "intercity_access", "INTEGER DEFAULT 0"),
        ("users", "car_brand", "TEXT"),
        ("users", "car_model", "TEXT"),
        ("users", "car_plate", "TEXT"),
        ("users", "car_color", "TEXT"),
        ("users", "car_year", "TEXT"),
        ("users", "car_photo", "TEXT"),
        ("users", "verification_status", "TEXT DEFAULT 'none'"),
        ("users", "reject_reason", "TEXT"),
        ("users", "last_lat", "REAL"),
        ("users", "last_lon", "REAL"),
        ("users", "id_photo", "TEXT"),
        ("users", "car_doc_photo", "TEXT"),
        ("users", "work_city", "TEXT"),
        ("users", "agreed_rules", "INTEGER DEFAULT 0"),
        ("users", "fraud_flag", "INTEGER DEFAULT 0"),
        ("users", "balance", "REAL DEFAULT 0"),
        ("rides", "from_lat", "REAL"),
        ("rides", "from_lon", "REAL"),
        ("rides", "to_lat", "REAL"),
        ("rides", "to_lon", "REAL"),
        ("rides", "rating", "INTEGER"),
        ("rides", "review", "TEXT"),
        ("rides", "service_type", "TEXT DEFAULT 'taxi'"),
        ("rides", "route", "TEXT DEFAULT 'local'"),
        ("rides", "item_note", "TEXT"),
        ("rides", "decline_reason", "TEXT"),
        ("rides", "offered_price", "TEXT"),
        ("rides", "client_rating", "INTEGER"),
        ("rides", "passenger_count", "INTEGER DEFAULT 1"),
    ]
    for table, col, ddl in alters:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
    conn.commit()
    conn.close()


def _init_db_sqlite():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            role TEXT DEFAULT NULL,   -- 'client' or 'driver'
            is_online INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            from_location TEXT,
            from_lat REAL,
            from_lon REAL,
            to_location TEXT,
            to_lat REAL,
            to_lon REAL,
            ride_time TEXT,
            price TEXT,
            status TEXT DEFAULT 'open',  -- open / accepted / arrived / completed / cancelled
            driver_id INTEGER,
            rating INTEGER,
            review TEXT,
            created_at TEXT,
            service_type TEXT DEFAULT 'taxi',  -- 'taxi' or 'delivery'
            route TEXT DEFAULT 'local',        -- 'local' (Жанақала ішінде) or 'intercity' (Жанақала — Орал)
            item_note TEXT,                    -- what to deliver (delivery orders only)
            FOREIGN KEY (client_id) REFERENCES users(id),
            FOREIGN KEY (driver_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tariffs (
            route TEXT PRIMARY KEY,   -- 'local' or 'intercity'
            base_price TEXT,
            price_per_km TEXT,
            note TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL,   -- 'in' (user -> admin) or 'out' (admin -> user)
            text TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,        -- 'commission' | 'topup' | 'withdrawal' | 'adjustment'
            amount REAL NOT NULL,      -- signed: commission/withdrawal negative, topup/adjustment can be +/-
            ride_id INTEGER,
            status TEXT DEFAULT 'completed',  -- 'pending' | 'completed' | 'rejected'
            note TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ride_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ride_id INTEGER NOT NULL,
            driver_id INTEGER NOT NULL,
            price TEXT NOT NULL,
            status TEXT DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected' | 'cancelled'
            created_at TEXT,
            FOREIGN KEY (ride_id) REFERENCES rides(id),
            FOREIGN KEY (driver_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    # migration guard: add is_online if upgrading an older db
    cur.execute("PRAGMA table_info(users)")
    cols = [c["name"] for c in cur.fetchall()]
    if "is_online" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_online INTEGER DEFAULT 0")
        conn.commit()
    if "is_blocked" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        conn.commit()
    if "intercity_access" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN intercity_access INTEGER DEFAULT 0")
        conn.commit()
    # --- driver vehicle / verification fields ---
    for col, ddl in (
        ("car_brand", "TEXT"), ("car_model", "TEXT"), ("car_plate", "TEXT"),
        ("car_color", "TEXT"), ("car_year", "TEXT"), ("car_photo", "TEXT"),
        ("verification_status", "TEXT DEFAULT 'none'"),  # none/pending/approved/rejected
        ("reject_reason", "TEXT"),
        ("last_lat", "REAL"), ("last_lon", "REAL"),
        ("id_photo", "TEXT"), ("car_doc_photo", "TEXT"), ("work_city", "TEXT"),
        ("agreed_rules", "INTEGER DEFAULT 0"), ("fraud_flag", "INTEGER DEFAULT 0"),
        ("balance", "REAL DEFAULT 0"),
    ):
        if col not in cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()

    cur.execute("PRAGMA table_info(rides)")
    ride_cols = [c["name"] for c in cur.fetchall()]
    for col in ("from_lat", "from_lon", "to_lat", "to_lon"):
        if col not in ride_cols:
            cur.execute(f"ALTER TABLE rides ADD COLUMN {col} REAL")
            conn.commit()
    if "rating" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN rating INTEGER")
        conn.commit()
    if "review" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN review TEXT")
        conn.commit()
    if "service_type" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN service_type TEXT DEFAULT 'taxi'")
        conn.commit()
    if "route" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN route TEXT DEFAULT 'local'")
        conn.commit()
    if "item_note" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN item_note TEXT")
        conn.commit()
    if "decline_reason" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN decline_reason TEXT")
        conn.commit()
    if "offered_price" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN offered_price TEXT")
        conn.commit()
    if "client_rating" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN client_rating INTEGER")
        conn.commit()
    if "passenger_count" not in ride_cols:
        cur.execute("ALTER TABLE rides ADD COLUMN passenger_count INTEGER DEFAULT 1")
        conn.commit()
    conn.close()


def get_or_create_user(telegram_id, username=None, full_name=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    user = cur.fetchone()
    if user is None:
        cur.execute(
            "INSERT INTO users (telegram_id, username, full_name, created_at) VALUES (?,?,?,?)",
            (telegram_id, username, full_name, datetime.now().isoformat()),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        user = cur.fetchone()
    conn.close()
    return dict(user)


def set_role(telegram_id, role):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role=? WHERE telegram_id=?", (role, telegram_id))
    conn.commit()
    conn.close()


def set_phone(telegram_id, phone):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET phone=? WHERE telegram_id=?", (phone, telegram_id))
    conn.commit()
    conn.close()


def set_online(telegram_id, is_online):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_online=? WHERE telegram_id=?", (1 if is_online else 0, telegram_id))
    conn.commit()
    conn.close()


def get_online_driver_telegram_ids():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM users WHERE role='driver' AND is_online=1")
    rows = [r["telegram_id"] for r in cur.fetchall()]
    conn.close()
    return rows


def create_ride(client_id, from_location, to_location, ride_time, price,
                 from_lat=None, from_lon=None, to_lat=None, to_lon=None,
                 service_type="taxi", route="local", item_note=None, passenger_count=1):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO rides (client_id, from_location, from_lat, from_lon, to_location, to_lat, to_lon,
                               ride_time, price, status, created_at, service_type, route, item_note, passenger_count)
           VALUES (?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?)""",
        (client_id, from_location, from_lat, from_lon, to_location, to_lat, to_lon,
         ride_time, price, datetime.now().isoformat(), service_type, route, item_note, passenger_count),
    )
    conn.commit()
    ride_id = cur.lastrowid
    conn.close()
    return ride_id


# ---------- Intercity (Жанақала — Орал) access control ----------

def set_intercity_access(user_id, allowed):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET intercity_access=? WHERE id=?", (1 if allowed else 0, user_id))
    conn.commit()
    conn.close()


def has_intercity_access(telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT intercity_access FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row["intercity_access"]) if row else False


def get_user_by_telegram_id(telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_rides(limit=20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT rides.*, users.full_name as client_name, users.phone as client_phone, users.username as client_username
           FROM rides JOIN users ON rides.client_id = users.id
           WHERE rides.status='open' ORDER BY rides.created_at DESC LIMIT ?""",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_ride(ride_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def accept_ride(ride_id, driver_id):
    """Driver accepts an open order -> status 'accepted'. Atomic: the WHERE status='open'
    guard is checked and applied in ONE statement, so if two drivers tap accept at the
    same instant, the database itself guarantees only the first UPDATE can succeed —
    no more separate SELECT-then-UPDATE race where both could win."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "UPDATE rides SET status='accepted', driver_id=? WHERE id=? AND status='open'",
            (driver_id, ride_id),
        )
        won = cur.rowcount > 0
        conn.commit()
        return won


def mark_arrived(ride_id, driver_id):
    """Driver has arrived at the pickup point -> status 'arrived'."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "UPDATE rides SET status='arrived' WHERE id=? AND driver_id=? AND status='accepted'",
            (ride_id, driver_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        return ok


def complete_ride(ride_id):
    """Force-complete, used by admin panel regardless of current stage."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE rides SET status='completed' WHERE id=?", (ride_id,))
    conn.commit()
    conn.close()


def complete_ride_by_driver(ride_id, driver_id):
    """Driver finishes the trip -> status 'completed'. Only from 'arrived'.
    Atomic UPDATE so a double-tap (or a retried Telegram callback) can't complete
    the same ride twice and charge commission twice."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "UPDATE rides SET status='completed' WHERE id=? AND driver_id=? AND status='arrived'",
            (ride_id, driver_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        return ok


def rate_ride(ride_id, client_id, rating, review=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, client_id, rating FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    if row is None or row["client_id"] != client_id or row["status"] != "completed" or row["rating"] is not None:
        conn.close()
        return False
    cur.execute("UPDATE rides SET rating=?, review=? WHERE id=?", (rating, review, ride_id))
    conn.commit()
    conn.close()
    return True


def get_driver_rating(driver_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT rating FROM rides WHERE driver_id=? AND rating IS NOT NULL", (driver_id,))
    rows = [r["rating"] for r in cur.fetchall()]
    conn.close()
    if not rows:
        return {"avg": None, "count": 0}
    return {"avg": round(sum(rows) / len(rows), 1), "count": len(rows)}


def get_driver_history(driver_id, limit=10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT rides.*, c.full_name as client_name FROM rides
           LEFT JOIN users c ON rides.client_id = c.id
           WHERE rides.driver_id=? AND rides.status='completed'
           ORDER BY rides.created_at DESC LIMIT ?""",
        (driver_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def set_blocked(user_id, blocked):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked=? WHERE id=?", (1 if blocked else 0, user_id))
    conn.commit()
    conn.close()


def is_blocked(telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT is_blocked FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row["is_blocked"]) if row else False


def get_setting(key, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


def get_commission_percent():
    val = get_setting("commission_percent", "10")
    try:
        return float(val)
    except (TypeError, ValueError):
        return 10.0


def get_min_balance():
    """Minimum driver wallet balance required to go online / accept a ride.
    Admin-configurable (settings page); default 500 ₸ matches the recommended MVP policy."""
    val = get_setting("min_balance_to_accept", "500")
    try:
        return float(val)
    except (TypeError, ValueError):
        return 500.0


def set_min_balance(value):
    set_setting("min_balance_to_accept", str(value))


def get_total_commission():
    """Real total commission actually deducted (ledger-based), not an estimate."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(-amount),0) as total FROM transactions WHERE type='commission' AND status='completed'")
    row = cur.fetchone()
    conn.close()
    return round((row["total"] if row else 0) or 0, 2)


# ---------- Driver balance & transactions (InDrive-style commission wallet) ----------

def get_balance(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return round((row["balance"] if row else 0) or 0, 2)


def _apply_balance(cur, user_id, amount):
    cur.execute("UPDATE users SET balance = COALESCE(balance,0) + ? WHERE id=?", (amount, user_id))


def record_commission(driver_id, ride_id, amount):
    """Deducts `amount` (positive number) from the driver's balance the moment a ride
    is completed, and logs it as a 'commission' transaction for history/reporting."""
    with db_cursor() as (conn, cur):
        _apply_balance(cur, driver_id, -abs(amount))
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, ride_id, status, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (driver_id, "commission", -abs(amount), ride_id, "completed", "Сапар комиссиясы", datetime.now().isoformat()),
        )
        conn.commit()


def request_topup(user_id, amount, note=None):
    """Driver asks to add money to their balance. Stays 'pending' until admin approves
    (Phase 1: cash/Kaspi paid outside the bot, admin just confirms receipt)."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, status, note, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, "topup", abs(amount), "pending", note, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def request_withdrawal(user_id, amount, note=None):
    """Driver asks to withdraw money. Balance is only debited once admin approves."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, status, note, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, "withdrawal", -abs(amount), "pending", note, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_transaction(tx_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def approve_transaction(tx_id):
    """Applies a pending topup/withdrawal to the driver's balance and marks it completed.

    Two safety fixes vs the old version:
    1. Atomic claim (UPDATE ... WHERE status='pending' first) — if the admin double-taps
       "Растау" (or Telegram redelivers the callback), only the FIRST call can ever win;
       the second sees rowcount=0 and does nothing, so the balance is never double-applied.
    2. For withdrawals, the driver's balance is re-checked at approval time (not just at
       request time) — if several withdrawal requests together now exceed the balance,
       approval is refused instead of driving the balance deeply negative.
    """
    with db_cursor() as (conn, cur):
        cur.execute("UPDATE transactions SET status='completed' WHERE id=? AND status='pending'", (tx_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return None  # already handled by someone else, or doesn't exist

        cur.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
        tx = dict(cur.fetchone())

        if tx["type"] == "withdrawal":
            cur.execute("SELECT balance FROM users WHERE id=?", (tx["user_id"],))
            bal_row = cur.fetchone()
            current_balance = (bal_row["balance"] if bal_row else 0) or 0
            if current_balance + tx["amount"] < -0.01:  # tx["amount"] is already negative
                conn.rollback()  # undoes the 'completed' claim above too — stays 'pending'
                tx["insufficient_balance"] = True
                tx["current_balance"] = round(current_balance, 2)
                return tx

        _apply_balance(cur, tx["user_id"], tx["amount"])
        conn.commit()
        return tx


def reject_transaction(tx_id, reason=None):
    with db_cursor() as (conn, cur):
        note = f"Бас тартылды: {reason}" if reason else "Бас тартылды"
        cur.execute("UPDATE transactions SET status='rejected', note=? WHERE id=? AND status='pending'", (note, tx_id))
        changed = cur.rowcount > 0
        conn.commit()
        return changed


def get_pending_transactions(tx_type=None):
    conn = get_conn()
    cur = conn.cursor()
    if tx_type:
        cur.execute(
            """SELECT t.*, u.full_name, u.username, u.phone, u.telegram_id
               FROM transactions t JOIN users u ON t.user_id = u.id
               WHERE t.status='pending' AND t.type=? ORDER BY t.created_at ASC""",
            (tx_type,),
        )
    else:
        cur.execute(
            """SELECT t.*, u.full_name, u.username, u.phone, u.telegram_id
               FROM transactions t JOIN users u ON t.user_id = u.id
               WHERE t.status='pending' ORDER BY t.created_at ASC"""
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_user_transactions(user_id, limit=20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def adjust_commission_percent(delta):
    """Nudges the commission percent by `delta` (can be negative), clamped 0-100.
    Used by the [-]/[+] buttons in the admin panel."""
    current = get_commission_percent()
    new_val = max(0, min(100, current + delta))
    set_setting("commission_percent", str(new_val))
    return new_val


def get_broadcast_recipients(role_filter=None):
    """role_filter: None (everyone), 'client', or 'driver'. Excludes blocked users."""
    conn = get_conn()
    cur = conn.cursor()
    if role_filter:
        cur.execute("SELECT telegram_id FROM users WHERE role=? AND is_blocked=0", (role_filter,))
    else:
        cur.execute("SELECT telegram_id FROM users WHERE is_blocked=0")
    rows = [r["telegram_id"] for r in cur.fetchall()]
    conn.close()
    return rows


def get_driver_active_ride(driver_id):
    """Return the driver's current accepted/arrived ride, if any."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM rides WHERE driver_id=? AND status IN ('accepted','arrived') ORDER BY created_at DESC LIMIT 1",
        (driver_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_client_active_ride(client_id):
    """Return the passenger's current accepted/arrived ride, if any (for the in-bot chat relay)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM rides WHERE client_id=? AND status IN ('accepted','arrived') ORDER BY created_at DESC LIMIT 1",
        (client_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_driver_earnings(driver_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT price FROM rides WHERE driver_id=? AND status='completed'", (driver_id,))
    rows = cur.fetchall()
    conn.close()
    total = 0
    count = 0
    for r in rows:
        count += 1
        digits = "".join(ch for ch in (r["price"] or "") if ch.isdigit())
        if digits:
            total += int(digits)
    return {"total": total, "count": count}


def cancel_ride(ride_id):
    """Force-cancel, used by admin panel."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE rides SET status='cancelled' WHERE id=?", (ride_id,))
    conn.commit()
    conn.close()


def cancel_ride_by_client(ride_id, client_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, client_id FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    if row is None or row["client_id"] != client_id or row["status"] not in ("open", "accepted"):
        conn.close()
        return False
    cur.execute("UPDATE rides SET status='cancelled' WHERE id=?", (ride_id,))
    conn.commit()
    conn.close()
    return True


def get_user_by_id(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_user(user_id):
    """Deletes the user together with their rides and support messages
    (as client and as driver), per admin panel 'delete' action."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM support_messages WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM rides WHERE client_id=? OR driver_id=?", (user_id, user_id))
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def get_all_rides(limit=200):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT rides.*, c.full_name as client_name, d.full_name as driver_name
           FROM rides
           LEFT JOIN users c ON rides.client_id = c.id
           LEFT JOIN users d ON rides.driver_id = d.id
           ORDER BY rides.created_at DESC LIMIT ?""",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_all_users(limit=200):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_stats():
    conn = get_conn()
    cur = conn.cursor()
    stats = {}
    cur.execute("SELECT COUNT(*) as c FROM users")
    stats["total_users"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role='driver'")
    stats["total_drivers"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role='driver' AND is_online=1")
    stats["online_drivers"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role='client'")
    stats["total_clients"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM rides")
    stats["total_rides"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM rides WHERE status='open'")
    stats["open_rides"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM rides WHERE status='completed'")
    stats["completed_rides"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM rides WHERE service_type='delivery'")
    stats["total_deliveries"] = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM rides WHERE route='intercity'")
    stats["total_intercity"] = cur.fetchone()["c"]
    cur.execute("SELECT AVG(rating) as a, COUNT(rating) as c FROM rides WHERE rating IS NOT NULL")
    row = cur.fetchone()
    stats["avg_rating"] = round(row["a"], 1) if row["a"] else None
    stats["rating_count"] = row["c"]
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role='driver' AND verification_status='pending'")
    stats["pending_drivers"] = cur.fetchone()["c"]
    conn.close()
    return stats


# ---------- Driver vehicle registration & verification ----------

def set_driver_vehicle(telegram_id, brand=None, model=None, plate=None, color=None, year=None, photo=None):
    """Saves vehicle fields. Verification stays 'pending' only once the full
    registration flow (ID photo, car doc photo, city, rules agreement) is done —
    see finalize_driver_registration()."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE users SET car_brand=?, car_model=?, car_plate=?, car_color=?, car_year=?, car_photo=?,
           reject_reason=NULL WHERE telegram_id=?""",
        (brand, model, plate, color, year, photo, telegram_id),
    )
    conn.commit()
    conn.close()


def set_id_photo(telegram_id, file_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET id_photo=? WHERE telegram_id=?", (file_id, telegram_id))
    conn.commit()
    conn.close()


def set_car_doc_photo(telegram_id, file_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET car_doc_photo=? WHERE telegram_id=?", (file_id, telegram_id))
    conn.commit()
    conn.close()


def set_work_city(telegram_id, city):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET work_city=? WHERE telegram_id=?", (city, telegram_id))
    conn.commit()
    conn.close()


def finalize_driver_registration(telegram_id):
    """Called after the driver agrees to the rules — the last registration step.
    Marks the profile 'pending' so it shows up for admin verification."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET agreed_rules=1, verification_status='pending', reject_reason=NULL WHERE telegram_id=?",
        (telegram_id,),
    )
    conn.commit()
    conn.close()


# ---------- Anti-fraud: one phone number = one account ----------

def _normalize_phone(phone):
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    # Kazakhstan numbers: normalize leading 8 -> 7 so "8701.." and "+7701.." match
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def find_user_by_phone(phone, exclude_telegram_id=None):
    """Returns another user row that already owns this phone number, or None.
    Used to stop one person opening several bot accounts with the same number."""
    norm = _normalize_phone(phone)
    if not norm:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE phone IS NOT NULL")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r["telegram_id"] == exclude_telegram_id:
            continue
        if _normalize_phone(r.get("phone")) == norm:
            return r
    return None


def set_fraud_flag(telegram_id, value=True):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET fraud_flag=? WHERE telegram_id=?", (1 if value else 0, telegram_id))
    conn.commit()
    conn.close()


def set_verification_status(user_id, status, reason=None):
    """status: 'approved' or 'rejected'."""
    conn = get_conn()
    cur = conn.cursor()
    if status == "rejected":
        # force offline if rejected after being verified
        cur.execute("UPDATE users SET verification_status=?, reject_reason=?, is_online=0 WHERE id=?", (status, reason, user_id))
    else:
        cur.execute("UPDATE users SET verification_status=?, reject_reason=NULL WHERE id=?", (status, user_id))
    conn.commit()
    conn.close()


def is_driver_verified(telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT verification_status FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row) and row["verification_status"] == "approved"


def get_pending_drivers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE role='driver' AND verification_status='pending' ORDER BY created_at")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_all_drivers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE role='driver' ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def vehicle_str(user: dict) -> str:
    parts = [p for p in [user.get("car_brand"), user.get("car_model")] if p]
    line = " ".join(parts) if parts else "белгісіз көлік"
    extra = []
    if user.get("car_color"):
        extra.append(user["car_color"])
    if user.get("car_year"):
        extra.append(str(user["car_year"]))
    if extra:
        line += f" ({', '.join(extra)})"
    if user.get("car_plate"):
        line += f" | 🔢 {user['car_plate']}"
    return line


# ---------- Cities / districts ----------

def add_city(name):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO cities (name, is_active, created_at) VALUES (?,1,?)", (name, datetime.now().isoformat()))
        conn.commit()
        ok = True
    except IntegrityError:
        conn.rollback()
        ok = False
    conn.close()
    return ok


def get_cities(active_only=False):
    conn = get_conn()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM cities WHERE is_active=1 ORDER BY name")
    else:
        cur.execute("SELECT * FROM cities ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def set_city_active(city_id, active):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE cities SET is_active=? WHERE id=?", (1 if active else 0, city_id))
    conn.commit()
    conn.close()


def delete_city(city_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM cities WHERE id=?", (city_id,))
    conn.commit()
    conn.close()


# ---------- Tariffs ----------

def set_tariff(route, base_price, price_per_km=None, note=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO tariffs (route, base_price, price_per_km, note) VALUES (?,?,?,?)
           ON CONFLICT(route) DO UPDATE SET base_price=excluded.base_price,
           price_per_km=excluded.price_per_km, note=excluded.note""",
        (route, base_price, price_per_km, note),
    )
    conn.commit()
    conn.close()


def get_tariff(route):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tariffs WHERE route=?", (route,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_tariffs():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tariffs")
    rows = {r["route"]: dict(r) for r in cur.fetchall()}
    conn.close()
    return rows


# ---------- Revenue (daily / monthly) ----------

def _sum_prices(rows):
    total = 0
    for r in rows:
        digits = "".join(ch for ch in (r["price"] or "") if ch.isdigit())
        if digits:
            total += int(digits)
    return total


def get_revenue_for_period(start_iso, end_iso=None):
    conn = get_conn()
    cur = conn.cursor()
    if end_iso:
        cur.execute(
            "SELECT price FROM rides WHERE status='completed' AND created_at>=? AND created_at<?",
            (start_iso, end_iso),
        )
    else:
        cur.execute("SELECT price FROM rides WHERE status='completed' AND created_at>=?", (start_iso,))
    rows = cur.fetchall()
    conn.close()
    total = _sum_prices(rows)
    percent = get_commission_percent()
    return {
        "turnover": total,
        "commission": round(total * percent / 100, 2),
        "rides": len(rows),
    }


def get_daily_revenue():
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return get_revenue_for_period(start)


def get_monthly_revenue():
    start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return get_revenue_for_period(start)


def get_revenue_by_day(days=7):
    """Completed-ride turnover for each of the last `days` days (oldest -> newest),
    used to draw the dashboard revenue chart."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT price, created_at FROM rides WHERE status='completed' AND created_at>=?",
        (start.isoformat(),),
    )
    rows = cur.fetchall()
    conn.close()
    buckets = {(start + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)}
    for r in rows:
        day_key = (r["created_at"] or "")[:10]
        if day_key in buckets:
            digits = "".join(ch for ch in (r["price"] or "") if ch.isdigit())
            if digits:
                buckets[day_key] += int(digits)
    return [{"day": k, "total": v} for k, v in buckets.items()]


def get_driver_daily_earnings(driver_id):
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT price FROM rides WHERE driver_id=? AND status='completed' AND created_at>=?",
        (driver_id, start),
    )
    rows = cur.fetchall()
    conn.close()
    return {"total": _sum_prices(rows), "count": len(rows)}


# ---------- Driver location (for nearby orders / live tracking) ----------

def set_driver_location(telegram_id, lat, lon):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_lat=?, last_lon=? WHERE telegram_id=?", (lat, lon, telegram_id))
    conn.commit()
    conn.close()


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def get_open_rides_near(lat, lon, limit=20):
    """Open rides sorted by distance from (lat, lon). Rides without coordinates go last."""
    rides = get_open_rides(limit=100)
    near, far = [], []
    for r in rides:
        if r.get("from_lat") is not None and r.get("from_lon") is not None:
            r["_distance_km"] = round(_haversine_km(lat, lon, r["from_lat"], r["from_lon"]), 1)
            near.append(r)
        else:
            r["_distance_km"] = None
            far.append(r)
    near.sort(key=lambda x: x["_distance_km"])
    return (near + far)[:limit]


# ---------- Order decline (with reason) & transfer to another driver ----------

def decline_ride(ride_id, driver_id, reason=None):
    """Driver who had accepted the ride gives it up -> back to 'open', reason logged."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, driver_id FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    if row is None or row["driver_id"] != driver_id or row["status"] not in ("accepted", "arrived"):
        conn.close()
        return False
    cur.execute(
        "UPDATE rides SET status='open', driver_id=NULL, decline_reason=? WHERE id=?",
        (reason, ride_id),
    )
    conn.commit()
    conn.close()
    return True


def transfer_ride(ride_id, from_driver_id, to_driver_id):
    """Hand an active ride off to another driver directly."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, driver_id FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    if row is None or row["driver_id"] != from_driver_id or row["status"] not in ("accepted", "arrived"):
        conn.close()
        return False
    cur.execute("UPDATE rides SET driver_id=?, status='accepted' WHERE id=?", (to_driver_id, ride_id))
    conn.commit()
    conn.close()
    return True


def get_other_online_drivers(exclude_driver_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE role='driver' AND is_online=1 AND verification_status='approved' AND id!=?",
        (exclude_driver_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------- Price offers (driver counter-offer on an open ride) ----------
# Rebuilt: the old version stored ONE offered_price column on the ride itself, so a
# second driver's offer silently overwrote the first driver's, and accepting an offer
# never actually assigned a driver to the ride (it only changed the price!). Now every
# offer is its own row, and accepting one atomically assigns THAT driver + auto-rejects
# every other pending offer on the same ride.

def create_offer(ride_id, driver_id, price):
    """Records/updates this driver's counter-offer for this ride. One pending offer
    per (ride, driver) — a repeat offer just updates the price instead of stacking."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id FROM ride_offers WHERE ride_id=? AND driver_id=? AND status='pending'",
            (ride_id, driver_id),
        )
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE ride_offers SET price=?, created_at=? WHERE id=?",
                        (price, datetime.now().isoformat(), row["id"]))
            offer_id = row["id"]
        else:
            cur.execute(
                "INSERT INTO ride_offers (ride_id, driver_id, price, status, created_at) VALUES (?,?,?, 'pending', ?)",
                (ride_id, driver_id, price, datetime.now().isoformat()),
            )
            offer_id = cur.lastrowid
        conn.commit()
        return offer_id


def get_offer(offer_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ride_offers WHERE id=?", (offer_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_offers_for_ride(ride_id, status="pending"):
    conn = get_conn()
    cur = conn.cursor()
    if status:
        cur.execute(
            """SELECT ro.*, u.full_name, u.phone FROM ride_offers ro JOIN users u ON ro.driver_id = u.id
               WHERE ro.ride_id=? AND ro.status=? ORDER BY ro.created_at ASC""",
            (ride_id, status),
        )
    else:
        cur.execute(
            """SELECT ro.*, u.full_name, u.phone FROM ride_offers ro JOIN users u ON ro.driver_id = u.id
               WHERE ro.ride_id=? ORDER BY ro.created_at ASC""",
            (ride_id,),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def accept_offer(offer_id):
    """Atomically: claim the ride for this offer's driver (only if still 'open'),
    accept this offer, and reject every other pending offer on the same ride.
    Returns the ride_offers row (with 'ride' and 'other_driver_ids' attached) on
    success, or None if the offer/ride is no longer available."""
    with db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM ride_offers WHERE id=? AND status='pending'", (offer_id,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        offer = dict(row)

        cur.execute(
            "UPDATE rides SET status='accepted', driver_id=?, price=? WHERE id=? AND status='open'",
            (offer["driver_id"], offer["price"], offer["ride_id"]),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None  # ride was taken/cancelled in the meantime

        cur.execute("UPDATE ride_offers SET status='accepted' WHERE id=?", (offer_id,))
        cur.execute(
            "SELECT driver_id FROM ride_offers WHERE ride_id=? AND status='pending' AND id!=?",
            (offer["ride_id"], offer_id),
        )
        other_driver_ids = [r["driver_id"] for r in cur.fetchall()]
        cur.execute(
            "UPDATE ride_offers SET status='rejected' WHERE ride_id=? AND status='pending' AND id!=?",
            (offer["ride_id"], offer_id),
        )
        conn.commit()
        offer["other_driver_ids"] = other_driver_ids
        return offer


def reject_offer(offer_id):
    with db_cursor() as (conn, cur):
        cur.execute("UPDATE ride_offers SET status='rejected' WHERE id=? AND status='pending'", (offer_id,))
        changed = cur.rowcount > 0
        conn.commit()
        return changed


def cancel_offers_for_ride(ride_id):
    """Called when a ride is claimed some other way (direct accept, client cancel) so
    every driver who made a pending counter-offer can be told it's no longer available."""
    with db_cursor() as (conn, cur):
        cur.execute("SELECT driver_id FROM ride_offers WHERE ride_id=? AND status='pending'", (ride_id,))
        driver_ids = [r["driver_id"] for r in cur.fetchall()]
        cur.execute("UPDATE ride_offers SET status='cancelled' WHERE ride_id=? AND status='pending'", (ride_id,))
        conn.commit()
        return driver_ids


# ---------- Fare estimate (uses admin-set tariffs + haversine distance) ----------

def estimate_price(route, distance_km):
    """Returns an int tenge estimate using the admin's tariff, or None if not enough data."""
    tariff = get_tariff(route)
    if not tariff or distance_km is None:
        return None
    try:
        base = float("".join(ch for ch in (tariff.get("base_price") or "0") if (ch.isdigit() or ch == ".")) or 0)
        per_km = float("".join(ch for ch in (tariff.get("price_per_km") or "0") if (ch.isdigit() or ch == ".")) or 0)
    except ValueError:
        return None
    if base == 0 and per_km == 0:
        return None
    return round(base + per_km * distance_km)


# ---------- Driver rates client (mutual rating) ----------

def rate_client(ride_id, driver_id, rating):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, driver_id, client_rating FROM rides WHERE id=?", (ride_id,))
    row = cur.fetchone()
    if row is None or row["driver_id"] != driver_id or row["status"] != "completed" or row["client_rating"] is not None:
        conn.close()
        return False
    cur.execute("UPDATE rides SET client_rating=? WHERE id=?", (rating, ride_id))
    conn.commit()
    conn.close()
    return True


def get_client_rating(client_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT client_rating FROM rides WHERE client_id=? AND client_rating IS NOT NULL", (client_id,))
    rows = [r["client_rating"] for r in cur.fetchall()]
    conn.close()
    if not rows:
        return {"avg": None, "count": 0}
    return {"avg": round(sum(rows) / len(rows), 1), "count": len(rows)}


# ---------- Support / help messages (client or driver <-> admin) ----------

def add_support_message(user_id, direction, text):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO support_messages (user_id, direction, text, created_at) VALUES (?,?,?,?)",
        (user_id, direction, text, datetime.now().isoformat()),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def get_support_threads():
    """One row per user who has any support message, with their latest message + unread count."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT u.id as user_id, u.full_name, u.username, u.phone, u.telegram_id, u.role,
                  MAX(s.created_at) as last_at,
                  SUM(CASE WHEN s.direction='in' AND s.is_read=0 THEN 1 ELSE 0 END) as unread
           FROM support_messages s JOIN users u ON s.user_id = u.id
           GROUP BY u.id ORDER BY last_at DESC"""
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_support_messages(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_messages WHERE user_id=? ORDER BY created_at ASC", (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def mark_support_read(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE support_messages SET is_read=1 WHERE user_id=? AND direction='in'", (user_id,))
    conn.commit()
    conn.close()
