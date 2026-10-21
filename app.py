"""WeBuilt quote service.

Internal tool. Upload a sliced file, get a price.

    ./run.sh            (or run.bat on Windows; sets everything up)
    uvicorn app:app --reload --port 8000     (development)

Folders:

  config/   shared settings and price lists. Replaced by updates.
  local/    this computer's own overrides. Never touched by updates.
  data/     the order log. Never touched by updates.

Design notes:

  * HTTP, not WebSocket. This is one request in, one quote out — there is no
    stream to keep open. If you later want progress on very large uploads,
    add Server-Sent Events on /quote rather than a socket.

  * The YAML files under config/ and local/ are hot-reloaded on mtime
    change, so prices can change mid-shift without restarting anything.

  * Every quote is appended to orders.db so you can audit margins later and
    back-fit the failure rates from what actually happened.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

import pricing
import updater
import receipt as receipt_doc
from pricing import QuoteItem
from slicefile import FilamentUse, ParseError, SlicedJob, parse

BASE = Path(__file__).parent
CONFIG_DIR = BASE / "config"
LOCAL_DIR = BASE / "local"
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

@asynccontextmanager
async def _lifespan(_app):
    update_check.get()          # start the first background check
    yield


app = FastAPI(title=RELEASE.get("name", "WeBuilt quote service"), version=APP_VERSION,
              lifespan=_lifespan)


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


# ---------------------------------------------------------------------------
# order log
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
    with db() as conn:
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


init_db()


def plate_thumbnails(items: list) -> dict[int, tuple[bytes, str]]:
    """Plate number -> (image, mime), numbered the same way as the quote's
    breakdown.plates (one entry per item, starting at 1)."""
    return {i: (it.job.thumbnail, it.job.thumbnail_mime)
            for i, it in enumerate(items, start=1)
            if it.job.thumbnail and it.job.thumbnail_mime}


def thumbnail_url(quote_id: int, plate: int) -> str:
    return f"/api/orders/{quote_id}/plates/{plate}/thumbnail"


def attach_thumbnails(payload: dict, thumbs: dict, quote_id: int | None) -> None:
    """Give each plate in the response a picture link. Saved quotes point at
    the stored image; unsaved ones carry it inline. Works on a copy of the
    plate list so nothing is written back into the stored quote."""
    plates = [dict(p) for p in payload["breakdown"].get("plates", [])]
    for p in plates:
        hit = thumbs.get(p["plate"])
        if not hit:
            p["thumbnail"] = None
        elif quote_id is not None:
            p["thumbnail"] = thumbnail_url(quote_id, p["plate"])
        else:
            data, mime = hit
            p["thumbnail"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    payload["breakdown"] = {**payload["breakdown"], "plates": plates}


def local_now() -> datetime:
    tz = config["receipt"].get("timezone", "Asia/Bangkok")
    return datetime.now(receipt_doc.zone(tz))


def next_receipt_no(conn: sqlite3.Connection, when: datetime) -> str:
    """WB-YYMM-NNN, restarting each month, never below the configured floor."""
    rc = config["receipt"]
    prefix = rc.get("receipt_prefix", "WB")
    yymm = when.strftime("%y%m")
    stem = f"{prefix}-{yymm}-"
    highest = 0
    for (no,) in conn.execute(
        "SELECT receipt_no FROM quotes WHERE receipt_no LIKE ?", (stem + "%",)
    ):
        tail = (no or "")[len(stem):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    floor = int((rc.get("next_number_floor") or {}).get(yymm, 1))
    return f"{stem}{max(highest + 1, floor):03d}"


def log_quote(
    filename: str,
    quote,
    printer: str,
    rush: str,
    delivery: str,
    customer: dict | None = None,
    thumbnails: dict[int, tuple[bytes, str]] | None = None,
) -> tuple[int, str]:
    """Append a quote to the order log.

    For a multi-plate order the per-plate risk numbers differ, so the summary
    columns carry the WORST plate's success probability and the highest risk
    factor — the ones worth noticing at a glance. Every plate's detail is in
    detail_json.
    """
    bd = quote.breakdown
    plates = bd.get("plates") or []
    materials = sorted({m["type"] for m in bd.get("materials", [])})
    worst_p = min((d["risk"]["p_success_first_try"] for d in plates), default=1.0)
    worst_rf = max((d["risk"]["factor"] for d in plates), default=1.0)
    econ = bd.get("economics", {})

    customer = customer or {}
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # IMMEDIATE takes the write lock before reading the highest receipt
        # number, so two quotes saved at once cannot get the same number.
        conn.execute("BEGIN IMMEDIATE")
        receipt_no = customer.get("receipt_no") or next_receipt_no(conn, local_now())
        cur = conn.execute(
            """INSERT INTO quotes
               (created_at, filename, printer, materials, grams, hours, plates,
                rush, delivery, total, currency, est_cost, est_margin,
                p_success, risk_factor, detail_json,
                receipt_no, customer_name, customer_contact, notes,
                payment_status, labour, contingency, profit, app_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                filename,
                printer,
                ",".join(materials),
                bd["order"]["grams_total"],
                bd["order"]["hours_total"],
                bd["order"]["plates"],
                rush,
                delivery,
                quote.total,
                quote.currency,
                bd["margin"]["estimated_direct_cost"],
                bd["margin"]["estimated_margin"],
                worst_p,
                worst_rf,
                json.dumps(quote.to_dict(), ensure_ascii=False),
                receipt_no,
                customer.get("customer_name"),
                customer.get("customer_contact"),
                customer.get("notes"),
                "unpaid",
                econ.get("labour", {}).get("total"),
                econ.get("contingency"),
                econ.get("profit"),
                APP_VERSION,
            ),
        )
        quote_id = cur.lastrowid
        for plate, (data, mime) in (thumbnails or {}).items():
            conn.execute(
                "INSERT INTO plate_thumbnails (quote_id, plate, mime, data)"
                " VALUES (?,?,?,?)", (quote_id, plate, mime, data))
        conn.execute("COMMIT")
        return quote_id, receipt_no
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(
            409, f"Receipt number {customer.get('receipt_no')} is already used."
        ) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# version and updates
