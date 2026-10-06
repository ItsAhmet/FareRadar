"""One-click launcher: start the dashboard (if needed) and open it in the browser.

    python launch.py            # start in the foreground; Ctrl+C stops it
    python launch.py --detach   # start in the background (used by the .app)
    python launch.py --stop     # stop a background server

Works with any python3: it re-executes itself with the project's .venv, so a
double-click from Finder or an IDE "Run" button behaves like the terminal.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
PORT = int(os.environ.get("AVELO_PORT", "8501"))
URL = f"http://localhost:{PORT}"
PID_FILE = ROOT / "data" / "streamlit.pid"
LOG_FILE = ROOT / "data" / "streamlit.log"


def _ensure_venv() -> None:
    """Re-exec under the project venv so streamlit & friends are importable."""
    if VENV_PY.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(ROOT / "launch.py"), *sys.argv[1:]])


def is_running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def wait_until_up(timeout: float = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(0.5)
    return False


def start(detach: bool) -> int:
    if is_running():
        print(f"Avelo Finder already running → {URL}")
        webbrowser.open(URL)
        return 0

    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
           "--server.port", str(PORT), "--server.headless", "true"]
    PID_FILE.parent.mkdir(exist_ok=True)

    if detach:
        log = open(LOG_FILE, "ab")
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        PID_FILE.write_text(str(proc.pid))
    else:
        proc = subprocess.Popen(cmd, cwd=ROOT)

    print(f"Starting Avelo Finder on {URL} …")
    if not wait_until_up(timeout=90):
        proc.terminate()
        print(f"Server didn't come up within 90s; see {LOG_FILE if detach else 'output above'}.")
        print("If Python itself is slow to start, check the project isn't in an iCloud-synced folder.")
        return 1
    webbrowser.open(URL)

    if detach:
        return 0
    print("Running. Close this window or press Ctrl+C to stop.")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


def stop() -> int:
    if not PID_FILE.exists():
        print("No background server recorded.")
        return 0
    pid = int(PID_FILE.read_text())
    try:
        os.killpg(pid, signal.SIGTERM)
        print(f"Stopped Avelo Finder (pid {pid}).")
    except ProcessLookupError:
        print("Server was already stopped.")
    PID_FILE.unlink(missing_ok=True)
    return 0


def main() -> int:
    _ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detach", action="store_true", help="run the server in the background")
    ap.add_argument("--stop", action="store_true", help="stop a background server")
    args = ap.parse_args()
    return stop() if args.stop else start(args.detach)


if __name__ == "__main__":
    sys.exit(main())
