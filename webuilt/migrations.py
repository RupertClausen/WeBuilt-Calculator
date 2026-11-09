"""Numbered database migrations.

The schema version is SQLite's own `PRAGMA user_version`. Each step runs
once, in order, inside the caller's transaction; a database at version N
only runs steps after N. A v1 database (1.2 and earlier) is at version 0
with its tables already there, so step 1 is written to be idempotent.

To change the schema, ADD a step at the end. Never edit one that has
shipped: databases in the field have already run it.
"""

from __future__ import annotations

import sqlite3


def step_001_v1_schema(conn: sqlite3.Connection) -> None:
    """Everything up to release 1.2: the schema init_db used to create.
    Idempotent, so a v1 database (user_version 0, tables already there)
    passes through it unchanged."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            filename      TEXT,
            printer       TEXT,
            materials     TEXT,
            grams         REAL,
            hours         REAL,
            plates        INTEGER,
            rush          TEXT,
            delivery      TEXT,
            total         REAL,
            currency      TEXT,
            est_cost      REAL,
            est_margin    REAL,
            p_success     REAL,
            risk_factor   REAL,
            outcome       TEXT DEFAULT 'quoted',
            detail_json   TEXT
        )
        """
    )
    # `outcome` is here so you can mark a job completed/failed later and
    # back-fit each printer's real failure rate from your own history.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_created ON quotes(created_at)"
    )

    # Columns added for receipts. ALTER only what is missing, so an
    # existing orders.db upgrades in place.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(quotes)")}
    for col, ddl in (
        ("receipt_no", "TEXT"),
        ("customer_name", "TEXT"),
        ("customer_contact", "TEXT"),
        ("notes", "TEXT"),
        ("payment_status", "TEXT DEFAULT 'unpaid'"),
        ("payment_method", "TEXT"),
        ("paid_at", "TEXT"),
        # Where the money goes, so a month of orders can be summed.
        ("labour", "REAL"),
        ("contingency", "REAL"),
        ("profit", "REAL"),
        # Which release (and so which price list) produced the quote.
        ("app_version", "TEXT"),
        # The discount given and why; `profit` is what was kept after it.
        ("discount", "REAL"),
        ("discount_label", "TEXT"),
        # 1 when the app chose the receipt number, NULL when typed by
        # hand. Only app-chosen numbers move the automatic counter.
        ("receipt_auto", "INTEGER"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_receipt"
        " ON quotes(receipt_no) WHERE receipt_no IS NOT NULL"
    )
    # Plate pictures live apart from detail_json so the quote record
    # stays small. `plate` matches breakdown.plates[].plate.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plate_thumbnails (
            quote_id  INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            plate     INTEGER NOT NULL,
            mime      TEXT NOT NULL,
            data      BLOB NOT NULL,
            PRIMARY KEY (quote_id, plate)
        )
        """
    )

    # --- team server -----------------------------------------------------
    # Order workflow and who did what. Existing rows start as 'quoted'.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(quotes)")}
    for col, ddl in (
        ("status", "TEXT NOT NULL DEFAULT 'quoted'"),
        ("created_by", "TEXT"),
        ("handled_by", "TEXT"),
        ("updated_at", "TEXT"),
        ("closed_at", "TEXT"),     # when it reached a closing status
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status)")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name  TEXT,
            pw_hash       TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'member',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            last_login    TEXT,
            failed_logins INTEGER NOT NULL DEFAULT 0,
            locked_until  TEXT
        );
        -- Only a hash of each session token is stored, so a copy of the
        -- database can't be used to log in as anyone.
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash  TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            last_seen   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_files (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id     INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            stored_as    TEXT NOT NULL,
            size         INTEGER NOT NULL,
            sha256       TEXT NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'sliced',
            uploaded_at  TEXT NOT NULL,
            uploaded_by  TEXT,
            deleted_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_files_quote ON order_files(quote_id);
        CREATE TABLE IF NOT EXISTS order_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id  INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            at        TEXT NOT NULL,
            user      TEXT,
            action    TEXT NOT NULL,
            detail    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_quote ON order_events(quote_id);
        -- A record of every deleted order. Its receipt number stays
        -- reserved: a number a customer may already hold is never reused.
        CREATE TABLE IF NOT EXISTS deleted_orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id      INTEGER NOT NULL,
            receipt_no    TEXT,
            receipt_auto  INTEGER,
            deleted_at    TEXT NOT NULL,
            deleted_by    TEXT,
            summary       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deleted_receipt ON deleted_orders(receipt_no);
        -- Small key/value store: sheet sync status, last backup, etc.
        CREATE TABLE IF NOT EXISTS app_state (
            key    TEXT PRIMARY KEY,
            value  TEXT
        );
        """
    )



