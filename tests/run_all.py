#!/usr/bin/env python3
"""Run every test module and report a single result."""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).parent
mods = sorted(p for p in HERE.glob("test_*.py"))
rc = 0
for m in mods:
    r = subprocess.run([sys.executable, str(m)], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr)
        rc = 1
print("=" * 50)
print("ALL SUITES PASSED" if rc == 0 else "FAILURES — see above")
sys.exit(rc)
