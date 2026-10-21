"""WeBuilt quote service.

Internal tool. Upload a sliced file, get a price.

    uvicorn app:app --reload --port 8000

Design notes:

  * HTTP, not WebSocket. This is one request in, one quote out — there is no
    stream to keep open. If you later want progress on very large uploads,
    add Server-Sent Events on /quote rather than a socket.

  * The three YAML files under config/ are hot-reloaded on mtime change, so
    you can edit filament prices mid-shift without restarting anything.

  * Every quote is appended to orders.db so you can audit margins later and
    back-fit the failure rates from what actually happened.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

import pricing
import receipt as receipt_doc
from pricing import QuoteItem
from slicefile import FilamentUse, ParseError, SlicedJob, parse

BASE = Path(__file__).parent
CONFIG_DIR = BASE / "config"
# WEBUILT_DB points the server at another database, e.g. a throwaway one for
# testing, so trial quotes never land in the real order log.
DB_PATH = Path(os.environ.get("WEBUILT_DB") or BASE / "orders.db")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024        # sliced files get big

app = FastAPI(title="WeBuilt quote service", version="1.0")


# ---------------------------------------------------------------------------
# config, with hot reload
# ---------------------------------------------------------------------------

class ConfigStore:
    FILES = {
        "pricing": "pricing.yaml",
        "printers": "printers.yaml",
        "filaments": "filaments.yaml",
        "receipt": "receipt.yaml",
    }

    def __init__(self, directory: Path):
        self.dir = directory
        self._data: dict[str, dict] = {}
        self._mtimes: dict[str, float] = {}
        self.load(force=True)

    def load(self, force: bool = False) -> list[str]:
        reloaded = []
        for key, name in self.FILES.items():
            path = self.dir / name
            if not path.exists():
                raise RuntimeError(f"Missing config file: {path}")
            mtime = path.stat().st_mtime
            if force or self._mtimes.get(key) != mtime:
                with path.open("r", encoding="utf-8") as fh:
                    self._data[key] = yaml.safe_load(fh) or {}
                self._mtimes[key] = mtime
                reloaded.append(name)
        return reloaded

    def __getitem__(self, key: str) -> dict:
        self.load()          # cheap stat() per request
        return self._data[key]


config = ConfigStore(CONFIG_DIR)


# ---------------------------------------------------------------------------
# order log
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Needed for ON DELETE CASCADE: deleting a quote removes its pictures.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    return datetime.now(ZoneInfo(tz))


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
                payment_status, labour, contingency, profit)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            " labour, contingency, profit"
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
