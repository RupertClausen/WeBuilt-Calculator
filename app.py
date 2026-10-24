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
from urllib.parse import quote as urlquote

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import auth
import housekeeping
import orders
import pricing
import sheets
import updater
import receipt as receipt_doc
from pricing import QuoteItem
from slicefile import FilamentUse, ParseError, SlicedJob, parse
from core import (BASE, CONFIG_DIR, LOCAL_DIR, DATA_DIR, FILES_DIR, BACKUP_DIR,
                  RELEASE, APP_VERSION, DB_PATH, MAX_UPLOAD_BYTES, ConfigStore,
                  config, db, deep_merge, get_state, init_db, local_now,
                  server_mode, set_state, utcnow)

@asynccontextmanager
async def _lifespan(_app):
    update_check.get()          # start the first background check
    housekeeping.start()        # backups, file retention, expired sessions
    if server_mode():
        sheets.mirror.start()   # does nothing until sheets.enabled is true
    yield
    housekeeping.stop()
    sheets.mirror.stop()


app = FastAPI(title=RELEASE.get("name", "WeBuilt quote service"), version=APP_VERSION,
              lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# ---------------------------------------------------------------------------
# logins (server mode only)
# ---------------------------------------------------------------------------

# Reachable without logging in: the login page itself, and the health check.
PUBLIC_PATHS = {"/login", "/api/login", "/api/me", "/healthz"}


@app.middleware("http")
async def team_gate(request: Request, call_next):
    """On the team server, everything needs a login, and every change needs
    the X-WeBuilt header. A page on another website can make your browser
    send a form, but not a custom header, so this stops cross-site requests
    riding on your login. On someone's own computer, nothing changes."""
    request.state.user = None
    if not server_mode():
        return await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            request.headers.get("x-webuilt") != "1":
        return JSONResponse({"detail": "Missing X-WeBuilt header."}, status_code=403)
    request.state.user = await run_in_threadpool(
        auth.user_for_token, request.cookies.get(auth.COOKIE))
    path = request.url.path
    if request.state.user or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in."}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={urlquote(target)}", status_code=303)


@app.middleware("http")
async def fresh_pages(request: Request, call_next):
    """Pages, scripts and styles: browsers must check for a newer copy every
    time (a tiny 304 when nothing changed). Otherwise, after an update, a
    browser can keep running yesterday's scripts against the new server."""
    response = await call_next(request)
    if request.method == "GET" and "cache-control" not in response.headers and \
            not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def actor(request: Request) -> str:
    """Who did it, for the order history. 'local' on someone's own computer."""
    u = current_user(request)
    return u["username"] if u else "local"


def require_admin(request: Request) -> None:
    if server_mode():
        u = current_user(request)
        if not u or u["role"] != "admin":
            raise HTTPException(403, "Only an admin can do that.")


def _secure_cookie(request: Request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https")


class LoginBody(BaseModel):
    username: str
    password: str


@app.get("/login")
def login_page():
    return FileResponse(BASE / "static" / "login.html")


@app.post("/api/login")
def api_login(body: LoginBody, request: Request):
    if not server_mode():
        raise HTTPException(409, "Logins are only used on the team server.")
    try:
        token, user = auth.login(body.username, body.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    resp = JSONResponse({"user": user})
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=_secure_cookie(request),
                    max_age=int(config["server"].get("session_days", 14)) * 86400)
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/me")
def api_me(request: Request):
    return {"server_mode": server_mode(), "user": current_user(request),
            "needs_first_admin": server_mode() and auth.user_count() == 0}


@app.get("/api/users")
def team_members(request: Request):
    """Active teammates, for 'handled by'. Names only, for anyone logged in."""
    return [{"username": u["username"], "display_name": u["display_name"]}
            for u in auth.list_users() if u["active"]] if server_mode() else []


class PasswordBody(BaseModel):
    current: str
    new: str


@app.post("/api/me/password")
def api_change_password(body: PasswordBody, request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(409, "Logins are only used on the team server.")
    try:
        auth.change_own_password(u["id"], body.current, body.new,
                                 keep_session=request.cookies.get(auth.COOKIE))
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "message": "Password changed. Log in again on your other devices."}


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "member"
    display_name: str | None = None


class UserChange(BaseModel):
    role: str | None = None
    active: bool | None = None
    display_name: str | None = None
    password: str | None = None


@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    return auth.list_users()


@app.post("/api/admin/users")
def admin_add_user(body: NewUser, request: Request):
    require_admin(request)
    try:
        return auth.create_user(body.username, body.password, body.role, body.display_name)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/admin/status")
def admin_status(request: Request):
    """What the background jobs are doing, for the admin page."""
    require_admin(request)
    with db() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM quotes GROUP BY status")}
        files = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes"
                             " FROM order_files WHERE deleted_at IS NULL").fetchone()
    return {
        "version": APP_VERSION,
        "server_mode": server_mode(),
        "orders_by_status": counts,
        "stored_files": {"count": files["n"], "bytes": files["bytes"]},
        "sheet": sheets.mirror.status(),
        "housekeeping": get_state("housekeeping", {}),
        "housekeeping_error": get_state("housekeeping_error"),
        "backups": sorted(p.name for p in BACKUP_DIR.glob("orders-*.db"))[-5:],
        "recently_deleted": orders.deleted_orders(10),
        "config_errors": list(config.errors.values()),
    }


