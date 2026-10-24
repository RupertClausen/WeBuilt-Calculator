"""Team server: logins, the gate, roles, and who-did-what."""
import os, pathlib, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-team-"))
(TMP / "local").mkdir()
(TMP / "local" / "server.yaml").write_text("enabled: true\nmax_failed_logins: 3\n", encoding="utf-8")
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod
import auth

FX = ROOT / "tests" / "fixtures"
H = {"X-WeBuilt": "1"}
auth.create_user("boss", "boss-password-1", "admin", "The Boss")
auth.create_user("pim", "pim-password-1", "member", "Pim")


def client(user=None, pw=None):
    c = TestClient(appmod.app)
    if user:
        r = c.post("/api/login", json={"username": user, "password": pw}, headers=H)
        assert r.status_code == 200, r.text
    return c


def quote(c, **extra):
    return c.post("/api/quote", headers=H, data={"client_profile": "engineering", **extra},
                  files=[("files", ("job.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))])


def test_everything_needs_a_login():
    c = client()
    assert c.get("/api/orders").status_code == 401
    assert c.get("/api/options").status_code == 401
    assert c.get("/receipt/1").status_code in (303, 307) or \
        c.get("/receipt/1", follow_redirects=False).status_code == 303
    r = c.get("/orders", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/orders"
    for open_path in ("/login", "/healthz", "/api/me", "/static/common.css"):
        assert c.get(open_path).status_code == 200, open_path
    print("  gate            API 401, pages redirect to /login?next=…, login/health/static open")


def test_changes_need_the_header():
    c = client("pim", "pim-password-1")
    r = c.post("/api/quote/manual", json={"filaments": [{"type": "PLA", "grams": 5}], "hours": 1})
    assert r.status_code == 403 and "X-WeBuilt" in r.json()["detail"]
    r = c.post("/api/login", json={"username": "pim", "password": "pim-password-1"})
    assert r.status_code == 403                                  # even logging in
    assert quote(c).status_code == 200
    print("  csrf            POST without X-WeBuilt -> 403 (login too); with it -> 200")


def test_session_cookie_is_safe():
    c = TestClient(appmod.app)
    r = c.post("/api/login", json={"username": "pim", "password": "pim-password-1"}, headers=H)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    token = r.cookies.get(auth.COOKIE)
    import sqlite3, hashlib
    with sqlite3.connect(appmod.DB_PATH) as db:
        stored = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
    assert token not in stored and hashlib.sha256(token.encode()).hexdigest() in stored
    print("  cookie          HttpOnly + SameSite=Lax; database keeps only the token's hash")


def test_wrong_password_and_lockout():
    c = TestClient(appmod.app)
    bad = lambda u, p: c.post("/api/login", json={"username": u, "password": p}, headers=H)
    r1, r2 = bad("pim", "nope"), bad("nobody", "nope")
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]            # never says which
    auth.create_user("locky", "locky-password-1")
    for _ in range(3):
        bad("locky", "wrong")
    r = bad("locky", "locky-password-1")                          # right password, but locked
    assert r.status_code == 401 and "Too many" in r.json()["detail"]
    print("  lockout         same message for unknown user / wrong password; 3 fails lock it")


def test_roles():
    pim, boss = client("pim", "pim-password-1"), client("boss", "boss-password-1")
    for path in ("/api/config", "/api/admin/users", "/api/admin/status"):
        assert pim.get(path).status_code == 403, path
        assert boss.get(path).status_code == 200, path
    assert pim.post("/api/admin/users", headers=H, json={
        "username": "x1", "password": "xxxxxxxxx"}).status_code == 403
    assert pim.get("/api/orders").status_code == 200              # members do orders
    print("  roles           supplier costs, users and status: admin only; orders: everyone")


def test_admin_manages_users():
    boss = client("boss", "boss-password-1")
    r = boss.post("/api/admin/users", headers=H, json={
        "username": "Nok", "password": "nok-password-1", "display_name": "Nok"})
    assert r.status_code == 200 and r.json()["username"] == "nok"
    assert boss.post("/api/admin/users", headers=H, json={
        "username": "nok", "password": "nok-password-1"}).status_code == 422
    assert boss.post("/api/admin/users", headers=H, json={
        "username": "short", "password": "123"}).status_code == 422
    nok_id = r.json()["id"]
    nok = client("nok", "nok-password-1")
    boss.patch(f"/api/admin/users/{nok_id}", headers=H, json={"active": False})
    assert nok.get("/api/orders").status_code == 401             # switched off: out at once
    me = boss.get("/api/me").json()["user"]
    r = boss.patch(f"/api/admin/users/{me['id']}", headers=H, json={"role": "member"})
    assert r.status_code == 422 and "only active admin" in r.json()["detail"]
    print("  admin           add (lower-cased), duplicate/short refused, switch-off signs out, "
          "last admin protected")


def test_own_password_change_keeps_this_session_only():
    auth.create_user("mai", "mai-password-1")
    here, other = client("mai", "mai-password-1"), client("mai", "mai-password-1")
    r = here.post("/api/me/password", headers=H,
                  json={"current": "mai-password-1", "new": "mai-password-2"})
    assert r.status_code == 200
    assert here.get("/api/orders").status_code == 200            # still in
    assert other.get("/api/orders").status_code == 401           # signed out elsewhere
    bad = here.post("/api/me/password", headers=H, json={"current": "wrong", "new": "whatever123"})
    assert bad.status_code == 422
    client("mai", "mai-password-2")
    print("  own password    this device stays in, other devices signed out, wrong current refused")


def test_logout():
    c = client("pim", "pim-password-1")
    assert c.post("/api/logout", headers=H).status_code == 200
    assert c.get("/api/orders").status_code == 401
    print("  logout          session ends")


def test_quotes_record_who():
    pim = client("pim", "pim-password-1")
    d = quote(pim).json()
    o = pim.get(f"/api/orders/{d['quote_id']}").json()
    assert o["created_by"] == "pim" and o["status"] == "quoted"
    assert o["events"][0]["user"] == "pim" and o["events"][0]["action"] == "created"
    assert pim.get("/api/me").json()["user"]["username"] == "pim"
    print(f"  who             quote {d['receipt_no']} created by pim, history starts with it")


def test_users_list_for_assignment():
    pim = client("pim", "pim-password-1")
    names = {u["username"] for u in pim.get("/api/users").json()}
    assert {"boss", "pim"} <= names and "nok" not in names       # switched-off users hidden
    assert all(set(u) == {"username", "display_name"} for u in pim.get("/api/users").json())
    print("  teammates       active names only, no roles or login times")


def test_first_admin_hint():
    import sqlite3
    with sqlite3.connect(appmod.DB_PATH) as db:
        n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert TestClient(appmod.app).get("/api/me").json()["needs_first_admin"] is (n == 0)
    print("  first admin     /api/me says whether manage.py add-user is still needed")


def test_only_admins_delete():
    pim, boss = client("pim", "pim-password-1"), client("boss", "boss-password-1")
    d = quote(pim).json()
    assert pim.delete(f"/api/orders/{d['quote_id']}", headers=H,
                      params={"confirm": d["receipt_no"]}).status_code == 403
    assert boss.delete(f"/api/orders/{d['quote_id']}",
                       params={"confirm": d["receipt_no"]}).status_code == 403   # header still needed
    r = boss.delete(f"/api/orders/{d['quote_id']}", headers=H, params={"confirm": d["receipt_no"]})
    assert r.status_code == 200
    gone = boss.get("/api/admin/status").json()["recently_deleted"][0]
    assert gone["receipt_no"] == d["receipt_no"] and gone["deleted_by"] == "boss"
    print("  delete          member 403, admin needs the header too, then deletes; shows on Admin as by boss")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} team-server tests\n")
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
