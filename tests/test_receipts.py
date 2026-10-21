"""Receipts: customer-safe, adds up, honest about what it claims."""
import copy, json, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"

import os

# Never touch the real order log: app reads WEBUILT_DB at import.
os.environ["WEBUILT_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "orders.db")

from fastapi.testclient import TestClient
import app as appmod
import receipt as receipt_doc

client = TestClient(appmod.app)

# Internal figures that must never reach a customer. The receipt DOES show
# the profit margin on purpose, as its own labelled line, so "margin" and
# "กำไร" are allowed; what stays hidden is the internal split and the
# supplier-cost-based numbers behind it.
FORBIDDEN = ("cost_per_kg", "estimated_direct_cost", "estimated_margin",
             "margin_pct", "supplier", "economics", "running_costs",
             "filament_markup", "architecture", "engineering",
             "general / staff", "internal / webuilt")


def save_upload(names, **data):
    files = [("files", (n, (FX / n).read_bytes())) for n in names]
    r = client.post("/api/quote", files=files, data={"save": "true", **data})
    assert r.status_code == 200, r.text
    return r.json()


def view(qid):
    return client.get(f"/api/receipt/{qid}").json()


def visible_text(html):
    import re
    html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    html = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    html = re.sub(r'style="[^"]*"', "", html)
    return html.lower()


FORBIDDEN_TH = ("ต้นทุน", "สถาปัตย", "วิศวกรรม")   # cost, faculties


def test_no_internal_numbers_reach_the_customer():
    """Supplier cost, margin and the client-profile name stay internal,
    for every profile, in every format, in every language."""
    for prof in ("engineering", "architecture", "general"):
        q = save_upload(["orca_petg_single.gcode", "bambu_ams_multi.gcode"],
                        client_profile=prof, postproc_minutes="15")
        qid = q["quote_id"]
        for lang in ("en", "th"):
            blobs = {
                "json": json.dumps(client.get(f"/api/receipt/{qid}?lang={lang}").json(),
                                   ensure_ascii=False).lower(),
                "html": visible_text(client.get(f"/receipt/{qid}?lang={lang}").text),
                "text": client.get(f"/receipt/{qid}.txt?lang={lang}").text.lower(),
            }
            for fmt, blob in blobs.items():
                for bad in FORBIDDEN + FORBIDDEN_TH:
                    assert bad not in blob, f"{prof}/{lang}/{fmt} leaked '{bad}'"
    print(f"  no leaks        3 profiles x 2 languages x 3 formats, "
          f"{len(FORBIDDEN) + len(FORBIDDEN_TH)} terms checked")


def test_lines_add_up_to_the_total_exactly():
    cases = [
        dict(names=["orca_tiny_pla.gcode"]),
        dict(names=["orca_petg_single.gcode", "orca_tiny_pla.gcode"],
             copies=["1", "4"], rush="same_day", delivery="express"),
        dict(names=["bambu_petg.gcode.3mf", "bambu_ams_multi.gcode"],
             client_profile="architecture", postproc_minutes="30", printer="large"),
    ]
    for c in cases:
        names = c.pop("names")
        copies = c.pop("copies", None)
        data = {**c}
        files = [("files", (n, (FX / n).read_bytes())) for n in names]
        if copies:
            data["copies"] = copies
        q = client.post("/api/quote", files=files, data={"save": "true", **data}).json()
        for lang in ("en", "th"):
            v = client.get(f"/api/receipt/{q['quote_id']}?lang={lang}").json()
            s = round(sum(l["amount"] for l in v["lines"]), 2)
            assert s == v["total"] == q["total"], (lang, s, v["total"], q["total"])
            assert len(v["lines"]) == len(view(q["quote_id"])["lines"])
    print(f"  adds up         {len(cases)} orders x 2 languages, same lines, exact totals")


def test_minimum_floor_is_labelled_as_minimum():
    import yaml
    cfg = copy.deepcopy(appmod.config["pricing"])
    q = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PLA", "grams": 1}], "minutes": 3,
        "client_profile": "engineering", "save": True}).json()
    v = view(q["quote_id"])
    labels = [l["label"] for l in v["lines"]]
    if q["breakdown"]["rates"]["minimum_applied"]:
        assert "Minimum order" in labels
    else:
        assert "Minimum order" not in labels
    print(f"  floor label     minimum_applied={q['breakdown']['rates']['minimum_applied']}, "
          f"labelled accordingly")