# ---------------------------------------------------------------------------

def local_settings() -> dict:
    """local/settings.yaml: this computer's preferences (update_check: false
    turns off the update notice). Optional."""
    f = LOCAL_DIR / "settings.yaml"
    if not f.exists():
        return {}
    try:
        return yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


class UpdateCheck:
    """Asks GitHub for the newest release in the background, at most every
    few hours, so the page never waits on the network."""

    EVERY = 6 * 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.result: dict = {}
        self.checked_at = 0.0
        self.running = False

    def _run(self):
        try:
            info = updater.latest_release(RELEASE.get("repo", ""), updater.github_token(BASE))
            newer = updater.parse_version(info["version"]) > updater.parse_version(APP_VERSION)
            res = {"latest": info["version"], "update_available": newer,
                   "notes": info["notes"], "url": info["url"], "error": None}
        except Exception as exc:                      # offline, no token, no releases yet
            res = {"latest": None, "update_available": False, "notes": "",
                   "url": "", "error": str(exc)}
        with self.lock:
            self.result, self.checked_at, self.running = res, time.time(), False

    def get(self, refresh: bool = False) -> dict:
        enabled = bool(RELEASE.get("repo")) and local_settings().get("update_check", True)
        if not enabled:
            return {"latest": None, "update_available": False, "notes": "", "url": "",
                    "error": None, "disabled": True}
        with self.lock:
            stale = refresh or time.time() - self.checked_at > self.EVERY
            if stale and not self.running:
                self.running = True
                threading.Thread(target=self._run, daemon=True).start()
            return {**self.result, "checking": self.running, "disabled": False}


update_check = UpdateCheck()


def _install_kind() -> str:
    if (BASE / ".git").exists():
        return "git"
    return "release" if (BASE / "MANIFEST.json").exists() else "unpacked"


@app.get("/api/version")
def version(refresh: bool = False):
    kind = _install_kind()
    return {
        "version": APP_VERSION,
        "install": kind,
        # The button only shows when the launcher can restart the app after.
        "can_self_update": kind == "release" and getattr(app.state, "self_update", False),
        "local_overrides": config.local_files(),
        "config_errors": list(config.errors.values()),
        **update_check.get(refresh),
    }


