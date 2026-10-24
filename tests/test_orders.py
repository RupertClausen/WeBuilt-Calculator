"""Orders: saved files, attachments, status workflow, history, search and
file retention."""
import os, pathlib, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-orders-"))
(TMP / "local").mkdir()
(TMP / "local" / "server.yaml").write_text("files:\n  max_attachment_mb: 1\n", encoding="utf-8")
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod
import core
import orders

FX = ROOT / "tests" / "fixtures"
c = TestClient(appmod.app)
CHANGES = []
orders.on_change(lambda: CHANGES.append(1))


def upload(names, **data):
    files = [("files", (n if isinstance(n, str) else n[0],
                        (FX / (n if isinstance(n, str) else n[1])).read_bytes())) for n in names]
    r = c.post("/api/quote", data={"client_profile": "engineering", **data}, files=files)
    assert r.status_code == 200, r.text
    return r.json()


def test_uploaded_files_are_kept_with_the_order():
    names = ["orca_petg_single.gcode", "bambu_two_plates.gcode.3mf"]
    d = upload(names)
    o = c.get(f"/api/orders/{d['quote_id']}").json()
    assert [f["name"] for f in o["files"]] == names and all(f["kind"] == "sliced" for f in o["files"])
    for f, n in zip(o["files"], names):
        r = c.get(f"/api/orders/{d['quote_id']}/files/{f['id']}")
        assert r.content == (FX / n).read_bytes()
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in r.headers["content-disposition"] and n in r.headers["content-disposition"]
    on_disk = sorted(p.name for p in orders.order_dir(d["quote_id"]).iterdir())
    assert len(on_disk) == 2 and not any(n in p for p in on_disk for n in names)
    print(f"  saved files     2 files byte-identical, download-only, stored under generated names")


def test_hostile_file_name_lands_safely():
    d = upload([("../../../evil.gcode", "orca_tiny_pla.gcode")])
    o = c.get(f"/api/orders/{d['quote_id']}").json()
    assert o["files"][0]["name"] == "evil.gcode"
    stored = list(orders.order_dir(d["quote_id"]).iterdir())
    assert len(stored) == 1 and stored[0].parent == orders.order_dir(d["quote_id"])
    assert not (core.FILES_DIR.parent / "evil.gcode").exists()
    assert not list(TMP.rglob("evil.gcode"))
    print("  hostile name    '../../../evil.gcode' stored inside its order folder, shown as evil.gcode")


def test_manual_quotes_have_no_files():
    r = c.post("/api/quote/manual", json={"filaments": [{"type": "PLA", "grams": 10}], "hours": 1}).json()
    assert c.get(f"/api/orders/{r['quote_id']}").json()["files"] == []
    print("  manual          no files, no folder")


def test_attachments():
    d = upload(["orca_tiny_pla.gcode"])
    qid = d["quote_id"]
    r = c.post(f"/api/orders/{qid}/files", files=[("files", ("photo.jpg", b"\xff\xd8\xff" + b"x" * 100))])
    assert r.status_code == 200 and r.json()[0]["kind"] == "attachment"
    big = c.post(f"/api/orders/{qid}/files", files=[("files", ("huge.stl", b"x" * (1024 * 1024 + 1)))])
    assert big.status_code == 413
    o = c.get(f"/api/orders/{qid}").json()
    assert [f["name"] for f in o["files"]] == ["orca_tiny_pla.gcode", "photo.jpg"]
    assert any(e["action"] == "attached" and "photo.jpg" in e["detail"] for e in o["events"])
    assert len(list(orders.order_dir(qid).iterdir())) == 2          # oversized one not left behind
    assert c.post("/api/orders/999999/files", files=[("files", ("a", b"x"))]).status_code == 404
    print("  attachments     added with history; over the limit refused and nothing left on disk")


def test_status_workflow_and_history():
    qid = upload(["orca_tiny_pla.gcode"])["quote_id"]
    n0 = len(CHANGES)
    for st in ("queued", "printing", "done"):
        r = c.patch(f"/api/orders/{qid}", json={"status": st})
        assert r.status_code == 200 and r.json()["status"] == st
    o = r.json()
    assert o["closed_at"] and o["status_label"] == "Done"
    assert c.patch(f"/api/orders/{qid}", json={"status": "printing"}).json()["closed_at"] is None
    assert c.patch(f"/api/orders/{qid}", json={"status": "teleported"}).status_code == 422
    assert c.patch(f"/api/orders/{qid}", json={}).status_code == 422
    assert c.patch("/api/orders/999999", json={"status": "done"}).status_code == 404
    c.patch(f"/api/orders/{qid}", json={"handled_by": "pim", "notes": "white PLA only",
                                        "customer_name": "K'Test", "customer_contact": "LINE @t"})
    o = c.get(f"/api/orders/{qid}").json()
    acts = [e["action"] for e in o["events"]]
    assert acts[:5] == ["created", "status", "status", "status", "status"]
    assert {"handled by", "notes", "customer name", "customer contact"} <= set(acts)
    assert "Printing → Done" in [e["detail"] for e in o["events"]]
    assert o["handled_by"] == "pim" and o["notes"] == "white PLA only"
    assert len(CHANGES) > n0
    print("  workflow        quoted→queued→printing→done sets closed_at; reopening clears it; "
          "every change in history")


def test_payment_goes_into_history():
    qid = upload(["orca_tiny_pla.gcode"])["quote_id"]
    assert c.post(f"/api/orders/{qid}/payment", data={"status": "paid", "method": "Cash"}).status_code == 200
    o = c.get(f"/api/orders/{qid}").json()
    assert o["payment_status"] == "paid" and o["events"][-1]["detail"] == "paid (Cash)"
    print("  payment         recorded with method in history")


