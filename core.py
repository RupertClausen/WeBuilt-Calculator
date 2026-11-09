"""Shared setup: where things live, the config, and the database.

Imported by the web app and by the accounts, orders, sheet-mirror and
housekeeping modules, so none of them needs to import the whole web app.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

import receipt as receipt_doc
from webuilt import migrations

BASE = Path(__file__).parent
CONFIG_DIR = BASE / "config"
# WEBUILT_LOCAL_DIR points at another overrides folder, e.g. for tests.
LOCAL_DIR = Path(os.environ["WEBUILT_LOCAL_DIR"]) if os.environ.get("WEBUILT_LOCAL_DIR") else BASE / "local"
DATA_DIR = BASE / "data"

RELEASE = json.loads((BASE / "release.json").read_text(encoding="utf-8"))
APP_VERSION = RELEASE["version"]


def _default_db() -> Path:
    """data/orders.db, moving a pre-1.0 orders.db from the app folder there
    once. Only moved when SQLite has no side files open next to it, so a
    database in use is never split from its journal."""
    new, old = DATA_DIR / "orders.db", BASE / "orders.db"
    if not new.exists() and old.exists():
        side = [p for p in BASE.glob("orders.db-*")]
        if side:
            print(f"[webuilt] keeping {old.name} in place: SQLite side files present")
            return old
        DATA_DIR.mkdir(exist_ok=True)
        old.rename(new)
        print(f"[webuilt] moved {old.name} to data/{new.name}")
    DATA_DIR.mkdir(exist_ok=True)
    return new


# WEBUILT_DB points the server at another database, e.g. a throwaway one for
# testing, so trial quotes never land in the real order log.
DB_PATH = Path(os.environ["WEBUILT_DB"]) if os.environ.get("WEBUILT_DB") else _default_db()
MAX_UPLOAD_BYTES = 200 * 1024 * 1024        # sliced files get big

# Uploaded files and database backups live next to the database, so a
# throwaway test database also gets throwaway files and backups.
FILES_DIR = DB_PATH.parent / "files"
BACKUP_DIR = DB_PATH.parent / "backups"


# ---------------------------------------------------------------------------
# config, with hot reload
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    """Overlay `override` on `base`. Mappings merge key by key; anything else
    (numbers, text, lists) is replaced whole. A key set to null in the
    override removes it, e.g. `large: null` drops a shared printer."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = deep_merge(base.get(k), v)
        return out
    return override


class ConfigStore:
    """Shared config from config/, with this computer's overrides from local/
    laid on top. Both are re-read when either file changes.

    A broken edit never takes the app down mid-shift: if a file stops
    parsing, the last good version stays in use and the problem is reported
    on every quote until it is fixed.
    """

    FILES = {
        "pricing": "pricing.yaml",
        "printers": "printers.yaml",
        "filaments": "filaments.yaml",
        "receipt": "receipt.yaml",
        "server": "server.yaml",
    }

    def __init__(self, directory: Path, local_dir: Path | None = None):
        self.dir = directory
        self.local_dir = local_dir
        self._data: dict[str, dict] = {}
        self._stamp: dict[str, tuple] = {}
        self.errors: dict[str, str] = {}
        self.load(force=True, strict=True)

    def _paths(self, name: str) -> tuple[Path, Path | None]:
        local = self.local_dir / name if self.local_dir else None
        return self.dir / name, (local if local and local.exists() else None)

    @staticmethod
    def _read(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping of settings")
        return data

    def load(self, force: bool = False, strict: bool = False) -> list[str]:
        reloaded = []
        for key, name in self.FILES.items():
            shared, local = self._paths(name)
            if not shared.exists():
                raise RuntimeError(f"Missing config file: {shared}")
            stamp = (shared.stat().st_mtime, local.stat().st_mtime if local else None)
            if not force and self._stamp.get(key) == stamp:
                continue
            current = shared
            try:
                merged = self._read(shared)
                if local:
                    current = local
                    merged = deep_merge(merged, self._read(local))
            except (yaml.YAMLError, ValueError, OSError) as exc:
                where = f"{current.parent.name}/{current.name}"
                if strict or key not in self._data:
                    raise RuntimeError(f"Cannot load {where}: {exc}") from exc
                self.errors[key] = (f"{where} has an error, so its last working "
                                    f"version is still in use: {str(exc).splitlines()[0]}")
                self._stamp[key] = stamp            # don't re-parse until it changes
                continue
            self._data[key] = merged
            self._stamp[key] = stamp
            self.errors.pop(key, None)
            reloaded.append(name)
        return reloaded

    def local_files(self) -> list[str]:
        return [n for n in self.FILES.values() if self._paths(n)[1]]

    def __getitem__(self, key: str) -> dict:
        self.load()          # cheap stat() per request
        return self._data[key]


config = ConfigStore(CONFIG_DIR, LOCAL_DIR)


def server_mode() -> bool:
    """True on the shared team server: logins required, sheet mirror on."""
    return bool(config["server"].get("enabled"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

@contextmanager
def db():
    """A connection that commits on success, rolls back on error, and is
    always closed: the server runs all day, so nothing may be left open."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Needed for ON DELETE CASCADE: deleting a quote removes its pictures.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Bring the database up to the current schema. The steps live in
    webuilt/migrations.py, numbered, each run once."""
    with db() as conn:
        migrations.run(conn)


init_db()


def get_state(key: str, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    with db() as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def local_now() -> datetime:
    tz = config["receipt"].get("timezone", "Asia/Bangkok")
    return datetime.now(receipt_doc.zone(tz))
