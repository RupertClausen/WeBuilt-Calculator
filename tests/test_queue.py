"""Printer categories, printers and the plate queue: plates join their
category's queue when an order is queued, go to the next free printer or a
pinned one, and move the order to Printing and Ready by themselves."""
import os, pathlib, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-queue-"))
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod

FX = ROOT / "tests" / "fixtures"
c = TestClient(appmod.app)


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def printer(name, cat="normal"):
    return ok(c.post("/api/printers", json={"name": name, "category": cat, "model": "test"}))["id"]


P2S1, P2S2, TRI = printer("P2S #1"), printer("P2S #2"), printer("Trident #1", "large")


def order(fixture="orca_tiny_pla.gcode", printer_cat="normal", queued=True):
    d = c.post("/api/quote", data={"printer": printer_cat},
               files=[("files", (fixture, (FX / fixture).read_bytes()))]).json()
    if queued:
        ok(c.patch(f"/api/orders/{d['quote_id']}", json={"status": "queued"}))
    return d["quote_id"]


def cat(board, key):
    return next(x for x in board["categories"] if x["id"] == key)


def card(board, pid):
    return next(p for x in board["categories"] for p in x["printers"] if p["id"] == pid)


def test_printers_belong_to_categories():
    b = ok(c.get("/api/printers"))
    assert [p["name"] for p in cat(b, "normal")["printers"]] == ["P2S #1", "P2S #2"]
    assert [p["name"] for p in cat(b, "large")["printers"]] == ["Trident #1"]
    assert c.post("/api/printers", json={"name": "p2s #1", "category": "normal"}).status_code == 409
    assert c.post("/api/printers", json={"name": "X", "category": "resin"}).status_code == 409
    print("  categories      printers grouped by printers.yaml category; duplicate names "
          "and unknown categories refused")


def test_queued_order_becomes_jobs_once():
    qid = order("bambu_two_plates.gcode.3mf")
    o = ok(c.get(f"/api/orders/{qid}"))
    assert len(o["jobs"]) == 2 and {j["status"] for j in o["jobs"]} == {"waiting"}
    assert all(j["category"] == "normal" for j in o["jobs"])
    # back to awaiting payment and into the queue again: no duplicates
    c.patch(f"/api/orders/{qid}", json={"status": "awaiting_payment"})
    c.patch(f"/api/orders/{qid}", json={"status": "queued"})
    assert len(ok(c.get(f"/api/orders/{qid}"))["jobs"]) == 2
    b = ok(c.get("/api/printers"))
    assert {j["quote_id"] for j in cat(b, "normal")["waiting"]} >= {qid}
    # a large-printer plate waits in Large, not Normal
    big = order(printer_cat="large")
    b = ok(c.get("/api/printers"))
    assert big in {j["quote_id"] for j in cat(b, "large")["waiting"]}
    assert big not in {j["quote_id"] for j in cat(b, "normal")["waiting"]}
    print("  jobs            2-plate order → 2 waiting jobs in its category, once; Large stays Large")


def test_print_flow_moves_the_order():
    qid = order()
    job = ok(c.get(f"/api/orders/{qid}"))["jobs"][0]["id"]
    # wrong category refused
    r = c.post(f"/api/jobs/{job}/start", json={"printer_id": TRI})
    assert r.status_code == 409 and "Large" in r.json()["detail"]
    ok(c.post(f"/api/jobs/{job}/start", json={"printer_id": P2S2}))
    o = ok(c.get(f"/api/orders/{qid}"))
    assert o["status"] == "printing" and o["jobs"][0]["printer_name"] == "P2S #2"
    # one plate at a time per printer
    other = order()
    ojob = ok(c.get(f"/api/orders/{other}"))["jobs"][0]["id"]
    assert c.post(f"/api/jobs/{ojob}/start", json={"printer_id": P2S2}).status_code == 409
    assert card(ok(c.get("/api/printers")), P2S2)["current"]["quote_id"] == qid
    ok(c.post(f"/api/jobs/{job}/finish", json={"outcome": "done"}))
    o = ok(c.get(f"/api/orders/{qid}"))
    assert o["status"] == "ready"
    assert any("Printing → Ready" == e["detail"] for e in o["events"])
    print("  flow            start → order Printing; one plate per printer; last plate done → Ready")


