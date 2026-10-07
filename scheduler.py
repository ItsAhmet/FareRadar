"""Unattended nightly scrapes.

macOS: a launchd user agent (`~/Library/LaunchAgents/<label>.plist`) that runs
`run_scraper.py --from-rules` at the chosen time, so each run covers what the
saved rules, watches and bookings need that night. Elsewhere: the equivalent cron line is returned for the user to
paste. The job's own log goes to data/scheduled.log.
"""
from __future__ import annotations

import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import config


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{config.SCHEDULE_LABEL}.plist"


def job_log() -> Path:
    return config.PROJECT_ROOT / "data" / "scheduled.log"


def job_command(mode: str = "calendar", promo: str | None = None, python: str | None = None) -> list[str]:
    """The targets are not baked in: `--from-rules` reads rules, watches and bookings when the job runs."""
    cmd = [python or sys.executable, str(config.PROJECT_ROOT / "run_scraper.py"), "--from-rules", "--mode", mode]
    if promo:
        cmd += ["--promo", promo]
    return cmd


def render_plist(cmd: list[str], hour: int, minute: int) -> dict:
    return {
        "Label": config.SCHEDULE_LABEL,
        "ProgramArguments": cmd,
        "WorkingDirectory": str(config.PROJECT_ROOT),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StandardOutPath": str(job_log()),
        "StandardErrorPath": str(job_log()),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
        "RunAtLoad": False,
    }


def cron_line(cmd: list[str], hour: int, minute: int) -> str:
    return f"{int(minute)} {int(hour)} * * * cd {shlex.quote(str(config.PROJECT_ROOT))} && {shlex.join(cmd)} >> {shlex.quote(str(job_log()))} 2>&1"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(cmd: list[str], hour: int, minute: int) -> dict:
    """Write and load the agent. Returns a status dict; on non-macOS returns
    the cron line instead of installing."""
    if not is_macos():
        return {"installed": False, "cron": cron_line(cmd, hour, minute)}
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    job_log().parent.mkdir(parents=True, exist_ok=True)
    _launchctl("unload", str(path))  # ignore result: may not be loaded
    with open(path, "wb") as fh:
        plistlib.dump(render_plist(cmd, hour, minute), fh)
    result = _launchctl("load", str(path))
    return {"installed": result.returncode == 0, "path": str(path), "error": result.stderr.strip() or None}


def remove() -> bool:
    path = plist_path()
    if not path.exists():
        return False
    _launchctl("unload", str(path))
    path.unlink()
    return True


def status() -> dict:
    path = plist_path()
    if not path.exists():
        return {"installed": False}
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except Exception:  # noqa: BLE001
        return {"installed": False, "error": "unreadable plist"}
    interval = data.get("StartCalendarInterval", {})
    loaded = is_macos() and config.SCHEDULE_LABEL in _launchctl("list").stdout
    return {
        "installed": True,
        "loaded": loaded,
        "hour": interval.get("Hour"),
        "minute": interval.get("Minute"),
        "command": data.get("ProgramArguments", []),
        "path": str(path),
    }
