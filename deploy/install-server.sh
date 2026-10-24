#!/usr/bin/env bash
# First-time install of the WeBuilt Quote team server on Debian, from a
# release zip. Run as root on the server:
#
#   bash install-server.sh webuilt-quote-X.Y.Z.zip
#
# Creates the unprivileged `webuilt` user, unpacks the release into
# /opt/webuilt-quote, installs and starts the systemd service. The first
# start creates .venv and installs the pinned packages (needs internet).
#
# Refuses to run over an existing install: update that with
# /opt/webuilt-quote/deploy/update.sh or the Admin page instead.
# Never touches data/ (orders) or local/ (settings, keys).
set -euo pipefail

ZIP="${1:?usage: install-server.sh webuilt-quote-X.Y.Z.zip}"
APP=/opt/webuilt-quote

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -f "$ZIP" ] || { echo "No such file: $ZIP"; exit 1; }
if [ -f "$APP/MANIFEST.json" ]; then
  echo "WeBuilt Quote is already installed in $APP; use deploy/update.sh to update."
  exit 1
fi
command -v python3 >/dev/null && python3 -c 'import venv' 2>/dev/null || {
  echo "Installing python3-venv…"; apt-get update -qq && apt-get install -y -qq python3 python3-venv; }

id webuilt >/dev/null 2>&1 || \
  useradd --system --home-dir "$APP" --no-create-home --shell /usr/sbin/nologin webuilt
mkdir -p "$APP"

# Unpack with Python's zipfile, refusing anything that would land outside
# the app folder or inside data/ or local/.
python3 - "$ZIP" "$APP" <<'PY'
import sys, zipfile, pathlib
zip_path, app = sys.argv[1], pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(zip_path) as zf:
    names = [n for n in zf.namelist() if not n.endswith("/")]
    prefix = "webuilt-quote/" if all(n.startswith("webuilt-quote/") for n in names) else ""
    for n in names:
        rel = pathlib.PurePosixPath(n[len(prefix):])
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] in ("data", "local", ".venv"):
            raise SystemExit(f"Refusing unsafe path in release: {n}")
        out = app / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(n))
print(f"unpacked {len(names)} files")
PY

mkdir -p "$APP/data" "$APP/local"
chown -R webuilt:webuilt "$APP"
chmod 750 "$APP" "$APP/data" "$APP/local"

install -m 644 "$APP/deploy/webuilt-quote.service" /etc/systemd/system/webuilt-quote.service
systemctl daemon-reload
systemctl enable --now webuilt-quote
echo "Installed. First start installs packages; follow it with: journalctl -fu webuilt-quote"
