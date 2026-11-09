"""Special additions: named extra work at the engineer's price, added after
the margin, discount, minimum and rounding, and editable on a saved order
until it's paid."""
import json, os, pathlib, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-additions-"))
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod
import pricing
from core import config
from slicefile import FilamentUse, SlicedJob

FX = ROOT / "tests" / "fixtures"
c = TestClient(appmod.app)
CFG = dict(pricing=config["pricing"], printers=config["printers"], filaments=config["filaments"])


def job(grams=120.0, hours=4.0):
    return SlicedJob(filaments=[FilamentUse(type="PLA", grams=grams)], seconds=hours * 3600,
                     plates=1, source="test")


def test_added_after_rounding_at_exact_price():
    base = pricing.build_quote(job(), **CFG)
    q = pricing.build_quote(job(), **CFG, additions=[
        {"label": "Model repair", "amount": 20, "reason": "walls thickened"},
        {"preset": "sanding", "amount": 37.5}])
    assert q.total == base.total + 57.5
    keys = [l.key for l in q.lines]
    assert keys[-2:] == ["addition_1", "addition_2"]
    assert keys.index("rounding" if "rounding" in keys else keys[-3]) < keys.index("addition_1")
    assert abs(sum(l.amount for l in q.lines) - q.total) < 0.005
    bd = q.breakdown
    assert bd["total_before_additions"] == base.total
    assert bd["economics"]["special_additions"] == 57.5 and bd["economics"]["total"] == q.total
    assert bd["special_additions"][1]["label"] == "Sanding"            # from the preset
    assert bd["special_additions"][1]["label_th"] == "ขัดผิวชิ้นงาน"
    # the printing is priced exactly as without them
    assert bd["economics"]["profit"] == base.breakdown["economics"]["profit"]
    print(f"  after rounding  {base.total:.2f} + 20 + 37.50 = {q.total:.2f}; lines still add up")


def test_never_discounted_and_not_floored_away():
    half = {"percent": 50, "reason": "test"}
    a = pricing.build_quote(job(), **CFG, discount=half)
    b = pricing.build_quote(job(), **CFG, discount=half, additions=[{"label": "Airbrush", "amount": 150}])
    assert b.total == a.total + 150
    # the setup fee alone clears today's minimum, so raise it for this check
    high_min = {**CFG, "pricing": {**config["pricing"], "minimum_price": 500}}
    tiny = job(grams=2, hours=0.1)
    m = pricing.build_quote(tiny, **high_min)
    assert m.breakdown["rates"]["minimum_applied"] and m.total == 500
    m2 = pricing.build_quote(tiny, **high_min, additions=[{"label": "Keyring", "amount": 5}])
    assert m2.total == m.total + 5            # on top of the minimum, not hidden inside it
    print(f"  untouched       50% discount and the {m.total:.0f} minimum leave additions whole")


def test_bad_additions_are_refused_clearly():
    bad = [
        ([{"preset": "nope"}], "Unknown special addition"),
        ([{"preset": "airbrush"}], "Enter an amount"),          # preset has no price set
        ([{"label": "Glue", "amount": 0}], "more than 0"),
        ([{"label": "Glue", "amount": 25000}], "at most"),
        ([{"amount": 10}], "needs a name"),
        ("not json", "JSON list"),
    ]
    for raw, msg in bad:
        try:
            pricing.resolve_additions(raw, config["pricing"])
        except pricing.QuoteInputError as exc:
            assert msg in str(exc), (raw, str(exc))
        else:
            raise AssertionError(f"accepted {raw!r}")
    r = c.post("/api/quote", data={"additions": json.dumps([{"label": "x", "amount": -1}])},
               files=[("files", ("a.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))])
    assert r.status_code == 422 and "more than 0" in r.json()["detail"]
    print(f"  refused         {len(bad)} bad inputs, each with a message the engineer can act on")


def test_on_receipts_in_both_languages():
    r = c.post("/api/quote", data={"additions": json.dumps([
        {"preset": "model_repair", "amount": 20, "reason": "Two walls were too thin"},
        {"label": "Heat-set inserts ×6", "amount": 30}])},
        files=[("files", ("a.gcode", (FX / "orca_petg_single.gcode").read_bytes()))])
    assert r.status_code == 200, r.text
    qid = r.json()["quote_id"]
    for lang, name in (("en", "Model repair"), ("th", "ซ่อมไฟล์โมเดล")):
        v = c.get(f"/api/receipt/{qid}?lang={lang}").json()
        assert abs(sum(l["amount"] for l in v["lines"]) - v["total"]) < 0.005, lang
        extra = [l for l in v["lines"] if l["key"].startswith("addition_")]
        assert [l["label"] for l in extra] == [name, "Heat-set inserts ×6"]
        assert extra[0]["detail"] == "Two walls were too thin"
        assert len(v["method"]) and ("no discount" in v["method"][-1] or "ส่วนลด" in v["method"][-1])
    html = c.get(f"/receipt/{qid}?lang=th").text
    assert "ซ่อมไฟล์โมเดล" in html
    print("  receipts        named lines with the engineer's reason, EN and TH, still add up")


def test_change_on_saved_order_until_paid():
    r = c.post("/api/quote/manual", json={"filaments": [{"type": "PLA", "grams": 50}], "hours": 2,
                                          "additions": [{"label": "Sanding", "amount": 40}]})
    assert r.status_code == 200, r.text
    qid, first = r.json()["quote_id"], r.json()["total"]
    o = c.put(f"/api/orders/{qid}/additions", json={"additions": [
        {"label": "Sanding", "amount": 40}, {"preset": "model_repair", "amount": 25}]}).json()
    assert o["total"] == first + 25
    assert [a["label"] for a in o["quote"]["special_additions"]] == ["Sanding", "Model repair"]
    # replacing is not cumulative
    o = c.put(f"/api/orders/{qid}/additions", json={"additions": []}).json()
    assert o["total"] == first - 40 and o["quote"]["special_additions"] == []
    assert any(e["action"] == "special additions" for e in o["events"])
    v = c.get(f"/api/receipt/{qid}").json()
    assert abs(sum(l["amount"] for l in v["lines"]) - v["total"]) < 0.005
    c.post(f"/api/orders/{qid}/payment", data={"status": "paid", "method": "Cash"})
    r = c.put(f"/api/orders/{qid}/additions", json={"additions": [{"label": "x", "amount": 5}]})
    assert r.status_code == 409 and "paid" in r.json()["detail"]
    assert c.put("/api/orders/999999/additions", json={"additions": []}).status_code == 404
    print(f"  saved order     +25 then removed all: total {first:.2f} → {first + 25:.2f} → "
          f"{first - 40:.2f}; refused once paid")


def test_presets_offered_to_the_page():
    presets = {p["id"]: p for p in c.get("/api/options").json()["special_additions"]}
    assert {"model_repair", "sanding", "acetone", "airbrush", "inserts"} <= set(presets)
    assert presets["acetone"]["label_th"]
    print(f"  presets         {len(presets)} offered on the quote page")


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
