import asyncio
from datetime import date

import pytest

import config
import handoff
from handoff import handoff_command, parse_legs


def test_handoff_command_round_trips_legs():
    legs = [("ILG", "TPA", date(2026, 10, 9), "XP"), ("LAL", "ILG", date(2026, 10, 12), "XP")]
    cmd = handoff_command(legs, promo="save20", python="py")
    assert cmd == ["py", str(config.PROJECT_ROOT / "handoff.py"), "--legs", "ILG-TPA:2026-10-09:XP,LAL-ILG:2026-10-12:XP", "--promo", "SAVE20"]
    assert parse_legs(cmd[3]) == legs
    assert "--promo" not in handoff_command(legs, python="py")


def test_legs_carry_an_airline_and_default_to_avelo():
    legs = handoff.parse_legs("ILG-TPA:2026-10-09,TPA-PHL:2026-10-12:f9")
    assert legs == [("ILG", "TPA", date(2026, 10, 9), "XP"), ("TPA", "PHL", date(2026, 10, 12), "F9")]
    cmd = handoff.handoff_command(legs, promo="save20", python="py")
    assert cmd[cmd.index("--legs") + 1] == "ILG-TPA:2026-10-09:XP,TPA-PHL:2026-10-12:F9"
    assert handoff.parse_legs(cmd[cmd.index("--legs") + 1]) == legs
    with pytest.raises(SystemExit):
        handoff.parse_legs("ILG-TPA:2026-10-09:ZZ")


def test_frontier_legs_become_links_and_never_reach_avelos_form():
    legs = [("ILG", "TPA", date(2026, 10, 9), "XP"), ("TPA", "PHL", date(2026, 10, 12), "F9")]
    avelo, urls = handoff.split_legs(legs)
    assert avelo == [("ILG", "TPA", date(2026, 10, 9))]
    assert urls == ["https://booking.flyfrontier.com/Flight/InternalSelect?o1=TPA&d1=PHL&dd1=Oct%2012%2C%202026&ADT=1&mon=true&promo="]
    opened = []
    assert handoff.open_urls(urls, opener=opened.append) == 1 and opened == urls


def test_all_frontier_handoff_starts_no_browser(monkeypatch):
    def no_browser(*_a, **_k):
        raise AssertionError("the assisted Avelo browser must not start for Frontier-only legs")

    opened = []
    monkeypatch.setattr(handoff, "async_playwright", no_browser)
    monkeypatch.setattr(handoff.webbrowser, "open", lambda url: opened.append(url) or True)
    code = asyncio.run(handoff.run([("PHL", "MCO", date(2026, 11, 12), "F9")], promo=None, hold=0))
    assert code == 0 and len(opened) == 1 and "o1=PHL&d1=MCO" in opened[0]
