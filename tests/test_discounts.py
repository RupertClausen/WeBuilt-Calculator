"""Discounts: presets and one-offs, percent or baht, never below break-even,
never on delivery, shown honestly on the receipt."""
import copy, os, pathlib, sqlite3, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ["WEBUILT_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "orders.db")

import yaml
from fastapi.testclient import TestClient
import app as appmod
import pricing
import slicefile

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"
CFG = {k: yaml.safe_load((ROOT / "config" / f"{k}.yaml").read_text(encoding="utf-8"))
       for k in ("pricing", "printers", "filaments")}
client = TestClient(appmod.app)
JOB = slicefile.parse("j.gcode", (FX / "orca_petg_single.gcode").read_bytes())


def q(pricing_cfg=None, **kw):
    return pricing.build_quote(JOB, pricing=pricing_cfg or CFG["pricing"],
                               printers=CFG["printers"], filaments=CFG["filaments"],
                               printer_id="normal",
                               **({"client_profile": "engineering"} | kw))


def pre_delivery(quote):
    return quote.breakdown["raw_total"] - quote.breakdown["economics"]["delivery"]


def given(quote):
    return -quote.breakdown["economics"]["discount"]


def test_preset_percent():
    base, disc = q(), q(discount={"preset": "repeat"})
    assert abs(given(disc) - pre_delivery(base) * 0.10) < 0.01
    assert disc.total < base.total
    line = next(l for l in disc.lines if l.key == "discount")
    assert line.label == "Discount — Repeat customer" and line.amount < 0
    print(f"  preset          'repeat' 10%: B{given(disc):.2f} off, "
          f"B{base.total:.0f} -> B{disc.total:.0f}")


def test_one_off_amount_and_percent():
    a = q(discount={"amount": 50, "reason": "Friend"})
    p = q(discount={"percent": 5, "reason": "Late delivery"})
    assert abs(given(a) - 50) < 1e-9
    assert abs(given(p) - pre_delivery(q()) * 0.05) < 0.01
    assert next(l for l in a.lines if l.key == "discount").label == "Discount — Friend"
    print(f"  one-off         B50 off -> B{given(a):.2f}; 5% off -> B{given(p):.2f}")


def test_delivery_is_never_discounted():
    pick = q(discount={"percent": 10, "reason": "x"}, delivery="pickup")
    exp = q(discount={"percent": 10, "reason": "x"}, delivery="express")
    assert abs(given(pick) - given(exp)) < 0.01
    print(f"  delivery        same B{given(pick):.2f} off with pickup and express")


def test_never_below_break_even():
    big = q(discount={"percent": 90, "reason": "too generous"})
    d = big.breakdown["discount"]
    assert d["capped"] and d["applied"] < d["requested"]
    assert abs(pre_delivery(big) - d["break_even"]) < 0.01
    assert any("below what it costs" in w for w in big.warnings)
    line = next(l for l in big.lines if l.key == "discount")
    assert "capped at break-even" in line.note
    print(f"  break-even      90% asked B{d['requested']:.2f}, given B{d['applied']:.2f}; "
          f"price stops at B{d['break_even']:.2f}")


def test_discount_comes_out_of_profit():
    disc = q(discount={"preset": "club"})
    e = disc.breakdown["economics"]
    assert abs(e["profit_after_discount"] - (e["profit"] + e["discount"])) < 0.01
    assert abs(sum(l.amount for l in disc.lines) - disc.total) < 0.005
    buckets = (e["running_costs"]["total"] + e["filament_markup"] + e["machine_time"]
               + e["labour"]["total"] + e["failure_allowance"] + e["contingency"]
               + e["rush_premium"] + e["rate_adjustment"] + e["profit"] + e["discount"]
               + e["delivery"] + e["rounding"])
    assert abs(buckets - e["total"]) < 0.05
    print(f"  accounting      profit B{e['profit']:.2f} - discount B{-e['discount']:.2f} "
          f"= kept B{e['profit_after_discount']:.2f}; lines and buckets add up")


def test_bad_requests_are_rejected():
    cases = [
        ({"preset": "repeat", "percent": 5}, "not both"),
        ({"preset": "nope"}, "Unknown discount"),
        ({"percent": 100}, "below 100"),
        ({"percent": -5}, "above 0"),
        ({"amount": 0.0001, "percent": 3}, "not both"),
        ({"amount": -20}, "above 0"),
    ]
    for req, msg in cases:
        try:
            q(discount=req)
        except pricing.QuoteInputError as e:
            assert msg in str(e), (req, e)
        else:
            raise AssertionError(f"accepted {req}")
    cfg = copy.deepcopy(CFG["pricing"]); cfg["discounts"]["allow_one_off"] = False
    try:
        q(cfg, discount={"percent": 5})
    except pricing.QuoteInputError as e:
        assert "preset" in str(e)
    else:
        raise AssertionError("one-off allowed")
    assert q(cfg, discount={"preset": "club"}).breakdown["discount"]["applied"] > 0
    print(f"  rejects         {len(cases)} bad requests; one-offs off -> presets still work")


def test_no_discount_means_no_change():
    assert q(discount=None).total == q().total == q(discount={}).total
    assert q(discount={"preset": None, "percent": None, "amount": None}).breakdown["discount"] is None
    print("  none            empty discount changes nothing")


def test_api_order_log_and_status_codes():
    r = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PETG", "grams": 100}], "hours": 2,
        "client_profile": "engineering", "discount": "repeat"}).json()
    with sqlite3.connect(appmod.DB_PATH) as c:
        disc, label, profit = c.execute(
            "SELECT discount, discount_label, profit FROM quotes WHERE id = ?",
            (r["quote_id"],)).fetchone()
    e = r["breakdown"]["economics"]
    assert disc == -e["discount"] and label == "Repeat customer"
    assert profit == e["profit_after_discount"]
    bad = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PLA", "grams": 5}], "hours": 1, "discount": "nope"})
    assert bad.status_code == 422 and "Unknown discount" in bad.json()["detail"]
    up = client.post("/api/quote", files=[("files", ("j.gcode",
                     (FX / "orca_petg_single.gcode").read_bytes()))],
                     data={"discount_amount": "25", "discount_reason": "Friend",
                           "save": "false"}).json()
    assert -up["breakdown"]["economics"]["discount"] == 25
    opts = client.get("/api/options").json()["discounts"]
    assert {p["id"] for p in opts["presets"]} >= {"repeat", "club"}
    print("  api             manual + upload accept discounts; log keeps amount, label and "
          "profit kept; bad preset -> 422")


