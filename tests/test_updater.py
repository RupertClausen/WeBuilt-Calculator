"""Releases and updates: build, install, update, roll back, and refuse
anything unsafe. Each test builds real release zips from a copy of the app."""
import hashlib, http.server, io, json, os, pathlib, shutil, sys, tempfile, threading, zipfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
os.environ["WEBUILT_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "orders.db")

import build_release
import updater
from updater import UpdateError

TMP = pathlib.Path(tempfile.mkdtemp(prefix="webuilt-upd-"))


def source_files(root: pathlib.Path) -> list[str]:
    files = [f for f in build_release.INCLUDE_FILES if (root / f).exists()]
    for d in build_release.INCLUDE_DIRS:
        files += [str(p.relative_to(root)).replace(os.sep, "/")
                  for p in (root / d).rglob("*") if p.is_file()]
    return sorted(files)


def make_release(version: str, mutate=None, name=None) -> pathlib.Path:
    """Copy the app, set its version, apply `mutate`, build a release zip."""
    src = TMP / f"src-{name or version}"
    if src.exists():
        shutil.rmtree(src)
    for rel in source_files(ROOT):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, src / rel)
    rel_json = json.loads((src / "release.json").read_text(encoding="utf-8"))
    rel_json["version"] = version
    (src / "release.json").write_text(json.dumps(rel_json), encoding="utf-8")
    if mutate:
        mutate(src)
    return build_release.build(TMP / f"dist-{name or version}", "owner/repo",
                               source_files(src), root=src)


def install(zip_path: pathlib.Path, name: str) -> pathlib.Path:
    """What a teammate does: extract the zip somewhere."""
    dest = TMP / f"install-{name}"
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    return dest / "webuilt-quote"


def digest(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree(base: pathlib.Path) -> dict:
    return {str(p.relative_to(base)): digest(p) for p in base.rglob("*")
            if p.is_file() and p.relative_to(base).parts[0] not in ("backups",)}


def quiet(*a, **k):
    pass


V1 = make_release("1.0.0")


def v2_changes(src):
    """A typical update: a price changes, a file is added, one is removed."""
    f = src / "config" / "filaments.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace(
        "flat_standard_price_per_kg: 540", "flat_standard_price_per_kg: 560"), encoding="utf-8")
    (src / "docs" / "NEW-IN-1.1.md").write_text("new\n", encoding="utf-8")
    (src / "docs" / "local-examples" / "settings.yaml").unlink()


V2 = make_release("1.1.0", v2_changes)


def seeded_install(name):
    """An installed 1.0.0 with a teammate's own data, settings and files."""
    base = install(V1, name)
    (base / "data").mkdir()
    (base / "data" / "orders.db").write_bytes(b"customer data")
    (base / "local").mkdir()
    (base / "local" / "receipt.yaml").write_text('receipt_prefix: "WB-B"\n', encoding="utf-8")
    (base / "local" / "github_token").write_text("secret\n", encoding="utf-8")
    (base / "my-notes.txt").write_text("mine\n", encoding="utf-8")
    return base


# --- building -----------------------------------------------------------------

def test_release_zip_contents():
    with zipfile.ZipFile(V1) as zf:
        names = zf.namelist()
        assert all(n.startswith("webuilt-quote/") for n in names)
        assert not any("/tests/" in n or "/tools/" in n or "/.github/" in n for n in names)
        rel = json.loads(zf.read("webuilt-quote/release.json"))
        assert rel["repo"] == "owner/repo" and rel["version"] == "1.0.0"
        assert b"\r\n" in zf.read("webuilt-quote/run.bat")
        assert b"\r\n" not in zf.read("webuilt-quote/run.sh")
        manifest = json.loads(zf.read("webuilt-quote/MANIFEST.json"))
        assert "app.py" in manifest["files"] and "MANIFEST.json" not in manifest["files"]
    sha = (V1.parent / (V1.name + ".sha256")).read_text().split()[0]
    assert sha == digest(V1)
    again = make_release("1.0.0", name="1.0.0-again")
    assert digest(again) == digest(V1)
    print(f"  build           {len(names)} files, no tests/tools/CI, repo injected, "
          "CRLF .bat, reproducible")


# --- updating -----------------------------------------------------------------

def test_update_keeps_teammate_data_and_settings():
    base = seeded_install("update")
    mine = {p: (base / p).read_bytes() for p in
            ("data/orders.db", "local/receipt.yaml", "local/github_token", "my-notes.txt")}
    r = updater.apply_release(V2, base, log=quiet)
    assert (r["from"], r["to"]) == ("1.0.0", "1.1.0")
    for p, b in mine.items():
        assert (base / p).read_bytes() == b, p
    assert "flat_standard_price_per_kg: 560" in (base / "config/filaments.yaml").read_text(encoding="utf-8")
    assert (base / "docs/NEW-IN-1.1.md").exists()
    assert not (base / "docs/local-examples/settings.yaml").exists()
    assert updater.current_version(base) == "1.1.0"
    assert (r["backup"] / "config/filaments.yaml").exists()
    print("  update          price changed, file added and removed; data/, local/, "
          "token and own files untouched")


