"""Profit margin: a share of the price, excluding delivery, reported apart
from labour and contingency, and shown honestly on the receipt."""
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


def test_default_is_fifteen_percent_of_price():
    assert CFG["pricing"]["profit_margin"] == 0.15
    quote = q()
    e = quote.breakdown["economics"]
    assert abs(e["profit"] / pre_delivery(quote) - 0.15) < 1e-3
    print(f"  15% of price    profit B{e['profit']:.2f} of B{pre_delivery(quote):.2f}")


def test_margin_not_markup():
    """15% margin multiplies by 1/0.85 = 1.176, not 1.15."""
    zero = copy.deepcopy(CFG["pricing"]); zero["profit_margin"] = 0.0
    without, with_ = pre_delivery(q(zero)), pre_delivery(q())
    assert abs(with_ / without - 1 / 0.85) < 1e-3, with_ / without
    print(f"  margin          x{with_ / without:.4f} (a markup would be x1.15)")


def test_delivery_is_excluded():
    a = q(delivery="pickup").breakdown["economics"]["profit"]
    b = q(delivery="express").breakdown["economics"]["profit"]
    assert a == b
    print(f"  delivery        profit B{a:.2f} with pickup and with express")


def test_profile_override():
    assert q(client_profile="internal").breakdown["economics"]["profit"] == 0.0
    cfg = copy.deepcopy(CFG["pricing"])
    cfg["client_profiles"]["architecture"]["profit_margin"] = 0.25
    quote = q(cfg, client_profile="architecture")
    assert abs(quote.breakdown["economics"]["profit"] / pre_delivery(quote) - 0.25) < 1e-3
    print("  override        internal 0%, a profile set to 25% gets 25%")


def test_invalid_margin_rejected():
    for bad in (-0.1, 0.9, 1.0):
        cfg = copy.deepcopy(CFG["pricing"]); cfg["profit_margin"] = bad
        try:
            q(cfg)
        except ValueError as e:
            assert "profit_margin" in str(e)
        else:
            raise AssertionError(f"{bad} accepted")
    print("  validation      -0.1, 0.9 and 1.0 rejected")


def test_lines_and_buckets_add_up():
    for kw in ({}, {"rush": "same_day", "delivery": "express"},
               {"client_profile": "architecture", "postproc_minutes": 30}):
        quote = q(**kw)
        assert abs(sum(l.amount for l in quote.lines) - quote.total) < 0.005
        e = quote.breakdown["economics"]
        buckets = (e["running_costs"]["total"] + e["filament_markup"] + e["machine_time"]
                   + e["labour"]["total"] + e["failure_allowance"] + e["contingency"]
                   + e["rush_premium"] + e["rate_adjustment"] + e["profit"]
                   + e["delivery"] + e["rounding"])
        assert abs(buckets - e["total"]) < 0.05, (buckets, e["total"])
    print("  reconciles      lines = total; costs + labour + contingency + profit + ... = total")


def test_profit_reported_apart_from_labour_and_contingency():
    e = q(client_profile="architecture", postproc_minutes=20).breakdown["economics"]
    assert e["labour"]["total"] > 0 and e["contingency"] > 0 and e["profit"] > 0
    assert e["labour"]["file_prep"] == 50.0 and e["labour"]["setup"] == 50.0
    assert len({e["labour"]["total"], e["contingency"], e["profit"]}) == 3
    print(f"  split           labour B{e['labour']['total']:.2f} · contingency "
          f"B{e['contingency']:.2f} · profit B{e['profit']:.2f}")


def test_order_log_records_the_split():
    r = client.post("/api/quote", files=[("files", ("j.gcode",
                    (FX / "orca_petg_single.gcode").read_bytes()))],
                    data={"client_profile": "engineering"}).json()
    with sqlite3.connect(appmod.DB_PATH) as c:
        labour, contingency, profit = c.execute(
            "SELECT labour, contingency, profit FROM quotes WHERE id = ?",
            (r["quote_id"],)).fetchone()
    e = r["breakdown"]["economics"]
    assert (labour, contingency, profit) == (e["labour"]["total"], e["contingency"], e["profit"])
    listed = client.get("/api/orders").json()[0]
    assert listed["profit"] == profit
    print(f"  order log       labour/contingency/profit columns stored and listed")


def test_receipt_line_in_both_languages():
    r = client.post("/api/quote", files=[("files", ("j.gcode",
                    (FX / "orca_petg_single.gcode").read_bytes()))],
                    data={"client_profile": "engineering"}).json()
    qid = r["quote_id"]
    for lang, label, detail in (("en", "Service margin", "Our profit: 15%"),
                                ("th", "ค่าบริการ", "กำไรของร้าน 15%")):
        v = client.get(f"/api/receipt/{qid}?lang={lang}").json()
        line = next(l for l in v["lines"] if l["key"] == "profit")
        assert line["label"] == label and line["detail"].startswith(detail)
        assert abs(line["amount"] - r["breakdown"]["economics"]["profit"]) < 0.005
        # the profit sentence specifically, not just any "15%" (contingency is 15% too)
        assert any(label.lower() in m.lower() and "15%" in m for m in v["method"]), v["method"]
    print("  receipt         'Service margin' / 'ค่าบริการ' line + method sentence")


def test_no_profit_line_when_margin_is_zero():
    r = client.post("/api/quote", files=[("files", ("j.gcode",
                    (FX / "orca_petg_single.gcode").read_bytes()))],
                    data={"client_profile": "internal"}).json()
    v = client.get(f"/api/receipt/{r['quote_id']}").json()
    assert not any(l["key"] == "profit" for l in v["lines"])
    assert not any("service margin" in m.lower() for m in v["method"])
    print("  zero margin     no line, no method sentence")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} profit tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
