"""Printer categories, the printers in them, and the plate queue.

* A **category** is a key of config/printers.yaml (normal, large…). Quotes
  are priced by category, exactly as before: its rates and failure model.
* A **printer** is one physical machine in a category (P2S #1, P2S #2…),
  added from the Printers page. Categories gain printers over time without
  any change to pricing.
* A **job** is one plate of an order (its copies run back to back). When an
  order joins the queue each plate becomes a waiting job in its category.
  It goes to the next free printer in that category, or an engineer pins it
  to one printer. Finished / failed is recorded per job, so failure rates
  can be measured per printer and per category.

Jobs move the order along: the first job to start moves a queued order to
Printing, and the last one to finish moves it to Ready.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core import config, db, utcnow

WAITING, PRINTING, DONE, FAILED, CANCELLED = "waiting", "printing", "done", "failed", "cancelled"
OPEN_JOB = (WAITING, PRINTING)


class QueueError(Exception):
    """A message safe to show to the person who asked."""


# ---------------------------------------------------------------------------
# categories and printers
# ---------------------------------------------------------------------------

def categories() -> dict[str, dict]:
    return config["printers"].get("printers") or {}


def fallback_category() -> str:
    cats = categories()
    fb = config["printers"].get("fallback_printer")
    return fb if fb in cats else next(iter(cats), "normal")


def _printer(conn, printer_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if row is None:
        raise QueueError(f"No printer {printer_id}.")
    return row


def add_printer(name: str, category: str, model: str | None = None,
                notes: str | None = None) -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise QueueError("Give the printer a name, e.g. “P2S #2”.")
    if category not in categories():
        raise QueueError(f"Unknown category '{category}'. Categories come from printers.yaml.")
    with db() as conn:
        if conn.execute("SELECT 1 FROM printers WHERE name = ?", (name,)).fetchone():
            raise QueueError(f"There's already a printer called “{name}”.")
        cur = conn.execute(
            "INSERT INTO printers (name, category, model, notes, created_at) VALUES (?,?,?,?,?)",
            (name, category, (model or "").strip()[:80] or None,
             (notes or "").strip()[:300] or None, utcnow()))
        return dict(_printer(conn, cur.lastrowid))


PRINTER_FIELDS = ("name", "category", "model", "notes", "active", "paused")


def update_printer(printer_id: int, changes: dict) -> dict:
    changes = {k: v for k, v in changes.items() if k in PRINTER_FIELDS and v is not None}
    if not changes:
        raise QueueError("Nothing to change.")
    with db() as conn:
        p = _printer(conn, printer_id)
        if "category" in changes:
            if changes["category"] not in categories():
                raise QueueError(f"Unknown category '{changes['category']}'.")
            if changes["category"] != p["category"] and conn.execute(
                    "SELECT 1 FROM plate_jobs WHERE printer_id = ? AND status IN (?, ?)",
                    (printer_id, *OPEN_JOB)).fetchone():
                raise QueueError("Finish or unpin this printer's jobs before moving it "
                                 "to another category.")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()[:60]
            if not changes["name"]:
                raise QueueError("A printer needs a name.")
        for flag in ("active", "paused"):
            if flag in changes:
                changes[flag] = 1 if changes[flag] else 0
        if changes.get("active") == 0:
            # A switched-off printer gives its pinned jobs back to the category.
            conn.execute("UPDATE plate_jobs SET printer_id = NULL WHERE printer_id = ?"
                         " AND status = ?", (printer_id, WAITING))
        sets = ", ".join(f"{k} = ?" for k in changes)
        try:
            conn.execute(f"UPDATE printers SET {sets} WHERE id = ?",
                         [changes[k] for k in changes] + [printer_id])
        except sqlite3.IntegrityError:
            raise QueueError(f"There's already a printer called “{changes['name']}”.") from None
        return dict(_printer(conn, printer_id))


# ---------------------------------------------------------------------------
# jobs from orders
# ---------------------------------------------------------------------------

def _next_position(conn) -> float:
    return (conn.execute("SELECT MAX(position) FROM plate_jobs").fetchone()[0] or 0) + 1


def ensure_jobs(conn: sqlite3.Connection, quote_id: int) -> int:
    """Make one waiting job per plate of the order, unless it already has
    jobs (re-queueing an order must not duplicate its plates). Returns the
    number of jobs made. Runs in the caller's transaction."""
    if conn.execute("SELECT 1 FROM plate_jobs WHERE quote_id = ?", (quote_id,)).fetchone():
        return 0
    row = conn.execute("SELECT detail_json FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    plates = (json.loads(row["detail_json"] or "{}").get("breakdown") or {}).get("plates") or []
    cats, pos = categories(), _next_position(conn)
    for i, pl in enumerate(plates):
        cat = pl.get("printer") if pl.get("printer") in cats else fallback_category()
        conn.execute(
            "INSERT INTO plate_jobs (quote_id, plate, label, copies, hours, grams, materials,"
            " category, position, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (quote_id, pl.get("plate", i + 1), pl.get("label"), int(pl.get("copies", 1)),
             float(pl.get("hours", 0)), float(pl.get("grams", 0)),
             ",".join(pl.get("materials") or []), cat, pos + i, WAITING, utcnow()))
    return len(plates)


