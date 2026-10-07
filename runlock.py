"""One scrape at a time: a lock file shared by the dashboard, nightly job and CLI.

Mutual exclusion comes from the kernel: the holder keeps an exclusive
``flock`` on the lock file for the whole run, and the kernel drops it when the
holder exits or crashes. Every decision in ``acquire`` is made while holding
that flock, so two scrapes starting at the same moment cannot both win (deciding
from the file's contents alone let two racers both take the lock).

The file also holds "<pid> <started iso>" so the dashboard and CLI can say who
is running. A file left behind by a crash keeps its dead pid; nobody has it
flocked, so the next scrape takes it over. A file naming a live pid that nobody
has flocked (written by hand, or by the earlier pid-file version) is respected.
"""
from __future__ import annotations

import fcntl
import os
from datetime import datetime
from pathlib import Path

import config

LOCK_PATH = config.PROJECT_ROOT / "data" / "scrape.lock"
BUSY_EXIT = 3  # run_scraper.main's exit code when another scrape holds the lock

_held: dict[str, int] = {}  # resolved lock path -> fd that carries our flock


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except (OverflowError, ValueError):
        return False
    return True


def holder_pid(path: Path | None = None) -> int | None:
    """The pid recorded in the lock file, or None when absent or unreadable."""
    try:
        pid = int((path or LOCK_PATH).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid > 0 else None  # kill(0 or negative) would probe a whole group


def running_pid(path: Path | None = None) -> int | None:
    """The pid of a scrape running right now, else None. Read-only: takes no lock."""
    pid = holder_pid(path)
    return pid if pid is not None and _alive(pid) else None


def _write_holder(fd: int) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
    os.fsync(fd)


def _same_file(fd: int, path: Path) -> bool:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    fs = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (fs.st_dev, fs.st_ino)


def acquire(path: Path | None = None) -> bool:
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    if key in _held:  # this process already holds it
        return False
    for _ in range(5):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)  # not inherited by children
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        # release() unlinks the file before dropping its flock; if we locked that
        # orphaned inode, the path now names another file (or none): start over.
        if not _same_file(fd, path):
            os.close(fd)
            continue
        pid = holder_pid(path)
        if pid is not None and pid != os.getpid() and _alive(pid):
            os.close(fd)  # unlocked file naming a live process: respect it
            return False
        _write_holder(fd)  # empty, garbage or dead pid: ours now
        _held[key] = fd
        return True
    return False


def release(path: Path | None = None) -> None:
    path = path or LOCK_PATH
    fd = _held.pop(str(path.resolve()), None)
    if holder_pid(path) == os.getpid():
        path.unlink(missing_ok=True)  # unlink before unlocking (see acquire)
    if fd is not None:
        os.close(fd)  # drops the flock
