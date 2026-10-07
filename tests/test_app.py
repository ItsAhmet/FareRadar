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
    import runlock
    monkeypatch.setattr(runlock, "LOCK_PATH", tmp_path / "scrape.lock")
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


def test_clearing_the_place_drops_cached_drive_times(mock_db):
    """Regression: run_search hands back the same cached Itinerary objects while the
    query is unchanged, and drives were only attached while a place was set, so clearing
    the place (destinations unchanged) left stale drive minutes and $/hour on them."""
    database.set_place("Siesta Key, FL", "Siesta Key, Florida, US", 27.2673, -82.5459)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    at.multiselect[0].set_value(["TPA"]).run()  # same airport set with and without the place
    next(t for t in at.text_input if t.label == "Place (optional)").set_value("Siesta Key, FL").run()
    assert not at.exception, at.exception
    df = next(d.value for d in at.dataframe if "Door-to-door" in d.value.columns)
    assert len(df) and df["Door-to-door"].notna().all()

    next(t for t in at.text_input if t.label == "Place (optional)").set_value("").run()
    assert not at.exception, at.exception
    df = next(d.value for d in at.dataframe if "Door-to-door" in d.value.columns)
    assert len(df) and df["Door-to-door"].isna().all() and df["Drive (min)"].isna().all()


class _FinishedProc:
    """Stands in for the scrape Popen once it has exited."""
    def __init__(self, rc):
        self.returncode = rc

    def poll(self):
        return self.returncode


def test_scrape_button_while_another_scrape_runs_says_busy(mock_db, tmp_path):
    (tmp_path / "scrape.lock").write_text("1 2026-10-06T03:15:00")   # pid 1 is always alive
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if "Execute Target Scrape" in b.label).click().run()
    assert not at.exception, at.exception
    assert "scrape_proc" not in at.session_state or at.session_state["scrape_proc"] is None  # nothing spawned
    assert any("Another scrape is already running (pid 1)" in i.value for i in at.info)
    assert not any("reported errors" in e.value for e in at.error)


def test_scrape_exit_code_3_is_busy_not_an_error(mock_db, tmp_path):
    import runlock
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.session_state["scrape_proc"] = _FinishedProc(runlock.BUSY_EXIT)
    at.session_state["scrape_started"] = 0.0
    at.run()
    assert not at.exception, at.exception
    assert any("Another scrape is already running" in i.value for i in at.info)
    assert not any("reported errors" in e.value for e in at.error)


def test_failed_scrape_still_reports_errors(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.session_state["scrape_proc"] = _FinishedProc(1)
    at.session_state["scrape_started"] = 0.0
    at.run()
    assert not at.exception, at.exception
    assert any("Scrape reported errors" in e.value for e in at.error)


def test_packages_from_a_second_home_airport_show_up(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    with database.connection() as conn:
        conn.execute("""
            INSERT INTO avelo_flights (flight_id, origin, destination, departure_datetime, arrival_datetime, price,
                currency, duration_minutes, flight_number, base_price, is_member_fare, promo_code, timestamp_scraped)
            SELECT replace(flight_id, 'ILG', 'PHL'),
                   CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END,
                   CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END,
                   departure_datetime, arrival_datetime, price - 40, currency, duration_minutes, flight_number,
                   base_price, is_member_fare, promo_code, timestamp_scraped
            FROM avelo_flights WHERE origin = 'ILG' OR destination = 'ILG'
        """)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if "Home" in df.value.columns]
    assert tables and (tables[0]["Home"] == "PHL").any()
    phl = tables[0][tables[0]["Home"] == "PHL"]
    assert (phl["Home-side $"] > 0).all(), "PHL parking (default $17/day) must be counted"


def test_packages_table_explains_door_to_door_and_home_side_costs(mock_db):
    import json

    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception
    table = next(d for d in at.dataframe if "Add-on Fees" in d.value.columns)  # the packages table
    columns = json.loads(table.proto.columns)
    door, home = columns["Door-to-door"]["help"], columns["Home-side $"]["help"]
    assert "Home-side" in door and "parking" in door
    assert "Parking" in home and "PHL $17" in home and "no dashboard control" in home


def test_latest_run_signals_cover_every_airlines_newest_run(mock_db):
    """One scrape writes a run row per airline; the Frontier row being newest must not hide Avelo's signals."""
    from signals import Event

    day = date.today()
    stale = database.start_run("calendar", [("ILG", "TPA")])
    xp = database.start_run("calendar", [("ILG", "TPA")])
    f9 = database.start_run("calendar", [("PHL", "MCO")], airline="F9")
    database.add_events(stale, [Event("sale_wave", "ILG", "SAV", day, "an older Avelo run")])
    database.add_events(xp, [Event("sale_wave", "ILG", "TPA", day, "Avelo sale")])
    database.add_events(f9, [Event("new_floor", "PHL", "MCO", day, "Frontier low")])
    for run in (stale, xp, f9):
        database.finish_run(run, {})

    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    table = next(d.value for d in at.dataframe if list(d.value.columns) == ["Kind", "Route", "Day", "Detail"])
    assert sorted(table["Detail"]) == ["Avelo sale", "Frontier low"]


def test_frontier_only_database_shows_no_signals_but_does_appear_in_freshness(mock_db):
    # documented in README/CLAUDE.md: packages, signals and pushes are Avelo-only; the freshness table and the
    # selection counts are not airline-aware yet (Phase 3)
    from datetime import datetime, timedelta
    from models import Flight
    import signals
    days = [date.today() + timedelta(days=20 + i) for i in range(6)]
    for price in (100.0, 60.0):
        run = database.start_run("full", [("PHL", "MCO")], None, airline="F9")
        fares = [Flight(flight_id="F9-2415-PHL-MCO", origin="PHL", destination="MCO",
                        departure_datetime=datetime.combine(d, datetime.min.time()).replace(hour=7),
                        price=price, base_price=price, member_price=price - 10, airline="F9") for d in days]
        database.upsert_flights(fares)
        database.record_seen(run, "PHL", "MCO", days, fares)
        database.finish_run(run, {}, status="ok")
        assert signals.run_detection(run) == []
    assert database.list_events(limit=50) == []
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert not any("PHL→MCO" in t and "lowest fare" in t for t in texts)
    assert not any("floor" in df.value.to_string() and "PHL" in df.value.to_string() for df in at.dataframe)
    # the known gap, pinned at the query the freshness table uses (the page itself caches it across AppTests)
    assert any((r["origin"], r["destination"]) == ("PHL", "MCO") for r in database.route_freshness(["PHL"], ["MCO"]))