def cancel_jobs(conn: sqlite3.Connection, quote_id: int) -> int:
    """An order that's cancelled or rejected leaves the queue. A plate on a
    printer right now is marked cancelled too; the engineer stops the print."""
    return conn.execute(
        "UPDATE plate_jobs SET status = ?, finished_at = ? WHERE quote_id = ? AND status IN (?, ?)",
        (CANCELLED, utcnow(), quote_id, *OPEN_JOB)).rowcount


# ---------------------------------------------------------------------------
# moving jobs
# ---------------------------------------------------------------------------

def _job(conn, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM plate_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"No job {job_id}.")
    return row


def _usable(conn, printer_id: int, category: str) -> sqlite3.Row:
    p = _printer(conn, printer_id)
    if p["category"] != category:
        raise QueueError(f"{p['name']} is a {categories().get(p['category'], {}).get('label', p['category'])} "
                         f"printer; this plate needs {categories().get(category, {}).get('label', category)}.")
    if not p["active"]:
        raise QueueError(f"{p['name']} is switched off.")
    return p


def _event(conn, quote_id: int, user: str, detail: str) -> None:
    import orders                                # orders imports this module
    orders.add_event(conn, quote_id, user, "queue", detail)


def pin(job_id: int, printer_id: int | None, user: str) -> None:
    """Pin a waiting job to one printer, or (None) hand it back to "next free"."""
    with db() as conn:
        job = _job(conn, job_id)
        if job["status"] != WAITING:
            raise QueueError("Only a waiting plate can be moved to another printer.")
        if printer_id is None:
            conn.execute("UPDATE plate_jobs SET printer_id = NULL WHERE id = ?", (job_id,))
            _event(conn, job["quote_id"], user, f"{job['label'] or 'plate'}: next free printer")
        else:
            p = _usable(conn, printer_id, job["category"])
            conn.execute("UPDATE plate_jobs SET printer_id = ? WHERE id = ?", (printer_id, job_id))
            _event(conn, job["quote_id"], user, f"{job['label'] or 'plate'}: pinned to {p['name']}")
    _notify()


def move(job_id: int, direction: str, user: str) -> None:
    """Swap a waiting job with its neighbour in the same line: the same
    printer's pinned jobs, or the category's unpinned ones."""
    if direction not in ("up", "down"):
        raise QueueError("direction must be up or down")
    with db() as conn:
        job = _job(conn, job_id)
        if job["status"] != WAITING:
            raise QueueError("Only a waiting plate can be reordered.")
        lane = ("printer_id = ?", job["printer_id"]) if job["printer_id"] is not None \
            else ("printer_id IS NULL AND category = ?", job["category"])
        cmp, order = ("<", "DESC") if direction == "up" else (">", "ASC")
        other = conn.execute(
            f"SELECT id, position FROM plate_jobs WHERE status = ? AND {lane[0]}"
            f" AND position {cmp} ? ORDER BY position {order} LIMIT 1",
            (WAITING, lane[1], job["position"])).fetchone()
        if other is None:
            return                                    # already first/last: nothing to do
        conn.execute("UPDATE plate_jobs SET position = ? WHERE id = ?", (other["position"], job_id))
        conn.execute("UPDATE plate_jobs SET position = ? WHERE id = ?", (job["position"], other["id"]))
    _notify()


