"""Update WeBuilt Quote to a newer release, safely.

    python updater.py check              is a newer release out?
    python updater.py apply              install the latest release
    python updater.py apply --zip FILE   install from a downloaded release zip
    python updater.py rollback           go back to the version before the last update

Standard library only, on purpose: it must still work when the app's own
packages are broken, which may be exactly why someone is updating.

What it touches, and what it never touches:

  * Only files listed in MANIFEST.json, the list every release ships of its
    own files. Anything else in the folder is left alone.
  * Never data/ (the order log), local/ (this computer's own settings),
    backups/ or .venv/. A release that tries to write there is rejected.

Every update:

  1. downloads the release zip and checks its SHA-256 against the one
     published with it, then checks every file inside against the zip's
     own manifest;
  2. copies the current version's files to backups/<version>-<time>/;
  3. swaps the files in, removing ones the new release no longer has;
  4. reinstalls packages only if requirements.txt changed;
  5. rolls itself back if any step fails.

Settings files edited in place (config/ instead of local/) are reported:
their edits are kept in the backup, not silently lost.

Private repositories need a GitHub token with read access to the repo, from
WEBUILT_GITHUB_TOKEN, GITHUB_TOKEN, or the file local/github_token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = os.environ.get("WEBUILT_GITHUB_API", "https://api.github.com").rstrip("/")
MANIFEST = "MANIFEST.json"
PROTECTED = ("data", "local", "backups", ".venv", ".update", ".git")
ASSET_PREFIX = "webuilt-quote-"
TIMEOUT = 30


class UpdateError(Exception):
    """Something the person running the update needs to read."""


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

def parse_version(v: str) -> tuple[int, int, int]:
    v = v.strip().lstrip("vV")
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise UpdateError(f"Not a version number: {v!r} (expected like 1.2.3)")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def read_release(base: Path = BASE) -> dict:
    return json.loads((base / "release.json").read_text(encoding="utf-8"))


def current_version(base: Path = BASE) -> str:
    return read_release(base)["version"]


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def github_token(base: Path = BASE) -> str | None:
    for var in ("WEBUILT_GITHUB_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    f = base / "local" / "github_token"
    if f.exists():
        tok = f.read_text(encoding="utf-8").strip()
        return tok or None
    return None


class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """GitHub serves release files by redirecting to a storage host. That host
    rejects a request that still carries the GitHub token, and the token has
    no business going there anyway, so drop it when the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).hostname
                                != urllib.parse.urlsplit(req.full_url).hostname):
            for h in ("Authorization", "authorization"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnHostChange)