@app.post("/api/update")
def request_update(x_webuilt_action: str | None = Header(None)):
    """Stop the app so the launcher can install the update and restart it.

    The custom header can't be sent by another website without a CORS
    preflight, which this app never grants, so a page you happen to be
    browsing can't trigger this.
    """
    if x_webuilt_action != "update":
        raise HTTPException(400, "Missing X-WeBuilt-Action: update")
    if _install_kind() != "release" or not getattr(app.state, "self_update", False):
        raise HTTPException(409, "This copy can't update itself; see README.")
    stop = getattr(app.state, "request_update", None)
    if stop is None:
        raise HTTPException(409, "Not started by the launcher.")
    threading.Timer(0.5, stop).start()       # let this response reach the page first
    return {"ok": True, "message": "Updating; the app will restart by itself."}


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/options")
def options():
    """Everything the UI needs to build its form."""
    pr, pc = config["printers"], config["pricing"]
    return {
        "printers": [
            {
                "id": pid,
                "label": p.get("label", pid),
                "model": p.get("model"),
                "measured": p.get("measured", False),
                "shape": p.get("shape", 1.0),
                "hazard": p.get("hazard"),
                "p_start": p.get("p_start"),
            }
            for pid, p in pr.get("printers", {}).items()
        ],
        "default_printer": pr.get("fallback_printer"),
        "rush_tiers": [
            {"id": k, "label": v.get("label", k), "multiplier": v["multiplier"]}
            for k, v in pc["rush_tiers"].items()
        ],
        "default_rush": pc.get("default_rush"),
        "delivery_options": [
            {"id": k, "label": v.get("label", k), "fee": v["fee"]}
            for k, v in pc["delivery_options"].items()
        ],
        "default_delivery": pc.get("default_delivery"),
        "client_profiles": [
            {
                "id": k,
                "label": v.get("label", k),
                "prep_minutes": v.get("prep_minutes", 0),
                "risk_multiplier": v.get("risk_multiplier", 1.0),
                "postproc_multiplier": v.get("postproc_multiplier", 1.0),
                "price_multiplier": v.get("price_multiplier", 1.0),
            }
            for k, v in pc.get("client_profiles", {}).items()
        ],
        "default_client_profile": pc.get("default_client_profile"),
        "materials": sorted((config["filaments"].get("materials") or {}).keys()),
        "labour_rate_per_hour": pc.get("labour_rate_per_hour"),
        "currency": pc.get("currency", "THB"),
    }


@app.get("/api/config")
def get_config():
    return {
        "pricing": config["pricing"],
        "printers": config["printers"],
        "filaments": config["filaments"],
    }


@app.post("/api/config/reload")
def reload_config():
    changed = config.load(force=True)
    return {"reloaded": changed}