def _set_order_status(conn, quote_id: int, new: str, user: str, only_from: tuple) -> None:
    import orders
    row = conn.execute("SELECT status FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row and row["status"] in only_from and new in orders.statuses():
        conn.execute("UPDATE quotes SET status = ?, closed_at = NULL, updated_at = ? WHERE id = ?",
                     (new, utcnow(), quote_id))
        orders.add_event(conn, quote_id, user, "status",
                         f"{orders.status_label(row['status'])} → {orders.status_label(new)}")


def start(job_id: int, printer_id: int, user: str) -> None:
    with db() as conn:
        job = _job(conn, job_id)
        if job["status"] != WAITING:
            raise QueueError("That plate isn't waiting to print.")
        p = _usable(conn, printer_id, job["category"])
        if p["paused"]:
            raise QueueError(f"{p['name']} is paused for new jobs.")
        busy = conn.execute("SELECT id FROM plate_jobs WHERE printer_id = ? AND status = ?",
                            (printer_id, PRINTING)).fetchone()
        if busy:
            raise QueueError(f"{p['name']} is already printing; finish that plate first.")
        conn.execute("UPDATE plate_jobs SET status = ?, printer_id = ?, started_at = ? WHERE id = ?",
                     (PRINTING, printer_id, utcnow(), job_id))
        _event(conn, job["quote_id"], user, f"{job['label'] or 'plate'}: printing on {p['name']}")
        _set_order_status(conn, job["quote_id"], "printing", user, ("queued",))
    _notify()


def start_next(printer_id: int, user: str) -> int:
    """Start the next plate for this printer: its own pinned plates first,
    then the category's next free plate. Returns the job id."""
    with db() as conn:
        p = _printer(conn, printer_id)
        nxt = conn.execute(
            "SELECT id FROM plate_jobs WHERE status = ? AND (printer_id = ? OR"
            " (printer_id IS NULL AND category = ?))"
            " ORDER BY (printer_id IS NULL), position LIMIT 1",
            (WAITING, printer_id, p["category"])).fetchone()
    if nxt is None:
        raise QueueError(f"Nothing waiting for {p['name']}.")
    start(nxt["id"], printer_id, user)
    return nxt["id"]


def finish(job_id: int, outcome: str, user: str) -> None:
    """Record how a plate came off the printer. A failed plate stays on
    record (it counts against that printer) and a reprint goes to the front
    of the category's queue."""
    if outcome not in (DONE, FAILED):
        raise QueueError("outcome must be done or failed")
    with db() as conn:
        job = _job(conn, job_id)
        if job["status"] != PRINTING:
            raise QueueError("That plate isn't printing.")
        name = conn.execute("SELECT name FROM printers WHERE id = ?",
                            (job["printer_id"],)).fetchone()
        name = name[0] if name else "a printer"
        conn.execute("UPDATE plate_jobs SET status = ?, finished_at = ?, finished_by = ? WHERE id = ?",
                     (outcome, utcnow(), user, job_id))
        if outcome == FAILED:
            front = conn.execute("SELECT MIN(position) FROM plate_jobs WHERE status = ?",
                                 (WAITING,)).fetchone()[0]
            conn.execute(
                "INSERT INTO plate_jobs (quote_id, plate, label, copies, hours, grams, materials,"
                " category, position, status, reprint_of, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["quote_id"], job["plate"], job["label"], job["copies"], job["hours"],
                 job["grams"], job["materials"], job["category"],
                 (front if front is not None else _next_position(conn)) - 1, WAITING,
                 job_id, utcnow()))
            _event(conn, job["quote_id"], user,
                   f"{job['label'] or 'plate'}: failed on {name}; reprint queued first")
        else:
            _event(conn, job["quote_id"], user, f"{job['label'] or 'plate'}: finished on {name}")
            left = conn.execute("SELECT 1 FROM plate_jobs WHERE quote_id = ? AND status IN (?, ?)",
                                (job["quote_id"], *OPEN_JOB)).fetchone()
            if not left:
                _set_order_status(conn, job["quote_id"], "ready", user, ("queued", "printing"))
    _notify()


