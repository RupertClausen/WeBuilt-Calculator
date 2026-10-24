"""Start WeBuilt Quote. Run by run.sh / run.bat; you rarely call it directly.

    python serve.py [--port 8000] [--no-browser]

Listens on 127.0.0.1 only, so the app is reachable from this computer and
nowhere else. If the port is taken, the next free one up to +10 is used;
in team-server mode it waits for that exact port instead.

Exit code 75 means "update requested from the app": the launcher then runs
the updater and starts the app again.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

UPDATE_EXIT_CODE = 75


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Same as uvicorn on Linux: connections from before a restart linger
        # (TIME_WAIT) and must not make the port look taken. Not on Windows,
        # where this option would let us bind a port another app is using.
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port(start: int, tries: int = 10) -> int:
    for port in range(start, start + tries):
        if port_is_free(port):
            return port
    raise SystemExit(f"No free port between {start} and {start + tries - 1}.")


def exact_port(port: int, wait: float = 30) -> int:
    """The team server sits behind a proxy pointed at one port; moving to
    another would take the site down. Wait for it, or stop with an error
    (systemd then tries again)."""
    deadline = time.monotonic() + wait
    while not port_is_free(port):
        if time.monotonic() > deadline:
            raise SystemExit(f"Port {port} is in use; not starting on another one.")
        time.sleep(1)
    return port


def open_browser(url: str) -> None:
    """Open the app in the browser without the browser's own chatter.

    On Linux, xdg-open starts Chrome/Chromium with this window as its output,
    so its internal messages ("zygote … Broken pipe") would land here and
    look like the app failing. Send them nowhere instead. Windows opens the
    browser through the shell, which prints nothing.
    """
    opener = shutil.which("xdg-open") if sys.platform.startswith("linux") else None
    if opener:
        try:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEBUILT_PORT", 8000)))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import uvicorn
    import app as webapp

    port = exact_port(args.port) if webapp.server_mode() else free_port(args.port)
    config = uvicorn.Config(webapp.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    wants_update = threading.Event()

    def request_update() -> None:
        wants_update.set()
        server.should_exit = True

    webapp.app.state.request_update = request_update
    webapp.app.state.self_update = os.environ.get("WEBUILT_LAUNCHER") == "1"

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  WeBuilt Quote {webapp.APP_VERSION} is running at {url}")
    print("  Leave this window open while you use it. Close it (or press Ctrl+C) to stop.\n")

    if not args.no_browser:
        def open_when_ready():
            for _ in range(50):
                if server.started:
                    open_browser(url)
                    return
                time.sleep(0.1)
        threading.Thread(target=open_when_ready, daemon=True).start()

    server.run()
    return UPDATE_EXIT_CODE if wants_update.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
