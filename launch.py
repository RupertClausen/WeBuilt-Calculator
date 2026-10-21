"""Set up and run WeBuilt Quote. Started by run.sh (Linux) or run.bat (Windows).

On the first start it creates a private Python environment in .venv and
installs the exact package versions from requirements.txt. Later starts
reinstall only when requirements.txt has changed.

It then runs the app and stays in a loop: when the app is closed through its
"Update and restart" button, this installs the update and starts the new
version. The logic lives here rather than in run.bat because Windows re-reads
a batch file while running it, so an update replacing run.bat mid-run would
break it. A running Python script is unaffected by its file being replaced.

Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
UPDATE_EXIT_CODE = 75
WINDOWS = os.name == "nt"


def fail(msg: str) -> int:
    print(f"\n{msg}\n", file=sys.stderr)
    if WINDOWS and sys.stdin and sys.stdin.isatty():
        input("Press Enter to close this window…")
    return 1


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def ensure_venv() -> None:
    if venv_python().exists():
        return
    print("First start: setting up WeBuilt Quote (this takes a minute)…")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
    except Exception as exc:
        hint = ("" if WINDOWS else
                "\nOn Ubuntu/Debian install the venv module first:  sudo apt install python3-venv")
        raise SystemExit(fail(f"Could not create the .venv folder: {exc}{hint}"))


def ensure_packages() -> None:
    want = hashlib.sha256((BASE / "requirements.txt").read_bytes()).hexdigest()
    stamp = VENV / ".requirements.sha256"
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == want:
        return
    print("Installing packages…")
    r = subprocess.run([str(venv_python()), "-m", "pip", "install",
                        "--disable-pip-version-check", "-q", "-r",
                        str(BASE / "requirements.txt")])
    if r.returncode != 0:
        raise SystemExit(fail("Installing packages failed. Check the internet "
                              "connection and run this again."))
    stamp.write_text(want, encoding="utf-8")


def main() -> int:
    if sys.version_info < (3, 10):
        return fail(f"WeBuilt Quote needs Python 3.10 or newer; this is "
                    f"{sys.version.split()[0]}.")
    os.chdir(BASE)
    (BASE / "data").mkdir(exist_ok=True)
    (BASE / "local").mkdir(exist_ok=True)
    ensure_venv()

    env = {**os.environ, "WEBUILT_LAUNCHER": "1", "PYTHONUTF8": "1"}
    args = sys.argv[1:]
    try:
        while True:
            ensure_packages()
            code = subprocess.run([str(venv_python()), "serve.py", *args], env=env).returncode
            if code != UPDATE_EXIT_CODE:
                return code
            print("\nInstalling the update…")
            r = subprocess.run([str(venv_python()), "updater.py", "apply"], env=env)
            if r.returncode != 0:
                print("The update did not install; starting the current version again.")
            args = [a for a in args if a != "--no-browser"] + ["--no-browser"]
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