def test_title_follows_payment():
    q = save_upload(["orca_tiny_pla.gcode"])
    qid = q["quote_id"]
    assert view(qid)["title"] == "Quotation"
    assert "Not yet paid" in client.get(f"/receipt/{qid}.txt").text
    r = client.post(f"/api/orders/{qid}/payment",
                    data={"status": "paid", "method": "PromptPay"})
    assert r.status_code == 200
    v = view(qid)
    assert v["title"] == "Receipt" and v["paid"] and v["payment_method"] == "PromptPay"
    assert "PAID" in client.get(f"/receipt/{qid}.txt").text
    bad = client.post(f"/api/orders/{qid}/payment",
                      data={"status": "paid", "method": "Bitcoin"})
    assert bad.status_code == 400
    print("  title           Quotation until paid, Receipt after; bad method rejected")


def test_receipt_numbers_continue_after_the_sheet():
    from datetime import datetime
    yymm = datetime.now(receipt_doc.zone("Asia/Bangkok")).strftime("%y%m")
    floor = int((appmod.config["receipt"].get("next_number_floor") or {}).get(yymm, 1))
    a = save_upload(["orca_tiny_pla.gcode"])["receipt_no"]
    b = save_upload(["orca_tiny_pla.gcode"])["receipt_no"]
    na, nb = int(a.rsplit("-", 1)[1]), int(b.rsplit("-", 1)[1])
    assert a.startswith(f"WB-{yymm}-") and na >= floor and nb == na + 1, (a, b)
    print(f"  numbering       {a} then {b} (floor for {yymm} is {floor:03d})")


