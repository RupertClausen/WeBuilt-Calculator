"""Receipts: customer-safe, adds up, honest about what it claims."""
import copy, json, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"

from fastapi.testclient import TestClient
import app as appmod
import receipt as receipt_doc

# Never touch the real order log.
appmod.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "orders.db"
appmod.init_db()
client = TestClient(appmod.app)

FORBIDDEN = ("cost_per_kg", "estimated_direct_cost", "margin", "supplier",
             "architecture", "engineering", "general / staff", "internal / webuilt")


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


def test_no_internal_numbers_reach_the_customer():
    """Supplier cost, margin and the client-profile name stay internal,
    for every profile, in every format."""
    for prof in ("engineering", "architecture", "general"):
        q = save_upload(["orca_petg_single.gcode", "bambu_ams_multi.gcode"],
                        client_profile=prof, postproc_minutes="15")
        qid = q["quote_id"]
        blobs = {
            "json": json.dumps(view(qid), ensure_ascii=False).lower(),
            "html": visible_text(client.get(f"/receipt/{qid}").text),
            "text": client.get(f"/receipt/{qid}.txt").text.lower(),
        }
        for fmt, blob in blobs.items():
            for bad in FORBIDDEN:
                assert bad not in blob, f"{prof}/{fmt} leaked '{bad}'"
    print(f"  no leaks        3 profiles x 3 formats, {len(FORBIDDEN)} terms checked")


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
        v = view(q["quote_id"])
        s = round(sum(l["amount"] for l in v["lines"]), 2)
        assert s == v["total"] == q["total"], (s, v["total"], q["total"])
    print(f"  adds up         {len(cases)} orders, every receipt sums to its total")


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
    from zoneinfo import ZoneInfo
    yymm = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%y%m")
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