def _get(url: str, token: str | None, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "webuilt-quote-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404) and not token:
            raise UpdateError(
                f"GitHub answered {exc.code}. If the repository is private, "
                "put a GitHub token with read access in local/github_token.") from exc
        if exc.code in (401, 403):
            raise UpdateError(f"GitHub refused the token ({exc.code}). "
                              "Check local/github_token is valid and can read the repo.") from exc
        raise UpdateError(f"GitHub answered {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not reach GitHub: {exc.reason}") from exc


def latest_release(repo: str, token: str | None = None) -> dict:
    """{'version', 'notes', 'zip_url', 'sha_url', 'url'} of the newest release."""
    if not repo:
        raise UpdateError("No update source: the 'repo' field in release.json is empty.")
    raw = _get(f"{API}/repos/{repo}/releases/latest", token, "application/vnd.github+json")
    rel = json.loads(raw)
    version = rel["tag_name"].lstrip("vV")
    name = f"{ASSET_PREFIX}{version}.zip"
    assets = {a["name"]: a for a in rel.get("assets", [])}
    if name not in assets or name + ".sha256" not in assets:
        raise UpdateError(f"Release {rel['tag_name']} is missing {name} or its .sha256 file.")
    return {
        "version": version,
        "notes": rel.get("body") or "",
        "url": rel.get("html_url", ""),
        # The API URL (not browser_download_url) works for private repos too.
        "zip_url": assets[name]["url"],
        "sha_url": assets[name + ".sha256"]["url"],
    }


def download_release(info: dict, dest: Path, token: str | None) -> Path:
    data = _get(info["zip_url"], token, "application/octet-stream")
    sha_line = _get(info["sha_url"], token, "application/octet-stream").decode().strip()
    expected = sha_line.split()[0].lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise UpdateError("The download is corrupt or was tampered with: its SHA-256 "
                          "does not match the one published with the release.")
    path = dest / f"{ASSET_PREFIX}{info['version']}.zip"
    path.write_bytes(data)
    return path


def verify_sidecar_checksum(zip_path: Path) -> bool:
    """For a zip downloaded by hand: if its .sha256 file is beside it, check
    it. Returns whether a checksum was checked."""
    side = zip_path.with_name(zip_path.name + ".sha256")
    if not side.exists():
        print("No .sha256 file next to the zip; checking the files inside only.")
        return False
    expected = side.read_text(encoding="utf-8").split()[0].lower()
    if sha256_file(zip_path) != expected:
        raise UpdateError("The zip does not match its .sha256 file: it is corrupt "
                          "or not the published release.")
    return True


# ---------------------------------------------------------------------------
# manifests and safety
# ---------------------------------------------------------------------------

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(root: Path) -> dict:
    p = root / MANIFEST
    if not p.exists():
        raise UpdateError(f"{MANIFEST} is missing from {root}. This folder was not "
                          "installed from a release, so the updater cannot tell "
                          "which files belong to the app.")
    return json.loads(p.read_text(encoding="utf-8"))


def check_relpath(rel: str) -> None:
    """Refuse anything that would land outside the app folder or in a place
    that holds people's own data."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or rel.startswith(("/", "\\")):
        raise UpdateError(f"Release contains an unsafe path: {rel}")
    if p.parts and p.parts[0] in PROTECTED:
        raise UpdateError(f"Release tries to write into {p.parts[0]}/: {rel}")


def extract_release(zip_path: Path, dest: Path) -> Path:
    """Unpack and verify. Returns the folder holding release.json."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        # Releases unpack into one folder (webuilt-quote/); accept a flat zip too.
        tops = {n.split("/", 1)[0] for n in names}
        if "release.json" in names:
            prefix = ""
        elif len(tops) == 1 and f"{next(iter(tops))}/release.json" in names:
            prefix = next(iter(tops)) + "/"
        else:
            raise UpdateError("This zip is not a WeBuilt Quote release (no release.json).")
        for n in names:
            rel = n[len(prefix):]
            check_relpath(rel)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    root = dest
    manifest = read_manifest(root)
    for rel, digest in manifest["files"].items():
        check_relpath(rel)
        f = root / rel
        if not f.exists():
            raise UpdateError(f"Release is incomplete: {rel} is listed but missing.")
        if sha256_file(f) != digest:
            raise UpdateError(f"Release file {rel} does not match its manifest.")
    if read_release(root)["version"] != manifest["version"]:
        raise UpdateError("Release is inconsistent: release.json and MANIFEST.json disagree.")
    return root


# ---------------------------------------------------------------------------
# install, back up, roll back
# ---------------------------------------------------------------------------

def _venv_python(base: Path) -> Path | None:
    for p in (base / ".venv" / "Scripts" / "python.exe", base / ".venv" / "bin" / "python"):
        if p.exists():
            return p
    return None


def install_requirements(base: Path) -> None:
    py = _venv_python(base)
    if py is None:
        return          # the launcher installs them on next start
    r = subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check",
                        "-q", "-r", str(base / "requirements.txt")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError("Installing the new packages failed:\n" + (r.stderr or r.stdout)[-1500:])
    (base / ".venv" / ".requirements.sha256").write_text(
        sha256_file(base / "requirements.txt"), encoding="utf-8")


def _swap_in(src_root: Path, base: Path, new_files: dict, old_files: dict) -> None:
    for rel in new_files:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".webuilt-new")
        shutil.copy2(src_root / rel, tmp)
        os.replace(tmp, target)
    for rel in set(old_files) - set(new_files):
        target = base / rel
        if target.exists():
            target.unlink()
            _prune_empty_dirs(target.parent, base)


def _prune_empty_dirs(d: Path, base: Path) -> None:
    while d != base and d.exists() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def backup_current(base: Path, manifest: dict) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = base / "backups" / f"{manifest['version']}-{stamp}"
    n = 1
    while dest.exists():                       # two updates in the same second
        dest = base / "backups" / f"{manifest['version']}-{stamp}{n:02d}"
        n += 1
    dest.mkdir(parents=True)
    for rel in list(manifest["files"]) + [MANIFEST]:
        src = base / rel
        if src.exists():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
    return dest


def locally_edited(base: Path, manifest: dict) -> list[str]:
    """Files of the app that someone changed by hand since install."""
    return sorted(rel for rel, digest in manifest["files"].items()
                  if (base / rel).exists() and sha256_file(base / rel) != digest)


def refuse_git_checkout(base: Path) -> None:
    if (base / ".git").exists():
        raise UpdateError("This folder is a git checkout, not an installed release. "
                          "Update it with `git pull` instead.")


