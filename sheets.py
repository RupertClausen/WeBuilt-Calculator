"""Google Sheet mirror: the app leads, its tab in the spreadsheet follows.

Every order is written to one tab ("Orders (app)" by default) of the queue
spreadsheet, with the same columns the team already uses. The app is the
source of truth: each sync rewrites the whole tab, so edits made in the sheet
are undone on the next sync. Other tabs are never touched.

  * Changes are coalesced: a burst of edits becomes one write a few seconds
    later, and the tab is also rewritten every `resync_minutes`.
  * Failures are retried with growing pauses (30 s up to 15 min) and shown
    on the admin page; nothing in the app waits on Google.
  * Values are written RAW, so a customer name such as "=IMPORTXML(...)"
    stays text and never becomes a live formula in your spreadsheet.
  * New rows are written before leftovers are cleared, so the tab is never
    briefly empty.

Access is a Google service account: share the spreadsheet with its email as
an Editor, and put its key file at `credentials_file` (in local/, so it never
ships or gets committed).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

from core import BASE, config, db, get_state, set_state, utcnow
import orders
import receipt as receipt_doc

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Order ID", "Order date", "Customer Name", "Filament Type", "Printer type",
          "Price (THB)", "Payment", "Status", "Delivery type", "Plates",
          "Created by", "Handled by", "Link"]


class SheetError(Exception):
    """A message an admin can act on."""


class SheetsClient(Protocol):
    def tab_titles(self) -> list[str]: ...
    def add_tab(self, title: str) -> None: ...
    def write(self, a1: str, rows: list[list]) -> None: ...
    def clear(self, a1: str) -> None: ...


def _a1(tab: str, cells: str) -> str:
    return "'" + tab.replace("'", "''") + "'!" + cells


# ---------------------------------------------------------------------------
# the real client
# ---------------------------------------------------------------------------

class GoogleSheetsClient:
    """Talks to the Sheets API with a service account. `session` can be
    passed in by tests; otherwise one is built from the key file."""

    def __init__(self, spreadsheet_id: str, credentials_file: Path, session=None):
        if not spreadsheet_id:
            raise SheetError("No spreadsheet_id in server.yaml.")
        self.sid = spreadsheet_id
        self.email = None
        if session is None:
            if not credentials_file.exists():
                raise SheetError(f"The service-account key is missing: put it at "
                                 f"{credentials_file}.")
            from google.oauth2 import service_account           # only when used
            from google.auth.transport.requests import AuthorizedSession
            try:
                creds = service_account.Credentials.from_service_account_file(
                    str(credentials_file), scopes=SCOPES)
            except (ValueError, KeyError) as exc:
                raise SheetError(f"The service-account key can't be read: {exc}") from exc
            self.email = creds.service_account_email
            session = AuthorizedSession(creds)
        self.session = session

    def _call(self, method: str, url: str, **kw):
        try:
            r = self.session.request(method, url, timeout=30, **kw)
        except Exception as exc:
            raise SheetError(f"Could not reach Google: {exc}") from exc
        if r.status_code in (401, 403):
            who = f" ({self.email})" if self.email else ""
            raise SheetError(f"Google refused access. Share the spreadsheet with the "
                             f"service account{who} as an Editor.")
        if r.status_code == 404:
            raise SheetError("Spreadsheet not found: check spreadsheet_id in server.yaml.")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise SheetError(f"Google answered {r.status_code}: {msg}")
        return r.json() if r.content else {}

    def tab_titles(self) -> list[str]:
        meta = self._call("GET", f"{API}/{self.sid}", params={"fields": "sheets.properties.title"})
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def add_tab(self, title: str) -> None:
        self._call("POST", f"{API}/{self.sid}:batchUpdate",
                   json={"requests": [{"addSheet": {"properties": {"title": title}}}]})

    def write(self, a1: str, rows: list[list]) -> None:
        # RAW: values are stored exactly as given; nothing is parsed as a formula.
        self._call("PUT", f"{API}/{self.sid}/values/{quote(a1, safe='')}",
                   params={"valueInputOption": "RAW"},
                   json={"range": a1, "majorDimension": "ROWS", "values": rows})

    def clear(self, a1: str) -> None:
        self._call("POST", f"{API}/{self.sid}/values/{quote(a1, safe='')}:clear", json={})


# ---------------------------------------------------------------------------
# what goes in the sheet
# ---------------------------------------------------------------------------

def sheet_rows() -> list[list]:
    """Header plus one row per order, oldest first, as the team's sheet is."""
    srv, rc = config["server"], config["receipt"]
    printers = config["printers"].get("printers", {})
    delivery = config["pricing"].get("delivery_options", {})
    tz = receipt_doc.zone(rc.get("timezone", "Asia/Bangkok"))
    base_url = (srv.get("public_url") or "").rstrip("/")
    rows = [HEADER]
    with db() as conn:
        for r in conn.execute(
                "SELECT id, receipt_no, created_at, customer_name, materials, printer, total,"
                " payment_status, status, delivery, plates, created_by, handled_by"
                " FROM quotes ORDER BY id"):
            created = datetime.fromisoformat(r["created_at"]).astimezone(tz)
            pr = printers.get(r["printer"]) or {}
            dl = delivery.get(r["delivery"]) or {}
            rows.append([
                r["receipt_no"] or "",
                created.strftime("%d/%m/%Y"),
                r["customer_name"] or "",
                (r["materials"] or "").replace(",", ", "),
                pr.get("label", r["printer"] or ""),
                round(float(r["total"] or 0), 2),
                "จ่ายแล้ว" if r["payment_status"] == "paid" else "ยังไม่จ่าย",
                orders.status_label(r["status"] or "quoted", "th"),
                dl.get("label_th") or dl.get("label") or (r["delivery"] or ""),
                r["plates"] or 0,
                r["created_by"] or "",
                r["handled_by"] or "",
                f"{base_url}/orders/{r['id']}" if base_url else "",
            ])
    return rows


