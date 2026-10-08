"""Guards the docs against claims that contradict the code (Phase 3 review findings)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name):
    return (ROOT / name).read_text()


def _flat(name):
    return re.sub(r"\s+", " ", _read(name))


def test_env_example_does_not_promise_a_dashboard_carry_on_toggle():
    line = next(l for l in _read(".env.example").splitlines() if "carry-on bag per flight leg" in l)
    assert "toggle" not in line and "Count a carry-on bag" in line and "no dashboard control" not in line
    assert "toggle" not in " ".join(l for l in _read("config.py").splitlines() if "carry-on" in l)
    # the sidebar has a "Count a carry-on bag" checkbox; these comments must not call it a toggle


def test_claude_md_calendar_and_events_statements_match_the_code():
    text = _flat("CLAUDE.md")
    assert "The fare calendar and the packages table's signal badge are keyed by airline" not in text
    assert "lowest fare across airlines per route and day" not in text   # the calendar is split by airline since Phase 4
    assert "by_airline=True" in text
    assert "`events.airline` is NULL" in text


def test_no_stale_member_fares_only_total():
    for name in ("CLAUDE.md", "README.md"):
        text = _flat(name)
        assert "member fares only" not in text
        assert "exactly the two member fares" not in text
        assert "locks add-ons at $0" not in text


def test_membership_change_claim_is_scoped():
    for name in ("CLAUDE.md", "README.md"):
        text = _flat(name)
        assert "a membership change raises no alert" not in text
        assert "booked trip" in text and "membership change raises no package-watch or route-signal alert" in text


def test_docs_describe_the_phase_4_dashboard_not_the_missing_one():
    for name in ("CLAUDE.md", "README.md"):
        text = _flat(name)
        assert "no controls yet" not in text and "has no control for that until Phase 4" not in text
        assert "planned for Phase 4" not in text
    claude = _flat("CLAUDE.md")
    for key in ("home_select", "airline_select", "open_jaw", "count_carry_on", "dest_select", "`views.py`"):
        assert key in claude
    assert "all four phases" in claude.lower()
    assert "AVELO_AIRLINES=XP,F9" in _read(".env.example")
    readme = _flat("README.md")
    assert "Airline vs airline" in readme and "Settings" in readme and "Home airports" in readme
