#!/usr/bin/env python3
"""
Portable DB helper for Atikur Pay.

- Local dev: SQLite (file at ATIKUR_PAY_DB or ./atikur_pay.db).
- Production: Postgres when DATABASE_URL is set (psycopg2).
- All SQL is written with `?` placeholders; they are converted to
  `%s` for Postgres. All primary keys are TEXT uuids (portable).
- Timestamps are set in Python (ISO strings) so no DB-specific
  defaults are needed.

Payment model (SMS-based TrxID verification, like UdoktaPay manual
mode — NO bKash PGW API is used):
- merchants add their OWN bKash number; customers Send Money to it.
- An Android app on the merchant's phone parses bKash SMS and POSTs
  them to /api/sms. The server stores them in sms_transactions.
- At checkout the customer enters their phone + TrxID; the server
  matches TrxID + amount + sender against sms_transactions and marks
  the payment paid. Mark-paid is idempotent per TrxID.
"""
import os
import sqlite3
import uuid
from datetime import datetime

try:
    import psycopg2
    HAVE_PG = True
except ImportError:  # pragma: no cover
    HAVE_PG = False


def is_pg():
    return bool(os.environ.get("DATABASE_URL")) and HAVE_PG


def get_conn():
    """Return (conn, is_pg). Caller must close."""
    url = os.environ.get("DATABASE_URL")
    if url and HAVE_PG:
        conn = psycopg2.connect(url)
        # Isolate Atikur Pay tables in their own Postgres schema
        # (atikur_pay) so this service can share the SMM panel's
        # Postgres database without colliding on the `payments` and
        # `sms_transactions` table names, which both services use
        # with different columns.
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS atikur_pay")
        cur.execute("SET search_path TO atikur_pay")
        conn.commit()
        cur.close()
        return conn, True
    path = os.environ.get(
        "ATIKUR_PAY_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "atikur_pay.db"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, False


def adapt(sql, pg):
    if not pg:
        return sql
    # Convert ? placeholders to %s (our SQL never uses ? in literals).
    return sql.replace("?", "%s")


def fetchall(conn, pg, sql, params=()):
    cur = conn.cursor()
    cur.execute(adapt(sql, pg), params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        if pg:
            out.append(dict(zip(cols, r)))
        else:
            out.append(dict(r))
    return out


def fetchone(conn, pg, sql, params=()):
    rows = fetchall(conn, pg, sql, params)
    return rows[0] if rows else None


def execute(conn, pg, sql, params=()):
    cur = conn.cursor()
    cur.execute(adapt(sql, pg), params)
    cur.close()


def commit(conn):
    conn.commit()


def new_id():
    return uuid.uuid4().hex


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS merchants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        phone TEXT,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY,
        merchant_id TEXT NOT NULL,
        label TEXT,
        key_value TEXT NOT NULL UNIQUE,
        secret TEXT,
        created_at TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS merchant_methods (
        id TEXT PRIMARY KEY,
        merchant_id TEXT NOT NULL,
        method TEXT NOT NULL,
        credentials_json TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(merchant_id, method)
    )""",
    """CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        public_id TEXT NOT NULL UNIQUE,
        merchant_id TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'BDT',
        method TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        merchant_invoice_number TEXT,
        customer_ref TEXT,
        callback_url TEXT,
        trx_id TEXT,
        customer_phone TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sms_transactions (
        id TEXT PRIMARY KEY,
        merchant_id TEXT NOT NULL,
        method TEXT NOT NULL DEFAULT 'bkash',
        sender_phone TEXT NOT NULL,
        amount REAL NOT NULL,
        trx_id TEXT NOT NULL UNIQUE,
        sms_text TEXT,
        received_at TEXT,
        used INTEGER NOT NULL DEFAULT 0,
        used_by_payment_id TEXT,
        created_at TEXT NOT NULL
    )""",
]


def init_schema():
    conn, pg = get_conn()
    try:
        for stmt in SCHEMA:
            execute(conn, pg, stmt)
        commit(conn)
    finally:
        conn.close()


def normalize_phone(phone):
    """Normalize a Bangladeshi phone to 01XXXXXXXXX form."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("880"):
        digits = "0" + digits[3:]
    elif digits.startswith("88") and len(digits) == 13:
        digits = "0" + digits[2:]
    return digits


def normalize_trx(trx_id):
    return str(trx_id or "").strip().upper()
