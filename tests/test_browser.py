import asyncio
import json
from pathlib import Path

import pytest

import browser
import config
from browser import BaseScraper, BlockedError, DateUnavailable, ScrapeError


class FakeContext:
    def __init__(self, state):
        self.state = state

    async def storage_state(self):
        return json.loads(json.dumps(self.state))


REAL_STATE_PATH = config.storage_state_path  # captured at import, before the autouse fixture replaces it


def real_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "storage_state_path", REAL_STATE_PATH)
    monkeypatch.setattr(config, "STORAGE_STATE_PATH", tmp_path / "data" / "browser_state.json")


def test_state_file_is_per_airline_and_written_atomically(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)

    class F9(BaseScraper):
        airline = "F9"

    s = F9(screenshots=False)
    assert s.state_path == tmp_path / "data" / "browser_state_F9.json"
    s._context = FakeContext({"cookies": [{"name": "a", "value": "1"}], "origins": [{"origin": "x"}]})
    asyncio.run(s._save_state())
    saved = json.loads(s.state_path.read_text())
    assert saved == {"cookies": [{"name": "a", "value": "1"}], "origins": []}      # cookies only
    assert not list(s.state_path.parent.glob("*.tmp"))                              # temp file renamed away


def test_corrupt_or_missing_state_file_is_ignored(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)
    s = BaseScraper(screenshots=False)
    assert s._usable_state() is None                                                # missing
    s.state_path.parent.mkdir(parents=True)
    s.state_path.write_text('{"cookies": [')                                        # truncated by a crash
    assert s._usable_state() is None
    s.state_path.write_text('{"cookies": [], "origins": []}')
    assert s._usable_state() == str(s.state_path)


def test_legacy_avelo_state_file_is_adopted(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)
    legacy = config.STORAGE_STATE_PATH
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"cookies": [{"name": "old"}], "origins": []}')
    s = BaseScraper(screenshots=False)                                               # airline XP
    assert s._usable_state() == str(s.state_path) and not legacy.exists()
    assert json.loads(s.state_path.read_text())["cookies"] == [{"name": "old"}]


def test_retry_never_retries_answers_or_blocks():
    s = BaseScraper(screenshots=False, max_retries=2)
    for exc in (DateUnavailable("sold out"), BlockedError("challenge")):
        calls = []

        async def fn(exc=exc, calls=calls):
            calls.append(1)
            raise exc

        with pytest.raises(type(exc)):
            asyncio.run(s._retry(fn, "t"))
        assert len(calls) == 1
    assert issubclass(BlockedError, ScrapeError) and issubclass(DateUnavailable, ScrapeError)


def test_snapshot_saves_html_beside_the_screenshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path)

    class Page:
        async def screenshot(self, path, full_page):
            open(path, "wb").write(b"png")

        async def content(self):
            return "<html>challenge</html>"

    s = BaseScraper()
    s._page = Page()
    asyncio.run(s._snapshot("F9-PHL-MCO", 0))
    assert [p.suffix for p in sorted(tmp_path.iterdir())] == [".html", ".png"]
    assert "challenge" in next(tmp_path.glob("*.html")).read_text()


def test_snapshot_keeps_the_html_when_the_screenshot_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path / "shots")
    order = []

    class Page:
        async def screenshot(self, path, full_page):
            order.append("png")
            raise RuntimeError("Protocol error (Page.captureScreenshot): Unable to capture screenshot")

        async def content(self):
            order.append("html")
            return '<html><div id="px-captcha"></div></html>'

    s = BaseScraper()
    s._page = Page()
    asyncio.run(s._snapshot("F9-PHL-MCO", 0))                                   # best-effort: does not raise
    assert order == ["html", "png"]
    assert [p.suffix for p in (tmp_path / "shots").iterdir()] == [".html"]
    assert "px-captcha" in next((tmp_path / "shots").glob("*.html")).read_text()


def test_empty_results_are_snapshotted_but_not_retried():
    calls, shots = [], []
    s = BaseScraper(screenshots=False, max_retries=2)

    async def empty():
        calls.append(1)
        raise browser.EmptyResults("no flights at all")

    async def snapshot(tag, attempt):
        shots.append((tag, attempt))

    s._snapshot = snapshot
    with pytest.raises(browser.EmptyResults):
        asyncio.run(s._retry(empty, "F9-PHL-MCO-2026-11-13"))
    assert len(calls) == 1 and shots == [("F9-PHL-MCO-2026-11-13", 0)]
    assert issubclass(browser.EmptyResults, ScrapeError) and not issubclass(browser.EmptyResults, (BlockedError, DateUnavailable))


def test_cookie_temp_file_is_git_ignored():
    ignored = (Path(__file__).resolve().parent.parent / ".gitignore").read_text().splitlines()
    assert "data/browser_state_*.json.tmp" in ignored and "data/browser_state_*.json" in ignored


def test_report_knows_when_a_run_was_blocked():
    assert browser.ScrapeReport().blocked is False
