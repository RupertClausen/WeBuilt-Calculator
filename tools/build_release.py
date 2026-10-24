"""Build the release zip teammates download.

    python tools/build_release.py [--repo owner/name] [--out dist]

Produces, for the version in release.json:

    dist/webuilt-quote-<version>.zip         the app, in a webuilt-quote/ folder
    dist/webuilt-quote-<version>.zip.sha256  its checksum, checked by the updater

The zip carries MANIFEST.json: every file it ships with its SHA-256. The
updater uses it to verify a download and to know which files it may replace
or remove on the next update. Anything not listed is never touched.

`--repo` (default: $GITHUB_REPOSITORY, set automatically on GitHub Actions)
is written into the packaged release.json, so installed copies know where to
look for updates.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "webuilt-quote"

# Shipped to teammates. Everything else in the repository (tests, CI, tools,
# editor settings) stays out of the download.
INCLUDE_FILES = ["app.py", "core.py", "auth.py", "orders.py", "sheets.py",
                 "housekeeping.py", "manage.py",
                 "pricing.py", "receipt.py", "slicefile.py", "serve.py",
                 "launch.py", "updater.py", "release.json", "requirements.txt",
                 "run.sh", "run.bat", "README.md", "CHANGELOG.md"]
INCLUDE_DIRS = ["config", "locales", "static", "docs", "deploy"]

# Fixed timestamp so the same source always gives the same zip.
ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def tracked_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                         text=True, check=True).stdout.split("\n")
    return [f for f in out if f]


def select(files: list[str]) -> list[str]:
    picked = [f for f in files if f in INCLUDE_FILES
              or any(f.startswith(d + "/") for d in INCLUDE_DIRS)]
    missing = [f for f in INCLUDE_FILES if f not in files]
    if missing:
        raise SystemExit(f"Not tracked by git, refusing to build: {missing}")
    return sorted(picked)


def content(rel: str, repo: str | None, root: Path = ROOT) -> bytes:
    data = (root / rel).read_bytes()
    if rel == "release.json" and repo:
        rel_json = json.loads(data)
        rel_json["repo"] = repo
        data = (json.dumps(rel_json, indent=2, ensure_ascii=False) + "\n").encode()
    # Windows batch files must have CRLF line endings, whatever the build host.
    if rel.endswith(".bat"):
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    elif rel.endswith(".sh"):
        data = data.replace(b"\r\n", b"\n")
    return data


def build(out: Path, repo: str | None, files: list[str] | None = None,
          root: Path = ROOT) -> Path:
    """Build from `root` (the repository by default; tests pass a copy)."""
    version = json.loads((root / "release.json").read_text(encoding="utf-8"))["version"]
    files = select(files if files is not None else tracked_files(root))
    blobs = {rel: content(rel, repo, root) for rel in files}
    manifest = {"version": version,
                "files": {rel: hashlib.sha256(b).hexdigest() for rel, b in blobs.items()}}
    blobs["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()

    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"webuilt-quote-{version}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(blobs):
            info = zipfile.ZipInfo(f"{FOLDER}/{rel}", ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if rel.endswith(".sh") else 0o644
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, blobs[rel])
    zip_path.write_bytes(buf.getvalue())
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    (out / (zip_path.name + ".sha256")).write_text(f"{digest}  {zip_path.name}\n",
                                                   encoding="utf-8")
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    if not args.repo:
        print("warning: no --repo; installed copies will not be able to check "
              "for updates", file=sys.stderr)
    z = build(Path(args.out), args.repo)
    print(f"built {z.relative_to(ROOT) if z.is_relative_to(ROOT) else z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
