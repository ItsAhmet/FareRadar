"""One scrape at a time: a pid lock file shared by the dashboard, nightly job and CLI.

The file holds "<pid> <started iso>". A lock whose pid is no longer running (a
crashed scrape) or whose contents are unreadable is stale and is taken over.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import config

LOCK_PATH = config.PROJECT_ROOT / "data" / "scrape.lock"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def holder_pid(path: Path | None = None) -> int | None:
    try:
        return int((path or LOCK_PATH).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def acquire(path: Path | None = None) -> bool:
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = holder_pid(path)
            if pid is not None and _alive(pid):
                return False
            path.unlink(missing_ok=True)  # stale: holder died or file is garbage
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}")
        return True
    return False


def release(path: Path | None = None) -> None:
    path = path or LOCK_PATH
    if holder_pid(path) == os.getpid():
        path.unlink(missing_ok=True)