def test_rollback_restores_exactly():
    base = seeded_install("rollback")
    before = tree(base)
    updater.apply_release(V2, base, log=quiet)
    updater.restore_backup(updater.latest_backup(base), base)
    assert tree(base) == before
    print("  rollback        tree identical to before the update (incl. removed/added files)")


def test_hand_edited_shared_file_is_reported_and_kept():
    base = seeded_install("edited")
    f = base / "config" / "pricing.yaml"
    f.write_text(f.read_text(encoding="utf-8") + "\n# my tweak\n", encoding="utf-8")
    r = updater.apply_release(V2, base, log=quiet)
    assert r["edited"] == ["config/pricing.yaml"]
    assert "# my tweak" in (r["backup"] / "config/pricing.yaml").read_text(encoding="utf-8")
    assert "# my tweak" not in f.read_text(encoding="utf-8")
    print("  hand edits      config/pricing.yaml reported; the edit is kept in the backup")


def test_failure_midway_rolls_back():
    base = seeded_install("midfail")
    before = tree(base)
    v3 = make_release("1.2.0", lambda s: (s / "requirements.txt").write_text(
        (ROOT / "requirements.txt").read_text() + "# changed\n"), name="1.2.0-req")
    real = updater.install_requirements
    updater.install_requirements = lambda b: (_ for _ in ()).throw(UpdateError("pip broke"))
    try:
        updater.apply_release(v3, base, log=quiet)
    except UpdateError as e:
        assert "pip broke" in str(e)
    else:
        raise AssertionError("should have failed")
    finally:
        updater.install_requirements = real
    assert tree(base) == before
    print("  midway failure  package install failed -> back to 1.0.0 exactly")


def test_refuses_downgrade_same_version_and_git():
    base = seeded_install("refuse")
    updater.apply_release(V2, base, log=quiet)
    for z, msg in ((V1, "older"), (V2, "already installed")):
        try:
            updater.apply_release(z, base, log=quiet)
        except UpdateError as e:
            assert msg in str(e), e
        else:
            raise AssertionError(msg)
    (base / ".git").mkdir()
    try:
        updater.apply_release(V1, base, log=quiet, allow_downgrade=True)
    except UpdateError as e:
        assert "git" in str(e)
    else:
        raise AssertionError("git checkout")
    print("  refuses         older version, same version, and git checkouts")


def _rezip(src_zip, out, edit):
    with zipfile.ZipFile(src_zip) as zi, zipfile.ZipFile(out, "w") as zo:
        for n in zi.namelist():
            data = zi.read(n)
            res = edit(n, data)
            for name, blob in (res if isinstance(res, list) else [(n, res)]):
                zo.writestr(name, blob)
    return out


def test_rejects_tampered_and_unsafe_releases():
    cases = {
        "tampered": lambda n, d: d + b"# evil\n" if n.endswith("/app.py") else d,
        "traversal": lambda n, d: [(n, d), ("webuilt-quote/../escape.txt", b"x")]
                                   if n.endswith("/app.py") else d,
        "into data/": lambda n, d: [(n, d), ("webuilt-quote/data/orders.db", b"x")]
                                    if n.endswith("/app.py") else d,
    }
    for label, edit in cases.items():
        base = seeded_install("bad-" + label.split()[0].strip("/"))
        before = tree(base)
        bad = _rezip(V2, TMP / f"bad-{label.split()[0].strip('/')}.zip", edit)
        try:
            updater.apply_release(bad, base, log=quiet)
        except UpdateError:
            pass
        else:
            raise AssertionError(label)
        assert tree(base) == before, label
        assert not (TMP / "escape.txt").exists()
    print("  rejects         tampered file, ../ path, write into data/ — install unchanged")


def test_sidecar_checksum():
    assert updater.verify_sidecar_checksum(V2) is True
    bad = TMP / "copy" / V2.name
    bad.parent.mkdir(exist_ok=True)
    shutil.copy2(V2, bad)
    (bad.parent / (bad.name + ".sha256")).write_text("0" * 64 + "  x\n")
    try:
        updater.verify_sidecar_checksum(bad)
    except UpdateError:
        pass
    else:
        raise AssertionError("bad checksum accepted")
    print("  --zip checksum  matching .sha256 accepted, mismatching rejected")


def test_backups_are_pruned():
    base = seeded_install("prune")
    for i in range(7):
        (base / "backups" / f"1.0.0-2026010{i}-000000").mkdir(parents=True)
        shutil.copy2(base / "MANIFEST.json", base / "backups" / f"1.0.0-2026010{i}-000000" / "MANIFEST.json")
    updater.prune_backups(base)
    left = sorted(d.name for d in (base / "backups").iterdir())
    assert len(left) == 5 and left[0].endswith("20260102-000000"), left
    print("  backups         pruned to the newest five")