# v1 statuses → v2. One CASE so the mapping can't chain (done → ready must
# not then become ...). v1 "quoted" meant "price sent, not yet paid".
V1_STATUS_MAP = {"quoted": "awaiting_payment", "done": "ready", "delivered": "done"}


def step_002_v2_statuses(conn: sqlite3.Connection) -> None:
    """v1 used quoted → queued → printing → done → delivered. v2 adds review,
    payment and shipping steps; see config/server.yaml."""
    cases = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in V1_STATUS_MAP.items())
    conn.execute(f"UPDATE quotes SET status = CASE status {cases} ELSE status END"
                 f" WHERE status IN ({','.join('?' * len(V1_STATUS_MAP))})",
                 list(V1_STATUS_MAP))


def step_003_printers_and_queues(conn: sqlite3.Connection) -> None:
    """Printer categories are the keys of config/printers.yaml (normal,
    large…): quotes price by category. The physical machines in each
    category live here, and every plate of a paid order becomes a job that
    waits in its category's queue until a printer takes it."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS printers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            category    TEXT NOT NULL,
            model       TEXT,
            notes       TEXT,
            active      INTEGER NOT NULL DEFAULT 1,
            paused      INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        -- One row per plate of an order (copies run back to back on one
        -- printer). printer_id is the printer it is pinned to or running on;
        -- NULL means "next free printer in the category". A failed job stays
        -- as a record and a new waiting job is made for the reprint.
        CREATE TABLE IF NOT EXISTS plate_jobs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id     INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            plate        INTEGER NOT NULL,
            label        TEXT,
            copies       INTEGER NOT NULL DEFAULT 1,
            hours        REAL NOT NULL DEFAULT 0,
            grams        REAL NOT NULL DEFAULT 0,
            materials    TEXT,
            category     TEXT NOT NULL,
            printer_id   INTEGER REFERENCES printers(id) ON DELETE SET NULL,
            position     REAL NOT NULL,
            status       TEXT NOT NULL DEFAULT 'waiting',
            reprint_of   INTEGER REFERENCES plate_jobs(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL,
            started_at   TEXT,
            finished_at  TEXT,
            finished_by  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_queue ON plate_jobs(status, category, position);
        CREATE INDEX IF NOT EXISTS idx_jobs_quote ON plate_jobs(quote_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_printer ON plate_jobs(printer_id, status);
        """
    )


STEPS = [
    (1, step_001_v1_schema),
    (2, step_002_v2_statuses),
    (3, step_003_printers_and_queues),
]

LATEST = STEPS[-1][0]


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def run(conn: sqlite3.Connection) -> list[int]:
    """Apply every step newer than the database. Returns the steps run.

    A database NEWER than this code (someone rolled the app back) is left
    alone and refused: running old code on a newer schema can lose data.
    """
    current = version(conn)
    if current > LATEST:
        raise RuntimeError(
            f"orders.db is at schema {current}, newer than this version of the app "
            f"understands ({LATEST}). Update the app, or restore a backup taken "
            "before the upgrade.")
    ran = []
    # Steps read columns by name.
    saved_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        for n, step in STEPS:
            if n > current:
                step(conn)
                # user_version can't take a bound parameter; n is our own int.
                conn.execute(f"PRAGMA user_version = {int(n)}")
                ran.append(n)
    finally:
        conn.row_factory = saved_factory
    return ran