@app.post("/api/quote")
async def quote_endpoint(
    files: list[UploadFile] = File(..., description="one or more sliced files"),
    printer: str | None = Form(None),
    copies: list[int] | None = Form(None),
    plates: int | None = Form(None),
    rush: str | None = Form(None),
    delivery: str | None = Form(None),
    postproc_minutes: float = Form(0.0),
    client_profile: str | None = Form(None),
    prep_minutes: float | None = Form(None),
    customer_name: str | None = Form(None),
    customer_contact: str | None = Form(None),
    notes: str | None = Form(None),
    receipt_no: str | None = Form(None),
    save: bool = Form(True),
):
    """Quote an order of one or more sliced files.

    A customer whose parts do not fit on one bed sends several files. Each
    file contributes one plate (or several, if it is a multi-plate
    .gcode.3mf), and every plate is risked on its own duration — see the
    module docstring in pricing.py for why that cannot be aggregated.

    `copies` is an optional parallel list: how many times to run each file.
    """
    if not files:
        raise HTTPException(400, "No files uploaded.")

    t0 = time.perf_counter()
    items: list[QuoteItem] = []
    file_meta = []
    total_bytes = 0

    for i, up in enumerate(files):
        data = await up.read()
        total_bytes += len(data)
        if total_bytes > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"Upload over {MAX_UPLOAD_BYTES // (1024*1024)} MB in total.")
        if not data:
            raise HTTPException(400, f"{up.filename or 'file'} is empty.")

        name = up.filename or f"upload-{i+1}"
        try:
            jobs = parse(name, data)
        except ParseError as exc:
            raise HTTPException(422, f"{name}: {exc}") from exc

        n_copies = 1
        if copies and i < len(copies):
            n_copies = max(int(copies[i]), 1)

        for j, job in enumerate(jobs):
            label = name if len(jobs) == 1 else f"{name} (plate {job.plate_index})"
            items.append(QuoteItem(job=job, copies=n_copies, label=label))

        file_meta.append({
            "name": name,
            "bytes": len(data),
            "plates": len(jobs),
            "copies": n_copies,
            "parsed_by": jobs[0].source,
        })

    pc, pr, fl = config["pricing"], config["printers"], config["filaments"]
    chosen_printer = printer or pr.get("fallback_printer", "normal")
    chosen_rush = rush or pc.get("default_rush", "standard")
    chosen_delivery = delivery or pc.get("default_delivery", "pickup")

    # Legacy single-file `plates` still means "run it this many times".
    if plates is not None and len(items) == 1:
        items[0].copies = max(int(plates), 1)

    try:
        quote = pricing.build_quote(
            items,
            pricing=pc, printers=pr, filaments=fl,
            printer_id=chosen_printer,
            rush=chosen_rush,
            delivery=chosen_delivery,
            postproc_minutes=postproc_minutes or 0.0,
            client_profile=client_profile,
            prep_minutes=prep_minutes,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(500, f"Pricing config problem: {exc}") from exc

    payload = quote.to_dict()
    payload["app_version"] = APP_VERSION
    # A broken edit to a config file is reported on every quote until fixed.
    payload["warnings"] = list(config.errors.values()) + payload["warnings"]
    payload["files"] = file_meta
    payload["file"] = {
        "name": (file_meta[0]["name"] if len(file_meta) == 1
                 else f"{len(file_meta)} files"),
        "bytes": total_bytes,
        "parsed_by": file_meta[0]["parsed_by"],
        "parse_ms": round((time.perf_counter() - t0) * 1000, 1),
    }

    thumbs = plate_thumbnails(items)
    if save:
        payload["quote_id"], payload["receipt_no"] = log_quote(
            payload["file"]["name"], quote,
            chosen_printer, chosen_rush, chosen_delivery,
            customer={"customer_name": customer_name,
                      "customer_contact": customer_contact,
                      "notes": notes, "receipt_no": receipt_no or None},
            thumbnails=thumbs,
        )
        payload["receipt_url"] = f"/receipt/{payload['quote_id']}"
    attach_thumbnails(payload, thumbs, payload.get("quote_id"))

    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# manual entry
# ---------------------------------------------------------------------------

class ManualFilament(BaseModel):
    type: str = Field(..., description="PLA, PETG, ABS, ...")
    grams: float = Field(..., gt=0)


class ManualQuote(BaseModel):
    """Everything a sliced file would have told us, entered by hand.

    Use when there is no sliced file: quoting from a photo, a sketch, a
    customer's own estimate, or a repeat of a job you already know.
    """

    # --- what the file would have provided ---
    filaments: list[ManualFilament] = Field(..., min_length=1)
    hours: float = Field(0, ge=0)
    minutes: float = Field(0, ge=0)

    # --- the same options as upload mode ---
    printer: str | None = None
    plates: int = Field(1, ge=1)
    rush: str | None = None
    delivery: str | None = None
    postproc_minutes: float = Field(0, ge=0)
    client_profile: str | None = None
    prep_minutes: float | None = Field(None, ge=0)

    # --- bookkeeping ---
    label: str | None = Field(None, description="job or customer reference")
    customer_name: str | None = None
    customer_contact: str | None = None
    notes: str | None = None
    receipt_no: str | None = None
    save: bool = True

    @property
    def seconds(self) -> float:
        return self.hours * 3600 + self.minutes * 60


@app.post("/api/quote/manual")
def quote_manual(body: ManualQuote):
    if body.seconds <= 0:
        raise HTTPException(422, "Enter a print time greater than zero.")

    # plates=1 on the job itself: `body.plates` is the number of RUNS and is
    # passed to build_quote as copies. Setting both would square it.
    job = SlicedJob(
        filaments=[FilamentUse(type=f.type.upper(), grams=f.grams)
                   for f in body.filaments],
        seconds=body.seconds,
        plates=1,
        source="manual entry",
    )

    pc, pr, fl = config["pricing"], config["printers"], config["filaments"]
    chosen_printer = body.printer or pr.get("fallback_printer", "normal")
    chosen_rush = body.rush or pc.get("default_rush", "standard")
    chosen_delivery = body.delivery or pc.get("default_delivery", "pickup")

    known = set((fl.get("materials") or {}).keys())
    for f in job.filaments:
        if f.type not in known:
            job.warnings.append(
                f"'{f.type}' is not in the filament roster — using the "
                "unknown-material fallback rate."
            )

    try:
        quote = pricing.build_quote(
            job,
            pricing=pc, printers=pr, filaments=fl,
            printer_id=chosen_printer,
            plates=body.plates,
            rush=chosen_rush,
            delivery=chosen_delivery,
            postproc_minutes=body.postproc_minutes,
            client_profile=body.client_profile,
            prep_minutes=body.prep_minutes,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(500, f"Pricing config problem: {exc}") from exc

    payload = quote.to_dict()
    payload["app_version"] = APP_VERSION
    # A broken edit to a config file is reported on every quote until fixed.
    payload["warnings"] = list(config.errors.values()) + payload["warnings"]
    payload["file"] = {"name": body.label or "manual entry",
                       "parsed_by": "manual entry"}

    if body.save:
        payload["quote_id"], payload["receipt_no"] = log_quote(
            body.label or "manual entry", quote,
            chosen_printer, chosen_rush, chosen_delivery,
            customer={"customer_name": body.customer_name,
                      "customer_contact": body.customer_contact,
                      "notes": body.notes, "receipt_no": body.receipt_no or None},
        )
        payload["receipt_url"] = f"/receipt/{payload['quote_id']}"
    attach_thumbnails(payload, {}, payload.get("quote_id"))   # manual: no pictures

    return JSONResponse(payload)


@app.get("/api/orders")
def list_orders(limit: int = 50):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, receipt_no, created_at, customer_name, filename, printer,"
            " materials, grams, hours, total, currency, est_margin, p_success,"
            " outcome, payment_status, payment_method, paid_at,"
            " labour, contingency, profit, app_version"
            " FROM quotes ORDER BY id DESC LIMIT ?",
            (min(limit, 500),),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/orders/{quote_id}/outcome")
def set_outcome(quote_id: int, outcome: str = Form(...)):
    """Mark what actually happened, so failure rates can be back-fitted.

    Use 'completed' or 'failed'. Once you have a few hundred of these you can
    fit each printer's p_start, hazard and shape from your own data instead
    of guessing them in printers.yaml.
    """
    if outcome not in {"quoted", "completed", "failed", "cancelled"}:
        raise HTTPException(400, "outcome must be quoted|completed|failed|cancelled")
    with db() as conn:
        cur = conn.execute(
            "UPDATE quotes SET outcome = ? WHERE id = ?", (outcome, quote_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"No quote {quote_id}")
    return {"id": quote_id, "outcome": outcome}


# ---------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------

def _load_order(quote_id: int) -> tuple[dict, dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No quote {quote_id}")
    row = dict(row)
    return row, json.loads(row["detail_json"])


def _view(quote_id: int, lang: str | None = None) -> dict:
    row, quote = _load_order(quote_id)
    with db() as conn:
        plates = [r["plate"] for r in conn.execute(
            "SELECT plate FROM plate_thumbnails WHERE quote_id = ?", (quote_id,))]
    return receipt_doc.build_view(
        row, quote, config["receipt"], lang,
        pricing_cfg=config["pricing"], printers_cfg=config["printers"],
        thumbnails={p: thumbnail_url(quote_id, p) for p in plates})


@app.get("/api/receipt/{quote_id}")
def receipt_json(quote_id: int, lang: str | None = None):
    """The customer-safe view model. Nothing outside it can reach a receipt."""
    return _view(quote_id, lang)


@app.get("/receipt/{quote_id:int}", response_class=HTMLResponse)
def receipt_html(quote_id: int, lang: str | None = None):
    return HTMLResponse(receipt_doc.render_html(
        _view(quote_id, lang), quote_id, config["receipt"].get("payment_methods")))


@app.get("/receipt/{quote_id:int}.txt", response_class=PlainTextResponse)
def receipt_text(quote_id: int, lang: str | None = None):
    return PlainTextResponse(receipt_doc.render_text(_view(quote_id, lang)))


@app.get("/api/orders/{quote_id}/plates/{plate}/thumbnail")
def get_thumbnail(quote_id: int, plate: int):
    with db() as conn:
        row = conn.execute(
            "SELECT mime, data FROM plate_thumbnails WHERE quote_id = ? AND plate = ?",
            (quote_id, plate)).fetchone()
    if row is None:
        raise HTTPException(404, "No picture for that plate")
    # Only PNG/JPEG are ever stored (checked by their bytes on upload), and
    # nosniff stops the browser second-guessing the type.
    return Response(content=row["data"], media_type=row["mime"], headers={
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/api/orders/{quote_id}/payment")
def set_payment(
    quote_id: int,
    status: str = Form(...),
    method: str | None = Form(None),
):
    """Record payment. The document title follows: Quotation until paid,
    Receipt after."""
    if status not in {"paid", "unpaid"}:
        raise HTTPException(400, "status must be paid|unpaid")
    allowed = config["receipt"].get("payment_methods") or []
    if status == "paid" and method and allowed and method not in allowed:
        raise HTTPException(400, f"method must be one of {allowed}")
    paid_at = (datetime.now(timezone.utc).isoformat(timespec="seconds")
               if status == "paid" else None)
    with db() as conn:
        cur = conn.execute(
            "UPDATE quotes SET payment_status = ?, payment_method = ?, paid_at = ?"
            " WHERE id = ?",
            (status, method if status == "paid" else None, paid_at, quote_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"No quote {quote_id}")
    return {"id": quote_id, "payment_status": status, "payment_method": method,
            "paid_at": paid_at}
