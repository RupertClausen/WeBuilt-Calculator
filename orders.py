"""Orders: status workflow, saved files and history.

Every saved quote is an order. It moves through the statuses in
config/server.yaml (quoted → in queue → printing → done → delivered, or
cancelled), keeps the files it was quoted from plus any attachments, and
records who changed what in order_events.

Files live on disk under FILES_DIR/<order id>/ with generated names; the
original file name is kept in the database and used when downloading, so a
customer's file name can never decide where something is written.

Anything that changes an order calls notify(), which the Google Sheet mirror
listens to.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from core import FILES_DIR, config, db, utcnow

_listeners: list[Callable[[], None]] = []


class OrderError(Exception):
    """A message safe to show to the person who asked."""


# ---------------------------------------------------------------------------
# change notifications
# ---------------------------------------------------------------------------

def on_change(fn: Callable[[], None]) -> None:
    _listeners.append(fn)


def notify() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception:                 # a listener must never break an order
            pass


# ---------------------------------------------------------------------------
# statuses
# ---------------------------------------------------------------------------

def statuses() -> dict[str, dict]:
    return config["server"].get("statuses") or {"quoted": {"label": "Quoted"}}


def status_label(key: str, lang: str = "en") -> str:
    spec = statuses().get(key) or {}
    if lang == "th" and spec.get("label_th"):
        return spec["label_th"]
    return spec.get("label", key)


def closing_statuses() -> set[str]:
    return {k for k, v in statuses().items() if v.get("closes")}


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def add_event(conn: sqlite3.Connection, quote_id: int, user: str, action: str,
              detail: str | None = None) -> None:
    conn.execute("INSERT INTO order_events (quote_id, at, user, action, detail)"
                 " VALUES (?, ?, ?, ?, ?)", (quote_id, utcnow(), user, action, detail))


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

_SAFE_EXT = re.compile(r"(\.[A-Za-z0-9]{1,8}){1,2}$")


def _stored_name(index: int, original: str, digest: str) -> str:
    """A name decided by us, never by the upload: index, part of the hash,
    and the original extension if it looks like one (.gcode, .gcode.3mf)."""
    m = _SAFE_EXT.search(original or "")
    return f"{index:03d}-{digest[:16]}{m.group(0).lower() if m else ''}"


def order_dir(quote_id: int) -> Path:
    return FILES_DIR / str(int(quote_id))


def save_files(conn: sqlite3.Connection, quote_id: int,
               files: list[tuple[str, bytes]], user: str,
               kind: str = "sliced") -> list[dict]:
    """Write files for an order and record them. Called inside the caller's
    transaction: if it fails, the caller rolls back and remove_order_dir()
    cleans up whatever was written."""
    if not files:
        return []
    folder = order_dir(quote_id)
    folder.mkdir(parents=True, exist_ok=True)
    start = conn.execute("SELECT COUNT(*) FROM order_files WHERE quote_id = ?",
                         (quote_id,)).fetchone()[0]
    saved = []
    for i, (name, data) in enumerate(files, start=start + 1):
        digest = hashlib.sha256(data).hexdigest()
        stored = _stored_name(i, name, digest)
        (folder / stored).write_bytes(data)
        clean = Path((name or f"file-{i}").replace("\\", "/")).name[:200] or f"file-{i}"
        cur = conn.execute(
            "INSERT INTO order_files (quote_id, name, stored_as, size, sha256, kind,"
            " uploaded_at, uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (quote_id, clean, stored, len(data), digest, kind, utcnow(), user))
        saved.append({"id": cur.lastrowid, "name": clean, "size": len(data), "kind": kind})
    return saved


def remove_order_dir(quote_id: int) -> None:
    shutil.rmtree(order_dir(quote_id), ignore_errors=True)


def file_path(quote_id: int, stored_as: str) -> Path:
    p = (order_dir(quote_id) / stored_as).resolve()
    if order_dir(quote_id).resolve() not in p.parents:      # belt and braces
        raise OrderError("Bad file reference.")
    return p


def attach_files(quote_id: int, files: list[tuple[str, bytes]], user: str) -> list[dict]:
    limit = int(config["server"].get("files", {}).get("max_attachment_mb", 200)) * 1024 * 1024
    if sum(len(d) for _, d in files) > limit:
        raise OrderError(f"Attachments are limited to {limit // (1024 * 1024)} MB at a time.")
    try:
        with db() as conn:
            _require(conn, quote_id)
            saved = save_files(conn, quote_id, files, user, kind="attachment")
            add_event(conn, quote_id, user, "attached",
                      ", ".join(f["name"] for f in saved))
            conn.execute("UPDATE quotes SET updated_at = ? WHERE id = ?", (utcnow(), quote_id))
    except Exception:
        # Only remove files this call wrote; earlier files stay.
        with db() as conn:
            known = {r["stored_as"] for r in conn.execute(
                "SELECT stored_as FROM order_files WHERE quote_id = ?", (quote_id,))}
        for p in order_dir(quote_id).glob("*"):
            if p.name not in known:
                p.unlink(missing_ok=True)
        raise
    notify()
    return saved


# ---------------------------------------------------------------------------
# reading and changing orders
# ---------------------------------------------------------------------------

LIST_COLUMNS = ("id, receipt_no, created_at, updated_at, customer_name, customer_contact,"
                " filename, printer, materials, grams, hours, plates, total, currency,"
                " payment_status, payment_method, paid_at, status, created_by, handled_by,"
                " delivery, rush, discount, discount_label, est_margin, p_success, outcome,"
                " labour, contingency, profit, app_version, closed_at")


def _require(conn, quote_id: int):
    row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        raise OrderError(f"No order {quote_id}.")
    return row


def list_orders(status: str | None = None, q: str | None = None,
                limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    if status == "open":
        closing = sorted(closing_statuses())
        if closing:
            where.append(f"status NOT IN ({','.join('?' * len(closing))})")
            args += closing
    elif status:
        where.append("status = ?")
        args.append(status)
    if q:
        like = f"%{q.strip()}%"
        where.append("(receipt_no LIKE ? OR customer_name LIKE ? OR customer_contact LIKE ?"
                     " OR filename LIKE ? OR notes LIKE ?)")
        args += [like] * 5
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM quotes{clause}", args).fetchone()[0]
        rows = conn.execute(f"SELECT {LIST_COLUMNS} FROM quotes{clause}"
                            " ORDER BY id DESC LIMIT ? OFFSET ?",
                            args + [min(int(limit), 500), max(int(offset), 0)]).fetchall()
    return [dict(r) for r in rows], total


def get_order(quote_id: int) -> dict:
    with db() as conn:
        row = _require(conn, quote_id)
        files = [dict(r) for r in conn.execute(
            "SELECT id, name, size, kind, uploaded_at, uploaded_by, deleted_at"
            " FROM order_files WHERE quote_id = ? ORDER BY id", (quote_id,))]
        events = [dict(r) for r in conn.execute(
            "SELECT at, user, action, detail FROM order_events WHERE quote_id = ?"
            " ORDER BY id", (quote_id,))]
        plates = [r["plate"] for r in conn.execute(
            "SELECT plate FROM plate_thumbnails WHERE quote_id = ? ORDER BY plate",
            (quote_id,))]
    order = dict(row)
    detail = json.loads(order.pop("detail_json") or "{}")
    order["quote"] = {"lines": detail.get("lines", []), "warnings": detail.get("warnings", []),
                      "plates": (detail.get("breakdown") or {}).get("plates", []),
                      "economics": (detail.get("breakdown") or {}).get("economics", {})}
    order["files"] = files
    order["events"] = events
    order["thumbnails"] = plates
    return order


EDITABLE = ("status", "handled_by", "notes", "customer_name", "customer_contact")


def update_order(quote_id: int, changes: dict, user: str) -> dict:
    changes = {k: v for k, v in changes.items() if k in EDITABLE and v is not None}
    if not changes:
        raise OrderError("Nothing to change.")
    with db() as conn:
        row = _require(conn, quote_id)
        if "status" in changes:
            new = changes["status"]
            if new not in statuses():
                raise OrderError(f"Unknown status '{new}'.")
            if new != row["status"]:
                closing = closing_statuses()
                # The retention clock starts the first time it closes, and
                # stops if the order is reopened.
                if new in closing and not row["closed_at"]:
                    conn.execute("UPDATE quotes SET closed_at = ? WHERE id = ?",
                                 (utcnow(), quote_id))
                elif new not in closing:
                    conn.execute("UPDATE quotes SET closed_at = NULL WHERE id = ?", (quote_id,))
                add_event(conn, quote_id, user, "status",
                          f"{status_label(row['status'])} → {status_label(new)}")
        for key in ("handled_by", "notes", "customer_name", "customer_contact"):
            if key in changes and (changes[key] or None) != (row[key] or None):
                value = str(changes[key]).strip()[:500] or None
                changes[key] = value
                add_event(conn, quote_id, user, key.replace("_", " "),
                          value if key != "notes" else "updated")
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE quotes SET {sets}, updated_at = ? WHERE id = ?",
                     [changes[k] for k in changes] + [utcnow(), quote_id])
    notify()
    return get_order(quote_id)


def record_payment(quote_id: int, status: str, method: str | None, user: str) -> str | None:
    """Returns paid_at (or None when marked unpaid)."""
    paid_at = utcnow() if status == "paid" else None
    with db() as conn:
        _require(conn, quote_id)
        conn.execute("UPDATE quotes SET payment_status = ?, payment_method = ?, paid_at = ?,"
                     " updated_at = ? WHERE id = ?",
                     (status, method if status == "paid" else None, paid_at, utcnow(),
                      quote_id))
        add_event(conn, quote_id, user, "payment",
                  f"paid ({method})" if status == "paid" and method else status)
    notify()
    return paid_at


def delete_order(quote_id: int, user: str, confirm: str) -> dict:
    """Delete an order for good: the row, its files, plate pictures and
    history. `confirm` must be its receipt number, so nothing is deleted by
    accident. A short record is kept in deleted_orders, and the receipt
    number is never issued again."""
    with db() as conn:
        row = _require(conn, quote_id)
        expected = row["receipt_no"] or str(quote_id)
        if (confirm or "").strip() != expected:
            raise OrderError(f"To delete it, confirm with its receipt number {expected}.")
        summary = {k: row[k] for k in ("customer_name", "customer_contact", "total",
                                        "currency", "status", "payment_status",
                                        "payment_method", "created_at", "created_by",
                                        "filename")}
        conn.execute(
            "INSERT INTO deleted_orders (quote_id, receipt_no, receipt_auto, deleted_at,"
            " deleted_by, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (quote_id, row["receipt_no"], row["receipt_auto"], utcnow(), user,
             json.dumps(summary, ensure_ascii=False)))
        # plate_thumbnails, order_files and order_events go with it (CASCADE)
        conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
    remove_order_dir(quote_id)
    notify()
    return {"id": quote_id, "receipt_no": row["receipt_no"]}


def deleted_orders(limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM deleted_orders ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def delete_expired_files(keep_days: int, now: datetime | None = None) -> int:
    """Delete stored files of orders closed more than keep_days ago. The
    order, its prices and plate pictures stay. Returns files deleted."""
    if keep_days <= 0:
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
              ).isoformat(timespec="seconds")
    closing = sorted(closing_statuses())
    if not closing:
        return 0
    deleted = 0
    with db() as conn:
        rows = conn.execute(
            f"SELECT f.id, f.quote_id, f.stored_as FROM order_files f"
            f" JOIN quotes q ON q.id = f.quote_id"
            f" WHERE f.deleted_at IS NULL AND q.closed_at IS NOT NULL AND q.closed_at < ?"
            f" AND q.status IN ({','.join('?' * len(closing))})",
            [cutoff] + closing).fetchall()
        for r in rows:
            try:
                file_path(r["quote_id"], r["stored_as"]).unlink(missing_ok=True)
            except OrderError:
                continue
            conn.execute("UPDATE order_files SET deleted_at = ? WHERE id = ?",
                         (utcnow(), r["id"]))
            deleted += 1
        for qid in {r["quote_id"] for r in rows}:
            add_event(conn, qid, "system", "files removed",
                      f"kept {keep_days} days after the order closed")
            d = order_dir(qid)
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
    return deleted