def test_duplicate_receipt_number_rejected():
    save_upload(["orca_tiny_pla.gcode"], receipt_no="WB-TEST-001")
    files = [("files", ("x.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))]
    r = client.post("/api/quote", files=files,
                    data={"save": "true", "receipt_no": "WB-TEST-001"})
    assert r.status_code == 409
    print("  duplicates      reusing a receipt number -> 409")


def test_method_text_never_overclaims():
    """The document must not claim measured failure data, or promise free
    reprints, unless that is actually true."""
    q = save_upload(["orca_tiny_pla.gcode"])
    row, quote = appmod._load_order(q["quote_id"])
    rc = copy.deepcopy(appmod.config["receipt"])

    text = " ".join(receipt_doc.build_view(row, quote, rc)["method"])
    assert "still collecting records" in text
    assert "own printers' failure records" not in text

    for pl in quote["breakdown"]["plates"]:
        pl["printer_measured"] = True
    text = " ".join(receipt_doc.build_view(row, quote, rc)["method"])
    assert "own printers' failure records" in text

    rc["free_reprints"] = False
    v = receipt_doc.build_view(row, quote, rc)
    text = " ".join(v["method"])
    risk_line = next(l for l in v["lines"] if l["key"] == "risk")
    assert "costs you nothing" not in text and "free reprints" not in risk_line["detail"]
    print("  no overclaim    measured / free-reprint claims only when true")


def test_txt_route_not_shadowed():
    q = save_upload(["orca_tiny_pla.gcode"])
    r = client.get(f"/receipt/{q['quote_id']}.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert client.get("/receipt/999999").status_code == 404
    print("  routes          /receipt/N.txt serves text; unknown id 404s")


# ---------------------------------------------------------------------------
# languages
# ---------------------------------------------------------------------------

def test_locales_have_the_same_keys_and_placeholders():
    """A key missing from th.yaml would silently show English; a placeholder
    typo would crash a receipt. Catch both here."""
    import string, yaml
    loc = {l: yaml.safe_load((ROOT / "locales" / f"{l}.yaml").read_text(encoding="utf-8"))
           for l in ("en", "th")}
    assert set(loc["en"]) == set(loc["th"]), set(loc["en"]) ^ set(loc["th"])
    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}
    for k, en in loc["en"].items():
        if isinstance(en, str):
            assert fields(en) == fields(loc["th"][k]), k
    assert set(loc["en"]["payment_methods"]) == set(loc["th"]["payment_methods"])
    assert set(loc["en"]["payment_methods"]) >= set(
        appmod.config["receipt"]["payment_methods"])
    print(f"  locale parity   {len(loc['en'])} keys, placeholders match, "
          "every payment method translated")


def test_thai_receipt():
    q = save_upload(["orca_petg_single.gcode"], rush="next_day", delivery="university")
    qid = q["quote_id"]
    v = client.get(f"/api/receipt/{qid}?lang=th").json()
    assert v["lang"] == "th" and v["title"] == "ใบเสนอราคา"
    assert "2569" in v["date"] and "ต.ค." in v["date"], v["date"]     # Buddhist Era
    assert v["order"]["rush"] == "ภายในวันถัดไป"
    assert v["order"]["delivery"] == "ส่งให้ที่มหาลัย"
    assert v["terms"][0].startswith("ค่าเผื่องานพิมพ์เสีย")
    client.post(f"/api/orders/{qid}/payment", data={"status": "paid", "method": "Cash"})
    v = client.get(f"/api/receipt/{qid}?lang=th").json()
    assert v["title"] == "ใบเสร็จรับเงิน" and v["payment_method"] == "เงินสด"
    html = client.get(f"/receipt/{qid}?lang=th").text
    assert '<html lang="th">' in html and ">English</a>" in html
    print(f"  thai            {v['date']} · {v['order']['rush']} · {v['payment_method']}")


def test_language_fallbacks():
    q = save_upload(["orca_tiny_pla.gcode"])
    qid = q["quote_id"]
    default = appmod.config["receipt"].get("default_language", "en")
    assert client.get(f"/api/receipt/{qid}").json()["lang"] == default
    assert client.get(f"/api/receipt/{qid}?lang=fr").json()["lang"] == default
    # a language with no locale file reads English rather than raw keys
    assert receipt_doc.Locale("xx")("title_quotation") == "Quotation"
    print(f"  fallbacks       no lang / unknown lang -> '{default}', missing keys -> English")


def test_old_quotes_without_label_keys_still_render():
    """Quotes saved before labels were stored by key keep their saved label."""
    q = save_upload(["orca_tiny_pla.gcode"], delivery="university")
    row, quote = appmod._load_order(q["quote_id"])
    for k in ("rush_key", "delivery_key"):
        quote["breakdown"]["rates"].pop(k)
    v = receipt_doc.build_view(row, quote, appmod.config["receipt"], "th",
                               pricing_cfg=appmod.config["pricing"],
                               printers_cfg=appmod.config["printers"])
    assert v["order"]["delivery"] == quote["breakdown"]["rates"]["delivery_label"]
    print(f"  old quotes      no stored key -> saved label '{v['order']['delivery']}'")


def test_thai_method_text_never_overclaims():
    q = save_upload(["orca_tiny_pla.gcode"])
    row, quote = appmod._load_order(q["quote_id"])
    rc = copy.deepcopy(appmod.config["receipt"])
    text = " ".join(receipt_doc.build_view(row, quote, rc, "th")["method"])
    assert "กำลังเก็บสถิติ" in text and "สถิติงานเสียของเครื่องพิมพ์ของเราเอง" not in text
    rc["free_reprints"] = False
    text = " ".join(receipt_doc.build_view(row, quote, rc, "th")["method"])
    assert "ไม่ต้องจ่ายเพิ่ม" not in text
    print("  thai claims     same conditional wording as English")


def test_time_zone_never_crashes():
    """Windows has no built-in time zone database. With tzdata installed this
    never triggers, but a broken install must still show Bangkok time."""
    from datetime import timedelta
    from zoneinfo import ZoneInfoNotFoundError
    real = receipt_doc.ZoneInfo
    def missing(name):
        raise ZoneInfoNotFoundError(name)
    receipt_doc.ZoneInfo = missing
    try:
        tz = receipt_doc.zone("Asia/Bangkok")
        assert tz.utcoffset(None) == timedelta(hours=7)
        assert receipt_doc.zone("Mars/Olympus").utcoffset(None) == timedelta(0)
    finally:
        receipt_doc.ZoneInfo = real
    print("  time zone       no database -> Bangkok still UTC+7; unknown zone -> UTC")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} receipt tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
