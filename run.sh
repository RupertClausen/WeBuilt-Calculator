#!/usr/bin/env bash
# Start WeBuilt Quote on Linux:  bash run.sh
# Finds Python 3.10+ and hands over to launch.py, which does the rest.
cd "$(dirname "$0")" || exit 1
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    exec "$c" launch.py "$@"
  fi
done
echo "WeBuilt Quote needs Python 3.10 or newer."
echo "On Ubuntu/Debian:  sudo apt install python3 python3-venv"
exit 1
