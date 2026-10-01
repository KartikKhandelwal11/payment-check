import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
-- Receiving accounts ("members" for historical reasons): one bank account +
-- the phone that gets its SMS and UPI app alerts.
CREATE TABLE IF NOT EXISTS members (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    upi_id      TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    last_seen   INTEGER,
    created_at  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | paused | closed
    closed_at   INTEGER,
    bank_account TEXT,
    ifsc        TEXT,
    last_assigned_at INTEGER
);

CREATE TABLE IF NOT EXISTS customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    balance_paise INTEGER NOT NULL DEFAULT 0,
    paused_until  INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    member_id     TEXT NOT NULL REFERENCES members(id),
    base_paise    INTEGER NOT NULL,
    amount_paise  INTEGER NOT NULL,
    note          TEXT,
    -- pending | expired | action | in_progress | review | paid | failed | cancelled
    status        TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    paid_at       INTEGER,
    payment_id    INTEGER,
    customer_id   INTEGER REFERENCES customers(id),
    method        TEXT NOT NULL DEFAULT 'upi',    -- upi | bank
    upload_until  INTEGER,
    review_reason TEXT,
    customer_message TEXT,
    push_seen_at  INTEGER,
    credited_paise INTEGER,
    utr           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_lookup ON orders(member_id, amount_paise, status);

-- A customer's screenshot and what was read from it.
CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL REFERENCES orders(id),
    created_at    INTEGER NOT NULL,
    image_path    TEXT,
    image_hash    TEXT NOT NULL,
    ocr_text      TEXT,
    utr           TEXT,
    utr_options   TEXT,
    amount_paise  INTEGER,
    amounts_seen  TEXT,
    paid_to_member TEXT,
    shot_at       INTEGER,
    shot_has_time INTEGER NOT NULL DEFAULT 0,
    status_word   TEXT,
    note_order    TEXT,
    problem       TEXT,
    problem_text  TEXT,
    -- rejected | pick | ready | submitted | review | matched | failed | replaced
    state         TEXT NOT NULL,
    submitted_at  INTEGER,
    decided_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_order ON claims(order_id);
CREATE INDEX IF NOT EXISTS idx_claims_utr ON claims(utr);
CREATE INDEX IF NOT EXISTS idx_claims_hash ON claims(image_hash);
-- A UTR can be credited once, ever.
CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_utr_matched ON claims(utr) WHERE state = 'matched';

-- Money the bank says arrived: from an HDFC SMS, a statement, or entered by an
-- admin after checking the bank. The proof side of every credit.
CREATE TABLE IF NOT EXISTS bank_credits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id     TEXT NOT NULL REFERENCES members(id),
    utr           TEXT,
    amount_paise  INTEGER NOT NULL,
    sender        TEXT,
    source        TEXT NOT NULL,                  -- sms | statement | admin | demo
    received_at   INTEGER NOT NULL,
    raw           TEXT,
    notification_id INTEGER,
    statement_id  INTEGER,
    in_statement  INTEGER NOT NULL DEFAULT 0,
    order_id      TEXT REFERENCES orders(id),
    status        TEXT NOT NULL                   -- unmatched | credited | no_utr
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_credits_utr ON bank_credits(utr) WHERE utr IS NOT NULL;

CREATE TABLE IF NOT EXISTS statements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id     TEXT NOT NULL REFERENCES members(id),
    uploaded_at   INTEGER NOT NULL,
    filename      TEXT,
    period_start  INTEGER,
    period_end    INTEGER,
    rows          INTEGER NOT NULL,
    credits       INTEGER NOT NULL,
    report        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    order_id      TEXT NOT NULL UNIQUE REFERENCES orders(id),
    amount_paise  INTEGER NOT NULL,
    utr           TEXT NOT NULL,
    source        TEXT NOT NULL,                  -- auto | admin
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  TEXT NOT NULL REFERENCES orders(id),
    at        INTEGER NOT NULL,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);

-- Things an admin must look at (shown on the dashboard).
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL,
    order_id  TEXT,
    member_id TEXT,
    resolved  INTEGER NOT NULL DEFAULT 0
);

-- Every notification and SMS the phones forward, kept so the parsers can be checked.
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id    TEXT NOT NULL REFERENCES members(id),
    package      TEXT,
    title        TEXT,
    text         TEXT,
    notif_key    TEXT,
    received_at  INTEGER NOT NULL,
    result       TEXT NOT NULL            -- payment | ignored | duplicate | sms | sms_rejected
);
CREATE INDEX IF NOT EXISTS idx_notifications_member ON notifications(member_id, received_at);

-- UPI app alerts (PhonePe / Paytm / GPay). They have no UTR, so they never
-- credit money; they only tell the customer "payment received, confirming".
CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id        TEXT NOT NULL REFERENCES members(id),
    notification_id  INTEGER NOT NULL REFERENCES notifications(id),
    amount_paise     INTEGER NOT NULL,
    payer_name       TEXT,
    received_at      INTEGER NOT NULL,
    status           TEXT NOT NULL,       -- signal (older rows: matched | unmatched | approved)
    order_id         TEXT REFERENCES orders(id)
);
"""

# Columns added after the first release; added to older databases on start.
NEW_COLUMNS = {
    "members": {
        "status": "TEXT NOT NULL DEFAULT 'active'", "closed_at": "INTEGER", "bank_account": "TEXT",
        "ifsc": "TEXT", "last_assigned_at": "INTEGER",
    },
    "orders": {
        "customer_id": "INTEGER REFERENCES customers(id)", "method": "TEXT NOT NULL DEFAULT 'upi'",
        "upload_until": "INTEGER", "review_reason": "TEXT", "customer_message": "TEXT",
        "push_seen_at": "INTEGER", "credited_paise": "INTEGER", "utr": "TEXT",
    },
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # Older databases: add new columns before the schema's indexes use them.
        for table, cols in NEW_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue
            for col, ddl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction():
    """A write transaction that locks the database, so two requests can never
    credit the same UTR at the same moment."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def reader():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
