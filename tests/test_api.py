"""API surface: upload mode and manual mode must agree."""
import pathlib, sys, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"

# Keep the test run out of the real order log: app reads WEBUILT_DB at import.
os.environ["WEBUILT_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "orders.db")

from fastapi.testclient import TestClient
import app as appmod

client = TestClient(appmod.app)


def test_options_exposes_everything_the_ui_needs():
    d = client.get("/api/options").json()
    for key in ("printers", "rush_tiers", "delivery_options",
                "client_profiles", "materials", "labour_rate_per_hour"):
        assert d.get(key), f"missing {key}"
    assert {p["id"] for p in d["client_profiles"]} >= {"engineering", "architecture"}
    print(f"  options         {len(d['printers'])} printers, "
          f"{len(d['client_profiles'])} profiles, {len(d['materials'])} materials")


def test_manual_matches_upload_for_the_same_job():
    """Manual entry of the same numbers must produce the same price as
    uploading the sliced file."""
    up = client.post(
        "/api/quote",
        files=[("files", ("orca_petg_single.gcode",
                          (FX / "orca_petg_single.gcode").read_bytes()))],
        data={"printer": "normal", "client_profile": "engineering", "save": "false"},
    ).json()

    man = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PETG", "grams": 222.86}],
        "hours": 9, "minutes": 49,
        "printer": "normal", "client_profile": "engineering", "save": False,
    }).json()

    assert up["total"] == man["total"], (up["total"], man["total"])
    print(f"  manual == upload  both B{man['total']:.2f}")


def test_manual_rejects_zero_time():
    r = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PLA", "grams": 10}],
        "hours": 0, "minutes": 0, "save": False})
    assert r.status_code == 422
    print(f"  zero time       rejected {r.status_code}")


def test_manual_multi_filament():
    r = client.post("/api/quote/manual", json={
        "filaments": [{"type": "PLA", "grams": 48.2},
                      {"type": "PETG", "grams": 12.75}],
        "hours": 3, "minutes": 13, "save": False}).json()
    assert len(r["breakdown"]["materials"]) == 2
    print(f"  multi filament  2 materials -> B{r['total']:.2f}")


def test_manual_unknown_material_warns():
    r = client.post("/api/quote/manual", json={
        "filaments": [{"type": "UNOBTAINIUM", "grams": 50}],
        "hours": 2, "save": False}).json()
    assert any("roster" in w for w in r["warnings"])
    print(f"  unknown filament warns, still prices (B{r['total']:.2f})")


def test_upload_rejects_unsliced_3mf():
    r = client.post("/api/quote", files=[
        ("files", ("unsliced_project.3mf",
                   (FX / "unsliced_project.3mf").read_bytes()))],
        data={"save": "false"})
    assert r.status_code == 422
    assert "not a sliced one" in r.json()["detail"]
    print(f"  unsliced 3mf    rejected {r.status_code} with a clear message")


def test_multiple_files_make_one_order():
    """A customer whose parts do not fit one bed sends several files."""
    names = ["orca_petg_single.gcode", "orca_tiny_pla.gcode", "bambu_ams_multi.gcode"]
    r = client.post("/api/quote", files=[
        ("files", (n, (FX / n).read_bytes())) for n in names],
        data={"printer": "normal", "client_profile": "engineering",
              "save": "false"}).json()
    o = r["breakdown"]["order"]
    assert o["files"] == 3 and o["plates"] == 3
    assert len(r["breakdown"]["plates"]) == 3
    # every plate carries its OWN risk factor
    factors = [p["risk"]["factor"] for p in r["breakdown"]["plates"]]
    assert len(set(round(f, 4) for f in factors)) == 3, factors
    print(f"  multi-file      3 files -> 3 plates, risk factors "
          f"{', '.join(f'{f:.3f}' for f in factors)} -> B{r['total']:.2f}")


def test_fee_structure_50_plus_20():
    """50 up front including the first plate, 20 for each extra."""
    def total_fees(n):
        r = client.post("/api/quote", files=[
            ("files", (f"p{i}.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))
            for i in range(n)], data={"save": "false"}).json()
        setup = next(l["amount"] for l in r["lines"] if l["key"] == "setup")
        extra = next((l["amount"] for l in r["lines"]
                      if l["key"] == "extra_plates"), 0.0)
        return setup, extra, r["breakdown"]["order"]["plates"]

    assert total_fees(1) == (50.0, 0.0, 1)
    assert total_fees(2) == (50.0, 20.0, 2)
    assert total_fees(3) == (50.0, 40.0, 3)
    print("  fee structure   1 plate B50 | 2 plates B50+20 | 3 plates B50+40")


def test_copies_multiply_plates_not_files():
    r = client.post("/api/quote", files=[
        ("files", ("a.gcode", (FX / "orca_tiny_pla.gcode").read_bytes())),
        ("files", ("b.gcode", (FX / "orca_tiny_pla.gcode").read_bytes()))],
        data={"copies": ["1", "4"], "save": "false"}).json()
    o = r["breakdown"]["order"]
    assert o["files"] == 2 and o["plates"] == 5, o
    extra = next(l["amount"] for l in r["lines"] if l["key"] == "extra_plates")
    assert extra == 4 * 20.0
    print(f"  copies          2 files (x1, x4) -> {o['plates']} plates, "
          f"B{extra:.0f} in extra-plate fees")


def test_per_plate_risk_beats_averaging():
    """An uneven split must not price the same as an even one."""
    def two_plates(h1, h2):
        return client.post("/api/quote/manual", json={
            "filaments": [{"type": "PLA", "grams": 100}],
            "hours": h1, "printer": "normal", "save": False}).json()["total"], \
               client.post("/api/quote/manual", json={
            "filaments": [{"type": "PLA", "grams": 100}],
            "hours": h2, "printer": "normal", "save": False}).json()["total"]
    short_long = two_plates(2, 20)
    even = two_plates(11, 11)
    assert sum(short_long) != sum(even)
    print(f"  convex risk     2h+20h priced apart from 11h+11h "
          f"({sum(short_long):.0f} vs {sum(even):.0f} as separate quotes)")


def test_one_bad_file_names_itself():
    r = client.post("/api/quote", files=[
        ("files", ("good.gcode", (FX / "orca_tiny_pla.gcode").read_bytes())),
        ("files", ("broken.3mf", (FX / "unsliced_project.3mf").read_bytes()))],
        data={"save": "false"})
    assert r.status_code == 422
    assert "broken.3mf" in r.json()["detail"]
    print(f"  bad file        rejected, names the offender: "
          f"{r.json()['detail'][:40]}...")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} API tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
