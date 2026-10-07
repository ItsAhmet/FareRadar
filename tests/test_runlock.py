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