def apply_release(zip_path: Path, base: Path = BASE, allow_downgrade: bool = False,
                  log=print) -> dict:
    refuse_git_checkout(base)
    old = read_manifest(base)
    with tempfile.TemporaryDirectory(dir=base, prefix=".update-") as tmp:
        new_root = extract_release(zip_path, Path(tmp))
        new = read_manifest(new_root)
        if parse_version(new["version"]) == parse_version(old["version"]):
            raise UpdateError(f"Version {new['version']} is already installed.")
        if parse_version(new["version"]) < parse_version(old["version"]) and not allow_downgrade:
            raise UpdateError(f"{new['version']} is older than the installed "
                              f"{old['version']}. Use rollback to go back.")
        edited = locally_edited(base, old)
        backup = backup_current(base, old)
        log(f"Backed up {old['version']} to {backup.relative_to(base)}")
        req_changed = (old["files"].get("requirements.txt")
                       != new["files"].get("requirements.txt"))
        try:
            _swap_in(new_root, base, new["files"], old["files"])
            shutil.copy2(new_root / MANIFEST, base / MANIFEST)
            if req_changed:
                log("Packages changed; installing them…")
                install_requirements(base)
        except Exception as exc:
            log(f"Update failed ({exc}); restoring {old['version']}…")
            # Also remove anything the half-installed release added.
            restore_backup(backup, base, extra=new["files"])
            raise
    prune_backups(base)
    return {"from": old["version"], "to": new["version"], "backup": backup,
            "edited": edited, "requirements_changed": req_changed}


def restore_backup(backup: Path, base: Path = BASE, extra: dict | None = None) -> dict:
    """Put a backed-up version back. Files belonging to the version being
    replaced (and any `extra` ones) that the backup doesn't have are removed."""
    saved = read_manifest(backup)
    current = read_manifest(base) if (base / MANIFEST).exists() else {"files": {}}
    present = {**current["files"], **(extra or {})}
    _swap_in(backup, base, {r: d for r, d in saved["files"].items()
                            if (backup / r).exists()}, present)
    shutil.copy2(backup / MANIFEST, base / MANIFEST)
    if saved["files"].get("requirements.txt") != current["files"].get("requirements.txt"):
        install_requirements(base)
    return saved


KEEP_BACKUPS = 5


def _backups(base: Path) -> list[Path]:
    root = base / "backups"
    if not root.exists():
        return []
    return sorted((d for d in root.iterdir() if (d / MANIFEST).exists()),
                  key=lambda d: d.name.rsplit("-", 2)[-2:])


def prune_backups(base: Path = BASE, keep: int = KEEP_BACKUPS) -> None:
    for d in _backups(base)[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def latest_backup(base: Path = BASE) -> Path:
    dirs = _backups(base)
    if not dirs:
        raise UpdateError("There is no backup to roll back to.")
    return dirs[-1]


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def _check(args) -> int:
    rel = read_release()
    info = latest_release(rel.get("repo", ""), github_token())
    cur = rel["version"]
    if parse_version(info["version"]) > parse_version(cur):
        print(f"Update available: {cur} -> {info['version']}")
        if info["notes"]:
            print("\n" + info["notes"].strip() + "\n")
        print("Install it with:  python updater.py apply")
        return 10
    print(f"Up to date ({cur}).")
    return 0


def _apply(args) -> int:
    refuse_git_checkout(BASE)
    with tempfile.TemporaryDirectory(dir=BASE, prefix=".download-") as tmp:
        if args.zip:
            zip_path = Path(args.zip).resolve()
            verify_sidecar_checksum(zip_path)
        else:
            rel = read_release()
            info = latest_release(rel.get("repo", ""), github_token())
            if parse_version(info["version"]) <= parse_version(rel["version"]):
                print(f"Already up to date ({rel['version']}).")
                return 0
            print(f"Downloading {info['version']}…")
            zip_path = download_release(info, Path(tmp), github_token())
        result = apply_release(zip_path, allow_downgrade=args.allow_downgrade)
    print(f"Updated {result['from']} -> {result['to']}.")
    if result["edited"]:
        print("\nThese files had been edited by hand and were replaced:")
        for f in result["edited"]:
            print(f"  {f}")
        print(f"Your versions are in {result['backup'].relative_to(BASE)}. "
              "Put personal changes in local/ so updates keep them.")
    return 0


def _rollback(args) -> int:
    refuse_git_checkout(BASE)
    backup = latest_backup()
    saved = restore_backup(backup)
    shutil.rmtree(backup)
    print(f"Rolled back to {saved['version']}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Update WeBuilt Quote.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="see whether a newer release exists")
    a = sub.add_parser("apply", help="install the newest release")
    a.add_argument("--zip", help="install from a release zip you already downloaded")
    a.add_argument("--allow-downgrade", action="store_true", help=argparse.SUPPRESS)
    a.add_argument("--yes", action="store_true", help="(accepted for scripts; never prompts)")
    sub.add_parser("rollback", help="return to the version before the last update")
    args = ap.parse_args(argv)
    try:
        return {"check": _check, "apply": _apply, "rollback": _rollback}[args.cmd](args)
    except UpdateError as exc:
        print(f"Update stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