def test_list_filter_search_and_count():
    a = upload(["orca_tiny_pla.gcode"], customer_name="Unique Somchai")["quote_id"]
    b = upload(["orca_tiny_pla.gcode"])["quote_id"]
    c.patch(f"/api/orders/{b}", json={"status": "cancelled"})
    r = c.get("/api/orders", params={"q": "Unique Somchai"})
    assert [o["id"] for o in r.json()] == [a] and r.headers["x-total-count"] == "1"
    open_ids = {o["id"] for o in c.get("/api/orders", params={"status": "open", "limit": 500}).json()}
    assert a in open_ids and b not in open_ids
    assert [o["id"] for o in c.get("/api/orders", params={"status": "cancelled"}).json()][0] == b
    page2 = c.get("/api/orders", params={"limit": 2, "offset": 2}).json()
    assert len(page2) == 2
    st = c.get("/api/statuses").json()
    assert [s["id"] for s in st][:3] == ["quoted", "queued", "printing"]
    assert {s["id"] for s in st if s["closes"]} == {"done", "delivered", "cancelled"}
    # the list's one-click "next step" button never lands on these
    assert {s["id"] for s in st if s["pick_only"]} == {"cancelled"}
    print(f"  list            search, 'open' hides closed, status filter, paging, X-Total-Count")


def test_retention_removes_files_not_orders():
    qid = upload(["bambu_two_plates.gcode.3mf"])["quote_id"]
    keep = upload(["orca_tiny_pla.gcode"])["quote_id"]           # stays open
    c.patch(f"/api/orders/{qid}", json={"status": "delivered"})
    later = datetime.now(timezone.utc) + timedelta(days=91)
    assert orders.delete_expired_files(0, later) == 0             # 0 = keep forever
    n = orders.delete_expired_files(90, later)
    assert n >= 1
    o = c.get(f"/api/orders/{qid}").json()
    assert all(f["deleted_at"] for f in o["files"])
    assert c.get(f"/api/orders/{qid}/files/{o['files'][0]['id']}").status_code == 410
    assert o["receipt_no"] and o["total"] and o["thumbnails"]     # the order stays whole
    assert c.get(o["thumbnails"][0]).status_code == 200
    assert not orders.order_dir(qid).exists()
    assert o["events"][-1]["action"] == "files removed"
    assert c.get(f"/api/orders/{keep}").json()["files"][0]["deleted_at"] is None
    print(f"  retention       closed 91 days: {n} file(s) removed, order/prices/pictures kept, "
          "download says 410; open orders untouched")


def test_failed_save_leaves_nothing():
    real = orders.save_files
    def boom(conn, quote_id, files, user, kind="sliced"):
        real(conn, quote_id, files, user, kind)          # write to disk, then fail
        raise OSError("disk full")
    orders.save_files = boom
    before = c.get("/api/orders").headers["x-total-count"]
    try:
        upload(["orca_tiny_pla.gcode"])
    except Exception:
        pass
    finally:
        orders.save_files = real
    assert c.get("/api/orders").headers["x-total-count"] == before
    with sqlite3.connect(core.DB_PATH) as db:
        last = db.execute("SELECT MAX(id) FROM quotes").fetchone()[0]
    assert not orders.order_dir(last + 1).exists()
    print("  failure         save fails halfway -> no order row, no files on disk")


def test_delete_order():
    d = upload(["bambu_two_plates.gcode.3mf"], customer_name="K'Gone")
    qid, no = d["quote_id"], d["receipt_no"]
    folder = orders.order_dir(qid)
    assert folder.exists()
    assert c.delete(f"/api/orders/{qid}").status_code == 422                     # no confirmation
    assert c.delete(f"/api/orders/{qid}", params={"confirm": "WB-wrong"}).status_code == 422
    assert c.get(f"/api/orders/{qid}").status_code == 200                        # still there
    n0 = len(CHANGES)
    r = c.delete(f"/api/orders/{qid}", params={"confirm": no})
    assert r.status_code == 200 and r.json()["receipt_no"] == no
    assert c.get(f"/api/orders/{qid}").status_code == 404
    assert not folder.exists() and len(CHANGES) > n0
    with sqlite3.connect(core.DB_PATH) as db:
        for table in ("plate_thumbnails", "order_files", "order_events"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE quote_id = ?", (qid,)).fetchone()[0] == 0, table
        rec = db.execute("SELECT receipt_no, deleted_by, summary FROM deleted_orders WHERE quote_id = ?",
                         (qid,)).fetchone()
    assert rec[0] == no and rec[1] == "local" and "K'Gone" in rec[2]
    assert c.delete("/api/orders/999999", params={"confirm": "x"}).status_code == 404
    print(f"  delete          needs the receipt number; removes row, files, pictures, history; "
          f"record kept for {no}")


def test_deleted_numbers_are_never_reissued():
    last = upload(["orca_tiny_pla.gcode"])
    c.delete(f"/api/orders/{last['quote_id']}", params={"confirm": last["receipt_no"]})
    nxt = upload(["orca_tiny_pla.gcode"])
    assert nxt["receipt_no"] != last["receipt_no"]
    assert int(nxt["receipt_no"].rsplit("-", 1)[1]) > int(last["receipt_no"].rsplit("-", 1)[1])
    typed = upload(["orca_tiny_pla.gcode"], receipt_no=last["receipt_no"])
    assert typed["receipt_no"] != last["receipt_no"]
    assert any("already used" in w for w in typed["warnings"])
    print(f"  no reuse        deleted {last['receipt_no']} -> next is {nxt['receipt_no']}; "
          "typing the deleted number falls back with a warning")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} order tests\n")
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
