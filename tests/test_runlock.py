import os
import subprocess

import runlock


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_second_acquire_fails_while_holder_alive(tmp_path):
    lock = tmp_path / "scrape.lock"
    assert runlock.acquire(lock)
    assert runlock.holder_pid(lock) == os.getpid()
    assert not runlock.acquire(lock)
    runlock.release(lock)
    assert not lock.exists()


def test_stale_or_garbage_lock_is_taken_over(tmp_path):
    lock = tmp_path / "scrape.lock"
    lock.write_text(f"{dead_pid()} 2026-10-06T03:15:00")
    assert runlock.acquire(lock)
    runlock.release(lock)
    lock.write_text("garbage")
    assert runlock.acquire(lock)
    runlock.release(lock)


def test_release_leaves_someone_elses_lock(tmp_path):
    lock = tmp_path / "scrape.lock"
    lock.write_text("1 2026-10-06T03:15:00")   # pid 1 (launchd) is always alive
    runlock.release(lock)
    assert lock.exists()
    assert not runlock.acquire(lock)


# --- races between processes (regression: both racers used to get the lock) ---

import sys
import textwrap
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)


def _spawn_acquirer(lock, *, ready=None, pause=0.0, hold=1.5):
    """A child process that acquires `lock` and prints 'A True' / 'A False'.

    With `ready`, it touches that file and sleeps `pause` seconds after creating
    and opening the lock file but before writing its pid into it (the old gap)."""
    script = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {ROOT!r})
        import runlock
        ready = {str(ready) if ready else None!r}
        if ready:
            real = runlock._write_holder
            def slow(fd):
                pathlib.Path(ready).write_text("x")
                time.sleep({pause})
                real(fd)
            runlock._write_holder = slow
        print("A", runlock.acquire(pathlib.Path({str(lock)!r})), flush=True)
        time.sleep({hold})
    """)
    return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)


def test_racer_cannot_steal_a_lock_whose_pid_is_not_written_yet(tmp_path):
    lock, ready = tmp_path / "scrape.lock", tmp_path / "ready"
    a = _spawn_acquirer(lock, ready=ready, pause=1.0)
    for _ in range(200):
        if ready.exists():
            break
        time.sleep(0.025)
    assert ready.exists()
    assert lock.exists() and lock.read_text() == ""   # A is inside the gap
    b_ok = runlock.acquire(lock)
    out, _ = a.communicate(timeout=15)
    if b_ok:
        runlock.release(lock)
    assert out.strip() == "A True"
    assert not b_ok


def test_two_takers_of_a_stale_lock_cannot_both_win(tmp_path, monkeypatch):
    lock = tmp_path / "scrape.lock"
    lock.write_text(f"{dead_pid()} 2026-10-06T03:15:00")
    child = {}
    real_alive = runlock._alive

    def alive_while_a_tries(pid):
        # B has judged the lock; A tries to take it before B writes its pid.
        child["p"] = p = _spawn_acquirer(lock, hold=0.0)
        child["out"] = p.communicate(timeout=15)[0].strip()
        return real_alive(pid)

    monkeypatch.setattr(runlock, "_alive", alive_while_a_tries)
    b_ok = runlock.acquire(lock)
    runlock.release(lock)
    assert b_ok and child["out"] == "A False"


def test_lock_dies_with_its_holder(tmp_path):
    lock = tmp_path / "scrape.lock"
    a = _spawn_acquirer(lock, hold=30)
    assert a.stdout.readline().strip() == "A True"
    assert not runlock.acquire(lock)
    assert runlock.running_pid(lock) == a.pid
    a.kill()
    a.wait()
    assert runlock.running_pid(lock) is None      # file left behind with a dead pid
    assert runlock.acquire(lock)
    runlock.release(lock)
    assert not lock.exists()


def test_nonpositive_or_huge_pid_is_garbage(tmp_path):
    lock = tmp_path / "scrape.lock"
    for junk in ("", "0 x", "-1 x", "99999999999999999999 x"):
        lock.write_text(junk)
        assert runlock.acquire(lock), junk
        runlock.release(lock)