def _notify() -> None:
    import orders
    orders.notify()


# ---------------------------------------------------------------------------
# what the pages show
# ---------------------------------------------------------------------------

def _job_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["run_hours"] = round(d["hours"] * d["copies"], 3)
    return d


JOB_SELECT = ("SELECT j.*, q.receipt_no, q.customer_name FROM plate_jobs j"
              " JOIN quotes q ON q.id = j.quote_id")


def order_jobs(quote_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(f"{JOB_SELECT} WHERE j.quote_id = ? ORDER BY j.plate, j.id",
                            (quote_id,)).fetchall()
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM printers")}
    out = []
    for r in rows:
        d = _job_dict(r)
        d["printer_name"] = names.get(d["printer_id"])
        out.append(d)
    return out


def _stats(conn) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT printer_id, status, COUNT(*) n, SUM(hours * copies) h FROM plate_jobs"
            " WHERE printer_id IS NOT NULL AND status IN (?, ?) GROUP BY printer_id, status",
            (DONE, FAILED)):
        s = out.setdefault(r["printer_id"], {"done": 0, "failed": 0, "hours": 0.0})
        s[r["status"]] = r["n"]
        s["hours"] += r["h"] or 0.0
    for s in out.values():
        runs = s["done"] + s["failed"]
        s["hours"] = round(s["hours"], 1)
        s["failure_rate"] = round(s["failed"] / runs, 3) if runs else None
    return out


def board(now: datetime | None = None) -> dict:
    """Everything the Printers page draws: each category with its printers,
    what each is printing, what's lined up on it, and what's waiting for
    any free printer in the category."""
    now = now or datetime.now(timezone.utc)
    cats = categories()
    with db() as conn:
        printers = [dict(r) for r in conn.execute(
            "SELECT * FROM printers ORDER BY category, name COLLATE NOCASE")]
        jobs = [_job_dict(r) for r in conn.execute(
            f"{JOB_SELECT} WHERE j.status IN (?, ?) ORDER BY j.position", OPEN_JOB)]
        stats = _stats(conn)

    out = []
    for key, spec in cats.items():
        mine = [p for p in printers if p["category"] == key]
        cards = []
        for p in mine:
            current = next((j for j in jobs if j["printer_id"] == p["id"]
                            and j["status"] == PRINTING), None)
            # Estimated start of each pinned plate: when the current one ends,
            # then one after another. An estimate from the slicer's times.
            t = now
            if current:
                started = datetime.fromisoformat(current["started_at"])
                end = started + timedelta(hours=current["run_hours"])
                current = {**current, "ends_at": end.isoformat(timespec="minutes"),
                           "progress": max(0.0, min(1.0, (now - started).total_seconds()
                                                    / max(current["run_hours"] * 3600, 1)))}
                t = max(end, now)
            lined_up = []
            for j in (j for j in jobs if j["printer_id"] == p["id"] and j["status"] == WAITING):
                lined_up.append({**j, "starts_at": t.isoformat(timespec="minutes")})
                t += timedelta(hours=j["run_hours"])
            cards.append({**p, "current": current, "queue": lined_up,
                          "stats": stats.get(p["id"], {"done": 0, "failed": 0, "hours": 0.0,
                                                       "failure_rate": None})})
        out.append({
            "id": key,
            "label": spec.get("label", key),
            "label_th": spec.get("label_th"),
            "model": spec.get("model"),
            "measured": bool(spec.get("measured", False)),
            "printers": cards,
            "waiting": [j for j in jobs if j["category"] == key and j["printer_id"] is None
                        and j["status"] == WAITING],
        })
    # Jobs whose category was removed from printers.yaml still need a home.
    orphans = [j for j in jobs if j["category"] not in cats]
    return {"categories": out, "orphans": orphans}
