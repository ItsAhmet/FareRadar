"""Smoke test: run the Streamlit script headlessly against a mock database."""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import config
import database
from mock_data import generate_mock_flights

APP = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture
def mock_db(tmp_path, monkeypatch):
    path = tmp_path / "flights.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "OFFLINE", True)  # no weather/ntfy calls in tests
    import scheduler
    monkeypatch.setattr(scheduler, "plist_path", lambda: tmp_path / "job.plist")
    database.init_db(path)
    database.upsert_flights(generate_mock_flights(date.today(), 70))
    return path


def test_dashboard_renders_without_exceptions(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception

    labels = [m.label for m in at.metric]
    assert labels[:4] == ["Cheapest getaway", "Best window fit", "Valid packages", "Free windows in span"]
    assert at.metric[2].value != "0", "mock data should yield packages in the default window"
    assert len(at.dataframe) >= 1
    assert any("Execute Target Scrape" in b.label for b in at.button)
    assert any("Wildcard weekend" in b.label for b in at.button)
    assert at.multiselect[0].value == list(config.DESTINATIONS)  # all routes selected by default
    assert any(config.MEMBER_BADGE in m.value for m in at.markdown), "Avelo Plus badge missing"
    assert any("Promo Code" in t.label for t in at.text_input)
    assert any("Inspect a package" in sb.label for sb in at.selectbox)  # trend & weather inspector
    assert any("Watch this package" in b.label for b in at.button)
    assert any("Open on Avelo" in b.label for b in at.button)
    assert any("Plus ROI" in m.value for m in at.markdown)
    assert any("Save rule" in b.label for b in at.button)
    assert any("Install / update" in b.label for b in at.button)
    assert any("This weekend" in b.label for b in at.button)
    assert any(c.label == "Extend around US holidays" for c in at.checkbox)
    assert any(t.label == "Place (optional)" for t in at.text_input)
    rank = next(r for r in at.radio if r.label == "Rank by")
    assert rank.options == ["Cheapest package", "Best window fit", "Sunniest under budget"]


def test_watch_and_rule_buttons_create_watches(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if "Watch this package" in b.label).click().run()
    assert not at.exception
    next(t for t in at.text_input if t.label == "Rule name").set_value("Test rule").run()
    next(b for b in at.button if b.label == "Save rule").click().run()
    assert not at.exception
    kinds = sorted(w["kind"] for w in database.list_watches())
    assert kinds == ["package", "rule"]
    df = next(d.value for d in at.dataframe if "Add-on Fees" in d.value.columns)  # the packages table
    assert "Signals" in df.columns
    assert (df["Add-on Fees"] == 0).all()
    assert (df["Total Getaway Cost"] == df["Out Price"] + df["Back Price"]).all()


def test_wildcard_button_shows_a_package(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if "Wildcard" in b.label).click().run()
    assert not at.exception
    assert any("all-in" in s.value for s in at.success)


def test_region_checkbox_drives_destination_selection(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    florida = next(c for c in at.checkbox if c.label.startswith("Florida"))
    florida.uncheck().run()
    assert not at.exception
    assert not (set(at.multiselect[0].value) & set(config.region_codes("Florida")))
    assert next(c for c in at.checkbox if c.label == "Select all routes").value is False


def test_dashboard_handles_empty_selection(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(c for c in at.checkbox if c.label == "Select all routes").uncheck().run()
    assert not at.exception
    assert at.multiselect[0].value == []
    assert any("Pick at least one destination" in i.value for i in at.info)


def test_place_mode_adds_door_to_door_ranking(mock_db):
    database.set_place("Siesta Key, FL", "Siesta Key, Florida, US", 27.2673, -82.5459)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(t for t in at.text_input if t.label == "Place (optional)").set_value("Siesta Key, FL").run()
    assert not at.exception
    rank = next(r for r in at.radio if r.label == "Rank by")
    assert "Door-to-door" in rank.options
    rank.set_value("Door-to-door").run()
    assert not at.exception
    df = next(d.value for d in at.dataframe if "Door-to-door" in d.value.columns)
    assert df["Door-to-door"].notna().all()
    assert df["Drive (min)"].iloc[0] <= df["Drive (min)"].max()


def test_season_plan_tab_and_booking_flow(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Trips" in labels and "Season cost" in labels  # season planner rendered
    next(b for b in at.button if b.label == "Save booking").click().run()
    assert not at.exception
    assert len(database.list_bookings()) == 1
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert any("1 booked trip" in m.value for m in at.markdown)


def test_identical_season_and_ranked_timelines_do_not_collide(mock_db):
    """Regression: with few packages the season plan equals the top-ranked list,
    so both timelines render the same figure and need distinct keys."""
    with database.connection() as conn:
        conn.execute("DELETE FROM avelo_flights WHERE destination NOT IN ('ILG','TPA') AND origin NOT IN ('ILG','TPA')")
        conn.execute("DELETE FROM avelo_flights WHERE (origin='ILG' AND destination!='TPA') OR (destination='ILG' AND origin!='TPA')")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception


def test_dashboard_labels_follow_the_configured_home_airports(mock_db, monkeypatch):
    codes = ["PHL", "ILG", "BWI"]
    monkeypatch.setattr(config, "HOME_CODES", codes)
    monkeypatch.setattr(config, "HOME_AIRPORTS", {c: config.HOME_AIRPORT_INFO[c] for c in codes})
    monkeypatch.setattr(config, "PRIMARY_HOME", "PHL")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception

    assert at.title[0].value == "Flexible Getaways from PHL / ILG / BWI"
    assert at.sidebar.title[0].value == "✈️ Avelo Getaways"
    markdown = " ".join(m.value for m in at.markdown)
    assert "PHL hub" in markdown and "ILG hub" not in markdown
