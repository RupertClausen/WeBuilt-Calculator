#!/usr/bin/env bash
# Update the team server to the newest release (run as root on the server).
#   /opt/webuilt-quote/deploy/update.sh
# Stops the service, runs the same updater teammates' copies use (backup,
# checksum, rollback on failure), then starts it again either way.
set -u
APP=/opt/webuilt-quote
systemctl stop webuilt-quote
# runuser, not sudo: minimal Debian containers have no sudo.
runuser -u webuilt -- env PYTHONUTF8=1 "$APP/.venv/bin/python" "$APP/updater.py" apply
status=$?
systemctl start webuilt-quote
exit $status
