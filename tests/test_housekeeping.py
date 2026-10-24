"""Daily chores: backups, pruning, file retention, expired sessions."""
import os, pathlib, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-hk-"))
(TMP / "local").mkdir()
(TMP / "local" / "server.yaml").write_text("backups:\n  hour: 3\n  keep: 3\n", encoding="utf-8")
os.environ["WEBUILT_DB"] = str(TMP / "orders.db")
os.environ["WEBUILT_LOCAL_DIR"] = str(TMP / "local")

from fastapi.testclient import TestClient
import app as appmod
import auth
import core
import housekeeping
import receipt as receipt_doc

BKK = receipt_doc.zone("Asia/Bangkok")
FX = ROOT / "tests" / "fixtures"
c = TestClient(appmod.app)
c.post("/api/quote/manual", json={"filaments": [{"type": "PLA", "grams": 10}], "hours": 1})


def at(day, hour):
    return datetime(2026, 11, day, hour, 0, tzinfo=BKK)


def test_nothing_before_the_hour_then_once_a_day():
    assert "backup" not in housekeeping.run_once(at(1, 2))          # 02:00, before 03:00
    did = housekeeping.run_once(at(1, 4))
    assert did["backup"] == "orders-2026-11-01.db"
    assert "backup" not in housekeeping.run_once(at(1, 23))          # already done today
    assert housekeeping.run_once(at(2, 3))["backup"] == "orders-2026-11-02.db"
    print("  schedule        nothing at 02:00, backup at 04:00, not again that day, next day at 03:00")


def test_backup_is_a_working_copy():
    b = core.BACKUP_DIR / "orders-2026-11-01.db"
    with sqlite3.connect(b) as copy, sqlite3.connect(core.DB_PATH) as live:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] >= 1
        tables = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quotes", "users", "order_files", "order_events"} <= tables
    assert not list(core.BACKUP_DIR.glob("*.part"))
    print("  backup          integrity ok, has the orders and all tables, no half-written file left")


def test_pruning_keeps_the_newest():
    for d in (3, 4, 5):
        housekeeping.run_once(at(d, 5))
    names = sorted(p.name for p in core.BACKUP_DIR.glob("orders-*.db"))
    assert names == ["orders-2026-11-03.db", "orders-2026-11-04.db", "orders-2026-11-05.db"], names
    print(f"  pruning         keep 3 -> {names}")


def test_cleanup_runs_daily_and_clears_sessions():
    auth.create_user("old", "old-password-1")
    token, _ = auth.login("old", "old-password-1")
    with core.db() as conn:
        conn.execute("UPDATE sessions SET expires_at = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
    did = housekeeping.run_once(at(6, 5))
    assert did["sessions_removed"] >= 1 and "files_removed" in did
    assert "files_removed" not in housekeeping.run_once(at(6, 6))     # once a day
    st = core.get_state("housekeeping")
    assert st["last_cleanup_day"] == "2026-11-06" and st["last_backup"] == "orders-2026-11-06.db"
    print(f"  cleanup         expired sessions removed ({did['sessions_removed']}), once a day, state recorded")


def test_backups_can_be_switched_off():
    (TMP / "local" / "server.yaml").write_text("backups:\n  enabled: false\n", encoding="utf-8")
    os.utime(TMP / "local" / "server.yaml", None)
    st = os.stat(TMP / "local" / "server.yaml")
    os.utime(TMP / "local" / "server.yaml", (st.st_atime, st.st_mtime + 5))
    assert "backup" not in housekeeping.run_once(at(7, 5))
    print("  switch off      backups.enabled: false -> no backup")


if __name__ == "__main__":
    fns = [test_nothing_before_the_hour_then_once_a_day, test_backup_is_a_working_copy,
           test_pruning_keeps_the_newest, test_cleanup_runs_daily_and_clears_sessions,
           test_backups_can_be_switched_off]                 # order matters here
    print(f"\nRunning {len(fns)} housekeeping tests\n")
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
