from pathlib import Path

import config
import scheduler


def test_job_command_reads_targets_at_run_time():
    cmd = scheduler.job_command(mode="calendar", promo="save20", python="py")
    assert cmd == ["py", str(config.PROJECT_ROOT / "run_scraper.py"), "--from-rules", "--mode", "calendar", "--promo", "save20"]


def test_cron_line_runs_the_command_at_the_chosen_time():
    line = scheduler.cron_line(scheduler.job_command(python="py"), 3, 15)
    assert line.startswith("15 3 * * * cd ") and "--from-rules --mode calendar" in line


def test_render_plist_shape():
    plist = scheduler.render_plist(["py", "run_scraper.py"], 4, 30)
    assert plist["Label"] == config.SCHEDULE_LABEL
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert plist["ProgramArguments"] == ["py", "run_scraper.py"]


def test_status_reads_installed_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "plist_path", lambda: tmp_path / "job.plist")
    monkeypatch.setattr(scheduler, "is_macos", lambda: False)
    assert scheduler.status() == {"installed": False}
    import plistlib
    with open(tmp_path / "job.plist", "wb") as fh:
        plistlib.dump(scheduler.render_plist(["py", "x"], 2, 5), fh)
    st = scheduler.status()
    assert st["installed"] and st["hour"] == 2 and st["minute"] == 5 and st["loaded"] is False
    assert scheduler.remove() is True and not (tmp_path / "job.plist").exists()


def test_install_on_non_macos_returns_cron_line(monkeypatch):
    monkeypatch.setattr(scheduler, "is_macos", lambda: False)
    result = scheduler.install(["py", "x"], 1, 2)
    assert result["installed"] is False and result["cron"].startswith("2 1 * * *")