def test_receipt_line_both_languages():
    def receipt(body, lang):
        r = client.post("/api/quote/manual", json={
            "filaments": [{"type": "PETG", "grams": 100}], "hours": 2,
            "client_profile": "engineering", **body}).json()
        v = client.get(f"/api/receipt/{r['quote_id']}?lang={lang}").json()
        assert abs(sum(l["amount"] for l in v["lines"]) - v["total"]) < 0.005
        return next(l for l in v["lines"] if l["key"] == "discount")

    l = receipt({"discount": "repeat"}, "en")
    assert l["label"] == "Discount — Repeat customer" and l["detail"].startswith("10% off")
    l = receipt({"discount": "repeat"}, "th")
    assert l["label"] == "ส่วนลด — ลูกค้าประจำ" and l["detail"].startswith("ลด 10%")
    # a small job (B35.81 above break-even), so keep this one-off under the cap
    l = receipt({"discount_amount": 20, "discount_reason": "เพื่อนชมรม"}, "en")
    assert l["label"] == "Discount — เพื่อนชมรม" and l["detail"] == "฿20.00 off"
    l = receipt({"discount_percent": 90, "discount_reason": "Big"}, "en")
    assert "%" not in l["detail"] and l["detail"].endswith(" off")      # capped -> baht only
    l = receipt({"discount_percent": 5}, "th")
    assert l["label"] == "ส่วนลด"
    print("  receipt         preset in en/th, one-off reason as typed, capped shows baht "
          "only, unnamed one-off")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} discount tests\n")
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
