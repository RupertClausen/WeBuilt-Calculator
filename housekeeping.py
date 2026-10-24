"""Daily chores: database backups, file retention, expired sessions.

A background thread wakes every hour and does whatever is due:

  * Once a day, after `backups.hour` local time, the database is copied to
    BACKUP_DIR/orders-YYYY-MM-DD.db with SQLite's online backup, so the copy
    is consistent even while people are quoting. The newest `backups.keep`
    are kept.
  * Files of orders closed more than `files.keep_days_after_closed` days ago
    are deleted. The order, its prices and plate pictures stay.
  * Expired login sessions are removed.

Uploaded files are not part of these backups; on the server, Proxmox's own
backups of the container cover them.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

import auth
import orders
from core import BACKUP_DIR, DB_PATH, config, get_state, local_now, set_state, utcnow

CHECK_EVERY = 3600


def backup_database(when: datetime) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"orders-{when:%Y-%m-%d}.db"
    tmp = dest.with_suffix(".db.part")
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    tmp.replace(dest)                     # never leave a half-written backup
    return dest.name


def prune_backups(keep: int) -> list[str]:
    copies = sorted(BACKUP_DIR.glob("orders-*.db"))
    gone = copies[:-keep] if keep > 0 else []
    for p in gone:
        p.unlink(missing_ok=True)
    return [p.name for p in gone]


def run_once(now: datetime | None = None) -> dict:
    """Do whatever is due. Safe to call any time; returns what it did."""
    now = now or local_now()
    srv = config["server"]
    state = get_state("housekeeping", {}) or {}
    did: dict = {}

    bk = srv.get("backups") or {}
    today = now.strftime("%Y-%m-%d")
    if bk.get("enabled", True) and now.hour >= int(bk.get("hour", 3)) \
            and state.get("last_backup_day") != today:
        try:
            did["backup"] = backup_database(now)
            did["pruned"] = prune_backups(int(bk.get("keep", 14)))
            state["last_backup_day"] = today
            state["last_backup"] = did["backup"]
            state.pop("backup_error", None)
        except Exception as exc:
            state["backup_error"] = f"{utcnow()}: {exc}"
            did["backup_error"] = str(exc)

    if state.get("last_cleanup_day") != today:
        keep = int((srv.get("files") or {}).get("keep_days_after_closed", 90))
        did["files_removed"] = orders.delete_expired_files(keep)
        did["sessions_removed"] = auth.purge_expired_sessions()
        state["last_cleanup_day"] = today
        state["last_files_removed"] = did["files_removed"]

    state["last_run"] = utcnow()
    set_state("housekeeping", state)
    return did


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            run_once()
        except Exception as exc:          # keep going; report on the admin page
            set_state("housekeeping_error", f"{utcnow()}: {exc}")
        stop.wait(CHECK_EVERY)


_stop = threading.Event()
_thread: threading.Thread | None = None


def start() -> None:
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, args=(_stop,), name="housekeeping",
                                   daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
