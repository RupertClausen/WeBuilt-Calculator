"""Database migrations: a v1 orders.db upgrades in place, every step runs
once, and a database newer than the app is refused rather than touched."""
import json, os, pathlib, sqlite3, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-migrate-"))
os.environ["WEBUILT_DB"] = str(TMP / "app.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from webuilt import migrations


def v1_database(path: pathlib.Path) -> sqlite3.Connection:
    """What 1.2 left behind: the v1 tables, user_version 0, orders in every
    v1 status."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        migrations.step_001_v1_schema(conn)
    assert migrations.version(conn) == 0
    with conn:
        for i, st in enumerate(["quoted", "queued", "printing", "done", "delivered", "cancelled"]):
            conn.execute(
                "INSERT INTO quotes (created_at, filename, total, currency, detail_json,"
                " receipt_no, status) VALUES (?, ?, ?, 'THB', ?, ?, ?)",
                ("2026-10-21T07:00:00+00:00", f"f{i}.gcode", 100 + i,
                 json.dumps({"lines": [], "breakdown": {}}), f"WB-2610-0{10 + i}", st))
    return conn


def test_v1_database_upgrades_in_place():
    conn = v1_database(TMP / "v1.db")
    before = {r["receipt_no"]: dict(r) for r in conn.execute("SELECT * FROM quotes")}
    with conn:
        ran = migrations.run(conn)
    assert ran == [n for n, _ in migrations.STEPS] and migrations.version(conn) == migrations.LATEST
    after = {r["receipt_no"]: dict(r) for r in conn.execute("SELECT * FROM quotes")}
    assert set(after) == set(before), "no order lost"
    got = {no: r["status"] for no, r in after.items()}
    assert got == {"WB-2610-010": "awaiting_payment", "WB-2610-011": "queued",
                   "WB-2610-012": "printing", "WB-2610-013": "ready",
                   "WB-2610-014": "done", "WB-2610-015": "cancelled"}, got
    # nothing but the status moved
    for no in before:
        for k in ("total", "filename", "detail_json", "created_at"):
            assert before[no][k] == after[no][k]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"printers", "plate_jobs"} <= tables
    print(f"  v1 upgrade      6 orders kept; quoted→awaiting payment, done→ready, "
          f"delivered→done; schema {migrations.LATEST}")


def test_each_step_runs_once():
    conn = v1_database(TMP / "twice.db")
    with conn:
        migrations.run(conn)
    with conn:
        assert migrations.run(conn) == []
    # done→ready happened once; a second pass must not turn ready-from-done
    # (or anything) into something else
    assert conn.execute("SELECT status FROM quotes WHERE receipt_no = 'WB-2610-014'"
                        ).fetchone()[0] == "done"
    print("  run twice       second run does nothing; statuses don't chain")


def test_fresh_database_gets_everything():
    conn = sqlite3.connect(TMP / "fresh.db")
    with conn:
        migrations.run(conn)
    assert migrations.version(conn) == migrations.LATEST
    print("  fresh           empty file → latest schema")


def test_newer_database_is_refused():
    conn = sqlite3.connect(TMP / "newer.db")
    conn.execute(f"PRAGMA user_version = {migrations.LATEST + 1}")
    try:
        migrations.run(conn)
    except RuntimeError as exc:
        assert "newer" in str(exc)
    else:
        raise AssertionError("an app older than its database must not run")
    print("  rolled back     a database from a newer app is refused, not modified")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
