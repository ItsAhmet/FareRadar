from pathlib import Path

import config
import scheduler


def test_job_command_and_cron_line():
    cmd = scheduler.job_command(["TPA", "LAL"], 45, promo="SAVE20", python="py")
    assert cmd == ["py", str(config.PROJECT_ROOT / "run_scraper.py"), "--destinations", "TPA,LAL", "--days", "45", "--mode", "calendar", "--promo", "SAVE20"]
    line = scheduler.cron_line(cmd, 3, 15)
    assert line.startswith("15 3 * * * cd ") and "--destinations TPA,LAL" in line


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


def test_targets_from_rules_fall_back_to_all_destinations(tmp_path, monkeypatch):
    import database
    path = tmp_path / "f.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    assert scheduler.targets_from_rules() == (list(config.DESTINATIONS), 60)
    database.add_watch("rule", "r", {"destinations": ["TPA", "CHS"], "span_days": 30})
    assert scheduler.targets_from_rules() == (["CHS", "TPA"], 30)