def test_failure_counts_and_reprints_first():
    first = order()
    second = order()
    j2 = ok(c.get(f"/api/orders/{second}"))["jobs"][0]["id"]
    j1 = ok(c.get(f"/api/orders/{first}"))["jobs"][0]["id"]
    ok(c.post(f"/api/jobs/{j2}/start", json={"printer_id": P2S1}))
    ok(c.post(f"/api/jobs/{j2}/finish", json={"outcome": "failed"}))
    o = ok(c.get(f"/api/orders/{second}"))
    assert [j["status"] for j in o["jobs"]] == ["failed", "waiting"]
    assert o["status"] == "printing"                    # not Ready: the reprint is still to do
    b = ok(c.get("/api/printers"))
    assert cat(b, "normal")["waiting"][0]["quote_id"] == second       # reprint goes first
    st = card(b, P2S1)["stats"]
    assert st["failed"] == 1 and st["failure_rate"] == 1.0
    # start-next takes the reprint
    nxt = ok(c.post(f"/api/printers/{P2S1}/start-next"))["job_id"]
    assert ok(c.get(f"/api/orders/{second}"))["jobs"][1]["id"] == nxt
    ok(c.post(f"/api/jobs/{nxt}/finish", json={"outcome": "done"}))
    assert card(ok(c.get("/api/printers")), P2S1)["stats"]["failure_rate"] == 0.5
    assert ok(c.get(f"/api/orders/{first}"))["jobs"][0]["id"] == j1
    print("  failure         counted against P2S #1 (1 of 2 = 50%); reprint queued first, "
          "order stays Printing until it's done")


def test_pin_reorder_and_eta():
    a, b2 = order(), order()
    ja = ok(c.get(f"/api/orders/{a}"))["jobs"][0]["id"]
    jb = ok(c.get(f"/api/orders/{b2}"))["jobs"][0]["id"]
    for j in (ja, jb):
        ok(c.post(f"/api/jobs/{j}/pin", json={"printer_id": P2S2}))
    q = card(ok(c.get("/api/printers")), P2S2)["queue"]
    assert [x["id"] for x in q][-2:] == [ja, jb]
    ok(c.post(f"/api/jobs/{jb}/move", json={"direction": "up"}))
    q = card(ok(c.get("/api/printers")), P2S2)["queue"]
    assert [x["id"] for x in q][-2:] == [jb, ja]
    assert q[-1]["starts_at"] >= q[-2]["starts_at"]
    assert c.post(f"/api/jobs/{ja}/pin", json={"printer_id": TRI}).status_code == 409
    # a switched-off printer hands its pinned plates back to the category
    ok(c.patch(f"/api/printers/{P2S2}", json={"active": False}))
    assert card(ok(c.get("/api/printers")), P2S2)["queue"] == []
    ok(c.patch(f"/api/printers/{P2S2}", json={"active": True}))
    # paused printers take no new plate
    ok(c.patch(f"/api/printers/{P2S1}", json={"paused": True}))
    assert c.post(f"/api/jobs/{ja}/start", json={"printer_id": P2S1}).status_code == 409
    ok(c.patch(f"/api/printers/{P2S1}", json={"paused": False}))
    print("  pinning         pin, ↑ reorders, start times follow; off returns plates; paused refuses")


def test_cancelling_takes_plates_out():
    qid = order()
    ok(c.patch(f"/api/orders/{qid}", json={"status": "cancelled"}))
    o = ok(c.get(f"/api/orders/{qid}"))
    assert {j["status"] for j in o["jobs"]} == {"cancelled"}
    assert qid not in {j["quote_id"] for j in cat(ok(c.get("/api/printers")), "normal")["waiting"]}
    print("  cancel          a cancelled order's plates leave the queue")


def test_printers_page_served():
    r = c.get("/printers")
    assert r.status_code == 200 and "Printers" in r.text
    print("  page            /printers served")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            import traceback; traceback.print_exc()
            print(f"  FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