def sync_once(client: SheetsClient, tab: str) -> int:
    """Rewrite the tab from the app. Returns the number of order rows."""
    if tab not in client.tab_titles():
        client.add_tab(tab)
    rows = sheet_rows()
    client.write(_a1(tab, "A1"), rows)
    # Clear anything below the new last row (orders are never deleted, but
    # the tab may have been edited or pasted into).
    client.clear(_a1(tab, f"A{len(rows) + 1}:Z"))
    # And any columns to the right of ours.
    client.clear(_a1(tab, f"{chr(ord('A') + len(HEADER))}1:Z{len(rows)}"))
    return len(rows) - 1


# ---------------------------------------------------------------------------
# background mirror
# ---------------------------------------------------------------------------

def default_client() -> SheetsClient:
    sc = config["server"].get("sheets") or {}
    path = Path(sc.get("credentials_file") or "local/google-service-account.json")
    if not path.is_absolute():
        path = BASE / path
    return GoogleSheetsClient(sc.get("spreadsheet_id", ""), path)


class SheetMirror:
    DEBOUNCE = 3.0
    BACKOFF = (30, 60, 120, 300, 600, 900)

    def __init__(self, client_factory: Callable[[], SheetsClient] = default_client):
        self.client_factory = client_factory
        self.dirty = threading.Event()
        self.lock = threading.Lock()              # one sync at a time
        self.thread: threading.Thread | None = None
        self.stopping = False

    @staticmethod
    def enabled() -> bool:
        srv = config["server"]
        return bool(srv.get("enabled") and (srv.get("sheets") or {}).get("enabled"))

    def mark_dirty(self) -> None:
        self.dirty.set()

    def status(self) -> dict:
        return {"enabled": self.enabled(), "pending": self.dirty.is_set(),
                **(get_state("sheet", {}) or {})}

    def sync_now(self) -> dict:
        """Run one sync right away (the admin's "Sync now"). Records and
        returns the outcome; never raises."""
        sc = config["server"].get("sheets") or {}
        tab = sc.get("tab") or "Orders (app)"
        prev = get_state("sheet", {}) or {}
        with self.lock:
            started = time.monotonic()
            state = {**prev, "tab": tab}
            try:
                client = self.client_factory()
                # Record the account's email as soon as the key loads: it's
                # what the admin must share the sheet with, and the first
                # sync can't succeed until they have.
                state["service_account"] = getattr(client, "email", None) or prev.get(
                    "service_account")
                n = sync_once(client, tab)
                state.update(last_ok=utcnow(), rows=n, error=None,
                             seconds=round(time.monotonic() - started, 2))
            except Exception as exc:
                state.update(error=str(exc), error_at=utcnow())
            set_state("sheet", state)
        return state

    def run(self) -> None:
        fails = 0
        while not self.stopping:
            resync = float((config["server"].get("sheets") or {}).get("resync_minutes", 30)) * 60
            self.dirty.wait(timeout=resync)
            if self.stopping:
                return
            if not self.enabled():
                self.dirty.clear()
                continue
            time.sleep(self.DEBOUNCE)          # let a burst of changes settle
            self.dirty.clear()
            state = self.sync_now()
            if state.get("error"):
                pause = self.BACKOFF[min(fails, len(self.BACKOFF) - 1)]
                fails += 1
                time.sleep(pause)
                self.dirty.set()                # try again
            else:
                fails = 0

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name="sheet-mirror", daemon=True)
            self.thread.start()
            self.dirty.set()                    # first sync on start

    def stop(self) -> None:
        self.stopping = True
        self.dirty.set()


mirror = SheetMirror()
orders.on_change(mirror.mark_dirty)