# --- talking to GitHub -----------------------------------------------------------

class FakeGitHub(http.server.BaseHTTPRequestHandler):
    seen: list = []
    zip_bytes = b""
    sha_text = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeGitHub.seen.append((self.headers.get("Host"), self.path,
                                self.headers.get("Authorization")))
        port = self.server.server_address[1]
        if self.path == "/repos/owner/repo/releases/latest":
            body = json.dumps({"tag_name": "v1.1.0", "body": "PETG up", "html_url": "u",
                "assets": [{"name": "webuilt-quote-1.1.0.zip",
                            "url": f"http://127.0.0.1:{port}/assets/zip"},
                           {"name": "webuilt-quote-1.1.0.zip.sha256",
                            "url": f"http://127.0.0.1:{port}/assets/sha"}]}).encode()
        elif self.path.startswith("/assets/"):
            # GitHub redirects asset downloads to a different storage host
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/blob/{self.path[8:]}")
            self.end_headers()
            return
        elif self.path == "/blob/zip":
            body = FakeGitHub.zip_bytes
        elif self.path == "/blob/sha":
            body = FakeGitHub.sha_text.encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_download_from_github_with_private_token():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_api = updater.API
    updater.API = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        FakeGitHub.zip_bytes = V2.read_bytes()
        FakeGitHub.sha_text = (V2.parent / (V2.name + ".sha256")).read_text()
        info = updater.latest_release("owner/repo", "tok123")
        assert info["version"] == "1.1.0" and info["notes"] == "PETG up"
        z = updater.download_release(info, TMP, "tok123")
        assert digest(z) == digest(V2)
        api = [a for h, p, a in FakeGitHub.seen if p.startswith(("/repos", "/assets"))]
        blob = [a for h, p, a in FakeGitHub.seen if p.startswith("/blob")]
        assert api and all(a == "Bearer tok123" for a in api), api
        assert blob and all(a is None for a in blob), blob
        FakeGitHub.sha_text = "0" * 64 + "  webuilt-quote-1.1.0.zip\n"
        try:
            updater.download_release(info, TMP, "tok123")
        except UpdateError as e:
            assert "SHA-256" in str(e)
        else:
            raise AssertionError("corrupt download accepted")
    finally:
        updater.API = old_api
        srv.shutdown()
    print("  github          token sent to GitHub, dropped on redirect to storage; "
          "bad checksum rejected")


# --- settings and the app ------------------------------------------------------------

def test_local_overrides_and_broken_files():
    import app as appmod
    cfg_dir, loc_dir = TMP / "cfg", TMP / "loc"
    shutil.copytree(ROOT / "config", cfg_dir)
    loc_dir.mkdir()
    (loc_dir / "printers.yaml").write_text(
        "printers:\n  normal:\n    hazard: 0.005\n  large: null\n", encoding="utf-8")
    store = appmod.ConfigStore(cfg_dir, loc_dir)
    pr = store["printers"]["printers"]
    assert pr["normal"]["hazard"] == 0.005 and pr["normal"]["p_start"] == 0.97
    assert "large" not in pr
    assert store.local_files() == ["printers.yaml"]

    f = loc_dir / "printers.yaml"
    f.write_text("printers: [unclosed\n", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    assert store["printers"]["printers"]["normal"]["hazard"] == 0.005   # last good kept
    assert f"{loc_dir.name}/printers.yaml" in store.errors["printers"]   # names the file
    f.write_text("printers:\n  normal:\n    hazard: 0.007\n", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    assert store["printers"]["printers"]["normal"]["hazard"] == 0.007
    assert "printers" not in store.errors
    print("  local/          merges, null removes, broken file keeps last good + names it")


def test_version_and_update_endpoints():
    from fastapi.testclient import TestClient
    import app as appmod
    c = TestClient(appmod.app)
    v = c.get("/api/version").json()
    assert v["version"] == json.loads((ROOT / "release.json").read_text())["version"]
    assert v["install"] == "git" and v["can_self_update"] is False
    assert c.post("/api/update").status_code == 400                       # no header
    assert c.post("/api/update", headers={"X-WeBuilt-Action": "update"}).status_code == 409
    q = c.post("/api/quote/manual", json={"filaments": [{"type": "PLA", "grams": 5}],
                                          "hours": 1, "save": True}).json()
    assert q["app_version"] == v["version"]
    row = c.get("/api/orders").json()[0]
    assert row["app_version"] == v["version"]
    print(f"  app             /api/version {v['version']}; update needs header and a "
          "release install; quotes record the version")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} updater tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback; traceback.print_exc()
            print(f"  FAIL {fn.__name__}: {e!r}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