@app.post("/api/admin/sheet/sync")
def admin_sheet_sync(request: Request):
    require_admin(request)
    if not sheets.mirror.enabled():
        raise HTTPException(409, "The sheet mirror is off: set sheets.enabled in "
                                 "local/server.yaml.")
    return sheets.mirror.sync_now()


@app.post("/api/admin/housekeeping")
def admin_housekeeping(request: Request):
    require_admin(request)
    return housekeeping.run_once()


@app.patch("/api/admin/users/{user_id}")
def admin_change_user(user_id: int, body: UserChange, request: Request):
    require_admin(request)
    try:
        return auth.update_user(user_id, role=body.role, active=body.active,
                                display_name=body.display_name, password=body.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------------------
# order log
# ---------------------------------------------------------------------------

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


def next_receipt_no(conn: sqlite3.Connection, when: datetime) -> str:
    """WB-YYMM-NNN, restarting each month.

    Continues from the last number the APP issued this month, never below
    the configured floor, and steps over any number already taken. Numbers
    typed by hand don't move it: a one-off WB-2610-999 must not make every
    following receipt 1000-something.
    """
    rc = config["receipt"]
    prefix = rc.get("receipt_prefix", "WB")
    yymm = when.strftime("%y%m")
    stem = f"{prefix}-{yymm}-"
    taken: set[int] = set()
    last_auto = 0
    # Deleted orders count too: their numbers may be in customers' hands.
    for no, auto in conn.execute(
        "SELECT receipt_no, receipt_auto FROM quotes WHERE receipt_no LIKE ?"
        " UNION ALL SELECT receipt_no, receipt_auto FROM deleted_orders"
        " WHERE receipt_no LIKE ?",
        (stem + "%", stem + "%"),
    ):
        tail = (no or "")[len(stem):]
        if tail.isdigit():
            taken.add(int(tail))
            if auto:
                last_auto = max(last_auto, int(tail))
    floor = int((rc.get("next_number_floor") or {}).get(yymm, 1))
    n = max(last_auto + 1, floor)
    while n in taken:
        n += 1
    return f"{stem}{n:03d}"


def log_quote(
    filename: str,
    quote,
    printer: str,
    rush: str,
    delivery: str,
    customer: dict | None = None,
    thumbnails: dict[int, tuple[bytes, str]] | None = None,
    created_by: str = "local",
    files: list[tuple[str, bytes]] | None = None,
) -> tuple[int, str, str | None]:
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
    quote_id = None
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # IMMEDIATE takes the write lock before reading the highest receipt
        # number, so two quotes saved at once cannot get the same number.
        conn.execute("BEGIN IMMEDIATE")
        # A typed number that's already used must not stop the quote: save
        # it under the next free number instead and say so.
        note = None
        typed = (customer.get("receipt_no") or "").strip()
        receipt_no, auto = typed, False
        if typed and conn.execute(
                "SELECT 1 FROM quotes WHERE receipt_no = ?"
                " UNION ALL SELECT 1 FROM deleted_orders WHERE receipt_no = ?",
                (typed, typed)).fetchone():
            receipt_no = ""
        if not receipt_no:
            receipt_no, auto = next_receipt_no(conn, local_now()), True
            if typed:
                note = (f"Receipt number {typed} is already used, so this "
                        f"quote was saved as {receipt_no}.")
        cur = conn.execute(
            """INSERT INTO quotes
               (created_at, filename, printer, materials, grams, hours, plates,
                rush, delivery, total, currency, est_cost, est_margin,
                p_success, risk_factor, detail_json,
                receipt_no, customer_name, customer_contact, notes,
                payment_status, labour, contingency, profit, app_version,
                discount, discount_label)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                econ.get("profit_after_discount", econ.get("profit")),
                APP_VERSION,
                -(econ.get("discount") or 0.0),
                (bd.get("discount") or {}).get("label") or (
                    (bd.get("discount") or {}).get("source")),
            ),
        )
        quote_id = cur.lastrowid
        if auto:
            conn.execute("UPDATE quotes SET receipt_auto = 1 WHERE id = ?", (quote_id,))
        for plate, (data, mime) in (thumbnails or {}).items():
            conn.execute(
                "INSERT INTO plate_thumbnails (quote_id, plate, mime, data)"
                " VALUES (?,?,?,?)", (quote_id, plate, mime, data))
        conn.execute("UPDATE quotes SET created_by = ?, status = 'quoted', updated_at = ?"
                     " WHERE id = ?", (created_by, utcnow(), quote_id))
        orders.add_event(conn, quote_id, created_by, "created",
                         f"{receipt_no}, {quote.currency} {quote.total:,.2f}")
        orders.save_files(conn, quote_id, files or [], created_by)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        if quote_id is not None:
            orders.remove_order_dir(quote_id)       # files written before the failure
        raise
    finally:
        conn.close()
    orders.notify()
    return quote_id, receipt_no, note


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
def request_update(request: Request, x_webuilt_action: str | None = Header(None)):
    """Stop the app so the launcher can install the update and restart it.

    The custom header can't be sent by another website without a CORS
    preflight, which this app never grants, so a page you happen to be
    browsing can't trigger this.
    """
    if x_webuilt_action != "update":
        raise HTTPException(400, "Missing X-WeBuilt-Action: update")
    require_admin(request)
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
        "discounts": {
            "allow_one_off": (pc.get("discounts") or {}).get("allow_one_off", True),
            "presets": [
                {"id": k, "label": v.get("label", k), "label_th": v.get("label_th"),
                 "percent": v.get("percent"), "amount": v.get("amount")}
                for k, v in ((pc.get("discounts") or {}).get("presets") or {}).items()
            ],
        },
        "materials": sorted((config["filaments"].get("materials") or {}).keys()),
        "labour_rate_per_hour": pc.get("labour_rate_per_hour"),
        "payment_methods": config["receipt"].get("payment_methods") or [],
        "currency": pc.get("currency", "THB"),
    }


@app.get("/api/config")
def get_config(request: Request):
    """Includes supplier costs, so admin-only on the team server."""
    require_admin(request)
    return {
        "pricing": config["pricing"],
        "printers": config["printers"],
        "filaments": config["filaments"],
    }


@app.post("/api/config/reload")
def reload_config(request: Request):
    require_admin(request)
    changed = config.load(force=True)
    return {"reloaded": changed}


@app.post("/api/quote")
async def quote_endpoint(
    request: Request,
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
    discount: str | None = Form(None, description="preset key"),
    discount_percent: float | None = Form(None),
    discount_amount: float | None = Form(None),
    discount_reason: str | None = Form(None),
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
    uploads: list[tuple[str, bytes]] = []
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
        uploads.append((name, data))
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
            discount={"preset": discount or None, "percent": discount_percent,
                      "amount": discount_amount, "reason": discount_reason},
        )
    except pricing.QuoteInputError as exc:
        raise HTTPException(422, str(exc)) from exc
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
        payload["quote_id"], payload["receipt_no"], number_note = log_quote(
            payload["file"]["name"], quote,
            chosen_printer, chosen_rush, chosen_delivery,
            customer={"customer_name": customer_name,
                      "customer_contact": customer_contact,
                      "notes": notes, "receipt_no": receipt_no or None},
            thumbnails=thumbs,
            created_by=actor(request),
            files=uploads,
        )
        payload["receipt_url"] = f"/receipt/{payload['quote_id']}"
        if number_note:
            payload["warnings"].insert(0, number_note)
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

    # --- discount: a preset key, or a one-off percent/amount with a reason ---
    discount: str | None = None
    discount_percent: float | None = None
    discount_amount: float | None = None
    discount_reason: str | None = None
    save: bool = True

    @property
    def seconds(self) -> float:
        return self.hours * 3600 + self.minutes * 60


@app.post("/api/quote/manual")
def quote_manual(body: ManualQuote, request: Request):
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
            discount={"preset": body.discount or None, "percent": body.discount_percent,
                      "amount": body.discount_amount, "reason": body.discount_reason},
        )
    except pricing.QuoteInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(500, f"Pricing config problem: {exc}") from exc

    payload = quote.to_dict()
    payload["app_version"] = APP_VERSION
    # A broken edit to a config file is reported on every quote until fixed.
    payload["warnings"] = list(config.errors.values()) + payload["warnings"]
    payload["file"] = {"name": body.label or "manual entry",
                       "parsed_by": "manual entry"}

    if body.save:
        payload["quote_id"], payload["receipt_no"], number_note = log_quote(
            body.label or "manual entry", quote,
            chosen_printer, chosen_rush, chosen_delivery,
            customer={"customer_name": body.customer_name,
                      "customer_contact": body.customer_contact,
                      "notes": body.notes, "receipt_no": body.receipt_no or None},
            created_by=actor(request),
        )
        payload["receipt_url"] = f"/receipt/{payload['quote_id']}"
        if number_note:
            payload["warnings"].insert(0, number_note)
    attach_thumbnails(payload, {}, payload.get("quote_id"))   # manual: no pictures

    return JSONResponse(payload)


@app.get("/api/orders")
def list_orders(status: str | None = None, q: str | None = None,
                limit: int = 50, offset: int = 0):
    """Newest first. `status` is a status key or 'open' (not closed); `q`
    searches receipt number, customer, file name and notes. The total count
    is in the X-Total-Count header, so the body stays a plain list."""
    items, total = orders.list_orders(status, q, limit, offset)
    for it in items:
        it["status_label"] = orders.status_label(it["status"])
    return JSONResponse(items, headers={"X-Total-Count": str(total)})


@app.get("/api/statuses")
def list_statuses():
    return [{"id": k, "label": v.get("label", k), "label_th": v.get("label_th"),
             "closes": bool(v.get("closes")), "pick_only": bool(v.get("pick_only"))}
            for k, v in orders.statuses().items()]


@app.get("/api/orders/{quote_id}")
def get_order(quote_id: int):
    try:
        order = orders.get_order(quote_id)
    except orders.OrderError as exc:
        raise HTTPException(404, str(exc)) from exc
    order["status_label"] = orders.status_label(order["status"])
    order["thumbnails"] = [thumbnail_url(quote_id, p) for p in order["thumbnails"]]
    order["receipt_url"] = f"/receipt/{quote_id}"
    return order


class OrderChange(BaseModel):
    status: str | None = None
    handled_by: str | None = None
    notes: str | None = None
    customer_name: str | None = None
    customer_contact: str | None = None


@app.patch("/api/orders/{quote_id}")
def change_order(quote_id: int, body: OrderChange, request: Request):
    try:
        orders.update_order(quote_id, body.model_dump(exclude_none=True), actor(request))
    except orders.OrderError as exc:
        code = 404 if str(exc).startswith("No order") else 422
        raise HTTPException(code, str(exc)) from exc
    return get_order(quote_id)


@app.delete("/api/orders/{quote_id}")
def delete_order(quote_id: int, request: Request, confirm: str = ""):
    """Delete for good. Admins only on the team server (members cancel
    instead), and `confirm` must be the order's receipt number."""
    require_admin(request)
    try:
        return orders.delete_order(quote_id, actor(request), confirm)
    except orders.OrderError as exc:
        code = 404 if str(exc).startswith("No order") else 422
        raise HTTPException(code, str(exc)) from exc


@app.post("/api/orders/{quote_id}/files")
async def attach_to_order(quote_id: int, request: Request,
                          files: list[UploadFile] = File(...)):
    """Add files to an order after quoting: the customer's original model, a
    photo, a revised slice."""
    data = [(f.filename or "file", await f.read()) for f in files]
    if not any(d for _, d in data):
        raise HTTPException(400, "The files are empty.")
    try:
        return orders.attach_files(quote_id, data, actor(request))
    except orders.OrderError as exc:
        code = 404 if str(exc).startswith("No order") else 413
        raise HTTPException(code, str(exc)) from exc


@app.get("/api/orders/{quote_id}/files/{file_id}")
def download_file(quote_id: int, file_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM order_files WHERE id = ? AND quote_id = ?",
                           (file_id, quote_id)).fetchone()
    if row is None:
        raise HTTPException(404, "No such file.")
    if row["deleted_at"]:
        raise HTTPException(410, "This file was removed after the order closed.")
    path = orders.file_path(quote_id, row["stored_as"])
    if not path.exists():
        raise HTTPException(404, "The file is missing from the server.")
    # Always a download, never shown in the browser: these are customers' files.
    return FileResponse(path, filename=row["name"], media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.post("/api/orders/{quote_id}/outcome")
def set_outcome(quote_id: int, request: Request, outcome: str = Form(...)):
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
        orders.add_event(conn, quote_id, actor(request), "print outcome", outcome)
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
    request: Request,
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
    try:
        paid_at = orders.record_payment(quote_id, status, method, actor(request))
    except orders.OrderError as exc:
        raise HTTPException(404, f"No quote {quote_id}") from exc
    return {"id": quote_id, "payment_status": status, "payment_method": method,
            "paid_at": paid_at}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/orders")
def orders_page():
    return FileResponse(BASE / "static" / "orders.html")


@app.get("/orders/{quote_id:int}")
def order_page(quote_id: int):
    return FileResponse(BASE / "static" / "order.html")


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "static" / "admin.html")


@app.get("/account")
def account_page():
    return FileResponse(BASE / "static" / "account.html")
