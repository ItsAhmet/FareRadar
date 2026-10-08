"""Smoke test: run the Streamlit script headlessly against a mock database."""
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
import database
from mock_data import generate_mock_flights

APP = Path(__file__).resolve().parent.parent / "app.py"


def _airports(at):
    """The destination multiselect (the sidebar also has home-airport and airline multiselects)."""
    return next(m for m in at.multiselect if m.label == "Airports")


AVELO, FRONTIER = "🟣 Avelo", "🟢 Frontier"     # the packages table's Airlines cell: a dot in the airline's colour


def _rendered(at) -> str:
    """Everything the script sent to the browser, as text: the protobuf of every element in every container
    (sidebar and main, any element type: tables, code blocks, charts, widget labels and help), plus each
    element's value. A test that must prove something is NOT on the page scans this, not a few element kinds."""
    parts = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if proto is not None:
            parts.append(str(proto))                     # text format: strings appear verbatim, bytes escaped
            parts.append(proto.SerializeToString().decode("utf-8", "replace"))   # Arrow tables, non-ASCII text
        for attr in ("value", "label", "help", "options", "placeholder"):
            try:
                parts.append(str(getattr(node, attr)))
            except Exception:
                pass
        children = getattr(node, "children", None)
        if isinstance(children, dict):
            for child in children.values():
                walk(child)

    for root in at._tree.children.values():
        walk(root)
    return "\n".join(parts)


def _packages_table(at):
    return next(d for d in at.dataframe if "Add-on Fees" in d.value.columns)


def _heatmap_rows(at) -> list[str]:
    import json
    specs = [json.loads(chart.proto.spec) for chart in at.get("plotly_chart") if chart.proto.spec]
    return [y for spec in specs for tr in spec.get("data", []) if tr.get("type") == "heatmap" for y in tr["y"]]


def _codes(multiselect):
    """Airport codes a multiselect offers: AppTest reports options as displayed ('DEN · Denver · Frontier')."""
    return [o.split(" · ")[0] for o in multiselect.options]


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
    st.cache_data.clear()  # the page's cached readers ignore the DB path; each test has its own database
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
    assert _airports(at).value == list(config.DESTINATIONS)  # every Avelo route selected by default
    assert any(config.MEMBER_BADGE in m.value for m in at.markdown), "Avelo Plus badge missing"
    assert any("Promo Code" in t.label for t in at.text_input)
    assert any("Inspect a package" in sb.label for sb in at.selectbox)  # trend & weather inspector
    assert any("Watch this package" in b.label for b in at.button)
    assert any("Open on airline site" in b.label for b in at.button)
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
    florida = lambda: next(c for c in at.checkbox if c.label.startswith("Florida"))
    assert florida().value is False                     # Frontier's Florida airports are offered but not preselected
    florida().check().run()
    assert not at.exception
    assert {"TPA", "MIA"} <= set(_airports(at).value)
    florida().uncheck().run()
    assert not at.exception
    assert not (set(_airports(at).value) & set(config.region_codes("Florida")))
    assert next(c for c in at.checkbox if c.label == "Select all routes").value is False


def test_dashboard_handles_empty_selection(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    select_all = lambda: next(c for c in at.checkbox if c.label == "Select all routes")
    select_all().check().run()
    assert not at.exception
    assert _airports(at).value == _codes(_airports(at))
    select_all().uncheck().run()
    assert not at.exception
    assert _airports(at).value == []
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
    assert at.sidebar.title[0].value == "✈️ Getaway Finder"
    markdown = " ".join(m.value for m in at.markdown)
    assert "home airport" in markdown and " hub" not in markdown      # the map legend names no single hub


def test_clearing_the_place_drops_cached_drive_times(mock_db):
    """Regression: run_search hands back the same cached Itinerary objects while the
    query is unchanged, and drives were only attached while a place was set, so clearing
    the place (destinations unchanged) left stale drive minutes and $/hour on them."""
    database.set_place("Siesta Key, FL", "Siesta Key, Florida, US", 27.2673, -82.5459)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _airports(at).set_value(["TPA"]).run()  # same airport set with and without the place
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
    # spec §6: the "would save" column only when non-zero. An Avelo PLUS member with Avelo fares has nothing to save.
    assert (table.value["Program would save"] == 0).all() and "Program would save" not in table.proto.column_order
    assert "Savings vs Public" in table.proto.column_order and set(table.value["Airlines"]) == {AVELO}
    assert "Parking" in home and "PHL $17" in home and "Settings" in home and "no dashboard control" not in home


def test_latest_run_signals_cover_every_airlines_newest_run(mock_db):
    """One scrape writes a run row per airline; the Frontier row being newest must not hide Avelo's signals."""
    from signals import Event

    day = date.today()
    stale = database.start_run("calendar", [("ILG", "TPA")])
    xp = database.start_run("calendar", [("ILG", "TPA")])
    f9 = database.start_run("calendar", [("PHL", "MCO")], airline="F9")
    database.add_events(stale, [Event("sale_wave", "ILG", "SAV", day, "an older Avelo run")])
    database.add_events(xp, [Event("sale_wave", "ILG", "TPA", day, "Avelo sale", airline="XP")])
    database.add_events(f9, [Event("new_floor", "PHL", "MCO", day, "Frontier low", airline="F9")])
    for run in (stale, xp, f9):
        database.finish_run(run, {})

    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    table = next(d.value for d in at.dataframe if list(d.value.columns) == ["Kind", "Airline", "Route", "Day", "Detail"])
    assert sorted(zip(table["Airline"], table["Detail"])) == [("F9", "Frontier low"), ("XP", "Avelo sale")]


def test_frontier_only_database_shows_frontier_signals_and_appears_in_freshness(mock_db):
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
        events = signals.run_detection(run)
    assert {"sale_wave", "new_floor"} <= {e.kind for e in events}
    assert {e["airline"] for e in database.list_events(limit=50)} == {"F9"}
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any("Frontier PHL→MCO" in t and "lowest fare" in t for t in texts)          # the alerts feed names the airline
    table = next(d.value for d in at.dataframe if "Airline" in d.value.columns)
    assert set(table["Airline"]) == {"F9"} and set(table["Route"]) == {"PHL→MCO"}
    # pinned at the query the freshness table uses (the page itself caches it across AppTests)
    assert any((r["origin"], r["destination"]) == ("PHL", "MCO") for r in database.route_freshness(["PHL"], ["MCO"]))


def test_signal_badge_belongs_to_the_airline_that_raised_it(mock_db, monkeypatch):
    """On a route both airlines fly, Frontier's sale and sell-out badge Frontier legs only (and Avelo's stay Avelo's)."""
    from datetime import datetime
    import streamlit as st
    from models import Flight
    import signals

    st.cache_data.clear()                                            # the signal map is cached across AppTests
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    with database.connection() as conn:                              # Avelo also flies PHL⇄MCO
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
        out_days, back_days = (sorted({date.fromisoformat(r[0]) for r in conn.execute(
            "SELECT DISTINCT substr(departure_datetime, 1, 10) FROM avelo_flights WHERE origin = ? AND destination = ?",
            route)}) for route in (("PHL", "MCO"), ("MCO", "PHL")))
    assert out_days and back_days

    def frontier(origin, destination, days, price):
        return [Flight(flight_id=f"F9CAL-{origin}-{destination}", origin=origin, destination=destination,
                       departure_datetime=datetime(d.year, d.month, d.day), price=price, base_price=price,
                       airline="F9") for d in days]

    on_sale, sold = out_days[::2], out_days[1::2]                    # Frontier: half the days sell out, half go on sale
    assert sold and on_sale
    database.upsert_flights(frontier("PHL", "MCO", on_sale, 45.0) + frontier("MCO", "PHL", back_days, 45.0))
    avelo = database.start_run("calendar", [("PHL", "MCO")])         # Avelo's newest run raised nothing
    database.finish_run(avelo, {})
    first = database.start_run("calendar", [("PHL", "MCO")], airline="F9")
    database.record_seen(first, "PHL", "MCO", out_days, frontier("PHL", "MCO", out_days, 90.0))
    signals.run_detection(first)
    database.finish_run(first, {})
    second = database.start_run("calendar", [("PHL", "MCO")], airline="F9")
    database.record_seen(second, "PHL", "MCO", out_days, frontier("PHL", "MCO", on_sale, 45.0))
    events = signals.run_detection(second)
    database.finish_run(second, {})
    assert {"sold_out", "sale_wave", "floor_fare"} <= {e.kind for e in events} and {e.airline for e in events} == {"F9"}

    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    st.cache_data.clear()
    df = next(d.value for d in at.dataframe if "Add-on Fees" in d.value.columns)
    avelo_only, frontier_only = df[df["Airlines"] == AVELO], df[df["Airlines"] == FRONTIER]
    assert len(avelo_only) and (avelo_only["Signals"] == "").all(), "a Frontier signal badged an Avelo-only package"
    assert len(frontier_only) and frontier_only["Signals"].str.contains("floor").all()   # Frontier's own badge still shows
    assert not frontier_only["Signals"].str.contains("sold out").any()   # no Frontier fare is left on those days


def _save_rule(at, name):
    next(t for t in at.text_input if t.label == "Rule name").set_value(name).run()
    next(b for b in at.button if b.label == "Save rule").click().run()
    assert not at.exception
    (rule,) = database.list_watches("rule")
    return rule["payload"]


def test_saved_rule_carries_the_partner_window_and_airlines(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(c for c in at.checkbox if c.label == "Only when we're both free").check().run()
    assert not at.exception
    payload = _save_rule(at, "Both free")
    assert set(payload["partner"]) == {"start_weekday", "start_time", "end_weekday", "end_time"}
    assert payload["airlines"] == sorted(config.KNOWN_AIRLINES) and payload["count_carry_on"] is False
    import alerts
    assert alerts.rule_query(payload) is not None                   # the stored window parses back into a query


def test_saved_rule_without_a_partner_has_none(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    payload = _save_rule(at, "Just me")
    assert payload["partner"] is None and payload["airlines"] == sorted(config.KNOWN_AIRLINES)


def add_frontier_copy(member_expr: str = "price - 41") -> None:
    """Frontier fares on the same dates as the Avelo mock schedule, from PHL, $40 cheaper."""
    with database.connection() as conn:
        conn.execute(f"""
            INSERT INTO avelo_flights (flight_id, origin, destination, departure_datetime, arrival_datetime, price, currency,
                duration_minutes, flight_number, base_price, member_price, is_member_fare, promo_code, airline, timestamp_scraped)
            SELECT 'F9-' || flight_id, CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END,
                   CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END, departure_datetime, arrival_datetime,
                   price - 40, currency, duration_minutes, flight_number, price - 40, {member_expr}, 0, promo_code, 'F9', timestamp_scraped
            FROM avelo_flights WHERE (origin = 'ILG' OR destination = 'ILG') AND arrival_datetime IS NOT NULL
        """)


def test_dashboard_shows_frontier_packages_with_their_airline(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if "Airlines" in df.value.columns]
    assert tables and (tables[0]["Airlines"] == FRONTIER).any()             # each airline behind a dot in its colour
    assert set(tables[0]["Airlines"]) <= {AVELO, FRONTIER, "🟣 Avelo out · 🟢 Frontier back", "🟢 Frontier out · 🟣 Avelo back"}
    frontier_rows = tables[0][tables[0]["Airlines"] == FRONTIER]
    assert (frontier_rows["Program would save"] > 0).all()
    assert "Program would save" in _packages_table(at).proto.column_order   # there is a saving to show
    assert any("airline" in df.value.columns and (df.value["airline"] == "F9").any() for df in at.dataframe)   # freshness table


def test_default_screen_matches_avelo_only_data(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    assert next(m for m in at.multiselect if m.label == "Home airports").value == list(config.HOME_CODES)
    assert next(m for m in at.multiselect if m.label == "Airlines").value == sorted(config.KNOWN_AIRLINES)
    assert at.metric[2].value != "0"                                    # packages still found
    dest = next(m for m in at.multiselect if m.label == "Airports")
    assert set(config.DESTINATIONS) <= set(_codes(dest)) and "DEN" in _codes(dest)     # Frontier airports offered too
    assert "DEN · Denver · Frontier" in dest.options and "TPA · Tampa · Avelo, Frontier" in dest.options
    assert any(c.label == "Return to a different home airport" and c.value is False for c in at.checkbox)
    assert any(c.label == "Count a carry-on bag" and c.value is False for c in at.checkbox)


def test_narrowing_airlines_and_homes_prunes_destinations_without_errors(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP"]).run()
    assert not at.exception, at.exception
    dest = next(m for m in at.multiselect if m.label == "Airports")
    assert "DEN" not in _codes(dest) and "DEN" not in dest.value and "TPA" in dest.value
    next(m for m in at.multiselect if m.label == "Airlines").set_value([]).run()
    assert not at.exception, at.exception
    assert any("Pick at least one airline" in w.value for w in at.warning)
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP", "F9"]).run()
    assert "TPA" in _airports(at).value            # an empty airline selection on the way did not wipe the destinations
    next(m for m in at.multiselect if m.label == "Home airports").set_value([]).run()
    assert not at.exception and any("Pick at least one home airport" in w.value for w in at.warning)
    assert any("Pick at least one home airport and one airline" in i.value for i in at.info)   # main page says why it is empty
    assert not any("Pick at least one destination" in i.value for i in at.info)


def test_saved_rule_carries_the_sidebar_selection(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Home airports").set_value(["ILG"]).run()
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP"]).run()
    next(c for c in at.checkbox if c.label == "Count a carry-on bag").set_value(True).run()
    next(t for t in at.text_input if t.label == "Rule name").set_value("Avelo from ILG").run()
    next(b for b in at.button if b.label == "Save rule").click().run()
    assert not at.exception, at.exception
    payload = database.list_watches("rule")[0]["payload"]
    assert (payload["origins"], payload["airlines"], payload["count_carry_on"], payload["allow_open_jaw"]) == (["ILG"], ["XP"], True, False)


def _pick(at, label, value):
    next(m for m in at.multiselect if m.label == label).set_value(value).run()
    assert not at.exception, at.exception


def test_passing_through_a_selection_that_serves_nothing_keeps_the_destinations(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    before = (_airports(at).value, at.metric[2].value)
    _pick(at, "Airlines", [])                       # swapping the only airline goes through "none"
    _pick(at, "Airlines", ["F9", "XP"])
    assert (_airports(at).value, at.metric[2].value) == before
    assert not any("Pick at least one destination" in i.value for i in at.info)

    _pick(at, "Airports", ["TPA"])                  # a narrowed selection survives it too
    _pick(at, "Airlines", [])
    _pick(at, "Airlines", ["XP", "F9"])
    assert _airports(at).value == ["TPA"]

    _pick(at, "Home airports", ["ILG"])
    _pick(at, "Airlines", ["F9"])                   # Frontier does not fly from ILG: nothing is offered
    assert any("No destinations for this choice" in c.value for c in at.sidebar.caption)
    _pick(at, "Airlines", ["XP"])
    assert _airports(at).value == ["TPA"]
    _pick(at, "Home airports", [])                  # and through "no home airport"
    _pick(at, "Home airports", ["ILG"])
    assert _airports(at).value == ["TPA"]


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """Commands the page tried to start; no scrape, browser or real log file is touched."""
    import subprocess
    commands = []

    def fake_popen(cmd, **_kw):
        commands.append(cmd)
        return _FinishedProc(0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(config, "SCRAPE_LOG", tmp_path / "scrape.log")
    return commands


def test_this_weekend_keeps_the_sidebar_choices_and_scrapes_the_offered_destinations(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG", "PHL"])
    _pick(at, "Airlines", ["XP"])
    next(c for c in at.checkbox if c.label == "Count a carry-on bag").check().run()
    next(c for c in at.checkbox if c.label == "Return to a different home airport").check().run()
    next(s for s in at.slider if s.label.startswith("Max total getaway cost")).set_value(250).run()
    offered = _codes(_airports(at))
    next(b for b in at.button if "This weekend" in b.label).click().run()
    assert not at.exception, at.exception
    assert next(m for m in at.multiselect if m.label == "Home airports").value == ["ILG", "PHL"]
    assert next(m for m in at.multiselect if m.label == "Airlines").value == ["XP"]
    assert next(c for c in at.checkbox if c.label == "Count a carry-on bag").value is True
    assert next(c for c in at.checkbox if c.label == "Return to a different home airport").value is True
    assert next(s for s in at.slider if s.label.startswith("Max total getaway cost")).value == 250
    assert _airports(at).value == offered and "DEN" not in offered
    assert len(launched) == 1
    cmd = launched[0]
    assert cmd[cmd.index("--destinations") + 1] == ",".join(offered)
    today = date.today()
    assert cmd[cmd.index("--start") + 1] == today.isoformat()
    assert at.session_state["span"][0] == today


def test_this_weekend_with_nothing_offered_launches_no_scrape(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG"])
    _pick(at, "Airlines", ["F9"])                   # Frontier does not fly from ILG: nothing is offered
    weekend = next(b for b in at.button if "This weekend" in b.label)
    assert weekend.disabled and next(b for b in at.button if "Execute Target Scrape" in b.label).disabled
    # the button cannot be clicked now; a scrape queued with no destinations must still not start
    at.session_state["pending_scrape"] = {"destinations": [], "start": date.today(), "end": date.today()}
    at.run()                                        # an empty --destinations would mean every route to run_scraper
    assert not at.exception, at.exception
    assert launched == [] and "pending_scrape" not in at.session_state


def test_catalog_refreshed_by_another_process_reaches_the_destination_list(mock_db):
    import os
    import time
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert "DEN" in _codes(_airports(at))
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], source="route_page")   # what the nightly job does
    stamp = time.time() + 5
    os.utime(mock_db, (stamp, stamp))               # a later write, whatever the clock's resolution
    at.run()
    assert not at.exception, at.exception
    offered = _codes(_airports(at))
    assert "DEN" not in offered and "MCO" in offered


def test_settings_panel_changes_membership_and_home_costs(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    den = next(c for c in at.checkbox if c.label == "Discount Den member")
    assert den.value is False and next(c for c in at.checkbox if c.label == "Avelo PLUS member").value is True
    den.set_value(True).run()
    next(n for n in at.number_input if n.key == "parking_PHL").set_value(22.0).run()
    next(n for n in at.number_input if n.key == "drive_PHL").set_value(35).run()
    next(b for b in at.button if b.key == "save_settings").click().run()
    assert not at.exception, at.exception
    assert database.memberships() == {"XP": True, "F9": True}
    assert database.home_costs()["PHL"] == config.HomeCost(35.0, 22.0)
    assert database.home_costs()["ILG"].drive_min is None                 # 0 minutes means "not set"
    assert database.list_events(notify_only=True) == []                   # no alert from the membership change


def _heatmap_cells(at):
    import json
    cells = []
    for chart in at.get("plotly_chart"):
        spec = json.loads(chart.proto.spec) if chart.proto.spec else {}
        cells += [tr.get("z") for tr in spec.get("data", []) if tr.get("type") == "heatmap"]
    return cells


def test_saving_a_membership_change_reprices_everything_the_page_shows(mock_db):
    from datetime import datetime
    import trips
    from models import Flight
    with database.connection() as c:
        row = c.execute("SELECT airline, origin, destination, departure_datetime FROM avelo_flights "
                        "WHERE airline = 'XP' AND member_price < base_price LIMIT 1").fetchone()
    assert row is not None
    leg = trips.trip_key(Flight(airline=row["airline"], origin=row["origin"], destination=row["destination"],
                                departure_datetime=datetime.fromisoformat(row["departure_datetime"]), price=1))
    watch = database.add_watch("package", "mock package", {"out_trip": leg, "in_trip": leg})
    database.update_watch_state(watch, {"last_total": 150.0, "alert_base": 150.0})
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    before = _heatmap_cells(at)
    coverage = lambda: next(c.value for c in at.caption if "member fares" in c.value)
    assert before and "are member fares" in coverage() and "· 0 are" not in coverage()
    next(c for c in at.checkbox if c.key == "member_XP").set_value(False).run()
    next(b for b in at.button if b.key == "save_settings").click().run()
    assert not at.exception, at.exception
    with database.connection() as c:
        xp = [dict(r) for r in c.execute("SELECT price, base_price, is_member_fare FROM avelo_flights WHERE airline = 'XP'")]
    assert xp and all(r["price"] == r["base_price"] and not r["is_member_fare"] for r in xp)   # stored fares repriced
    at.run()
    assert _heatmap_cells(at) != before                                   # the price calendar shows the new prices
    assert "· 0 are member fares" in coverage()                # and so does the coverage line
    state = database.list_watches("package")[0]["state"]
    assert "last_total" not in state and "alert_base" not in state        # price alerts restart from the new basis
    assert database.list_events(notify_only=True) == []


def test_scrape_button_launches_the_selection(mock_db, launched):
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Home airports").set_value(["ILG", "PHL"]).run()
    next(m for m in at.multiselect if m.label == "Airports").set_value(["TPA", "MCO"]).run()
    assert any("Avelo 8" in c.value and "Frontier 2" in c.value for c in at.sidebar.caption)
    next(b for b in at.button if "Execute Target Scrape" in b.label).click().run()
    assert not at.exception, at.exception
    cmd = launched[0]
    assert cmd[cmd.index("--origins") + 1] == "ILG,PHL" and cmd[cmd.index("--airlines") + 1] == "F9,XP"
    assert cmd[cmd.index("--destinations") + 1] == "TPA,MCO"


def test_scrape_caption_calls_an_unfetched_frontier_route_list_an_estimate(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()     # no catalog rows: Frontier's list is not fetched
    captions = [c.value for c in at.sidebar.caption if c.value.startswith("Target:")]
    assert len(captions) == 1
    assert "Frontier up to" in captions[0] and "route list not fetched yet" in captions[0]
    assert "Avelo up to" not in captions[0]
    next(b for b in at.button if "Execute Target Scrape" in b.label).click().run()
    assert not at.exception, at.exception
    cmd = launched[0]
    assert cmd[cmd.index("--airlines") + 1] == "F9,XP"


def test_this_weekend_scrape_uses_the_homes_and_airlines_selected_when_clicked(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG", "PHL"])
    _pick(at, "Airlines", ["XP"])
    next(b for b in at.button if "This weekend" in b.label).click().run()
    assert not at.exception, at.exception
    cmd = launched[0]
    assert cmd[cmd.index("--origins") + 1] == "ILG,PHL" and cmd[cmd.index("--airlines") + 1] == "XP"


def _target_caption(at):
    (caption,) = [c.value for c in at.sidebar.caption if c.value.startswith("Target:")]
    return caption


def test_scrape_caption_says_a_plain_zero_when_frontier_serves_none_of_the_homes(mock_db):
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")    # fetched, but not for ILG
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG"])
    _pick(at, "Airports", ["TPA", "MCO"])
    caption = _target_caption(at)
    assert "Frontier 0" in caption and "up to" not in caption and "not fetched" not in caption


def test_scrape_caption_calls_a_partial_frontier_catalog_a_minimum(mock_db):
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")    # BWI's page was never read
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["PHL", "BWI"])
    _pick(at, "Airports", ["MCO"])
    assert "Frontier at least 2 (route list refreshes first)" in _target_caption(at)


def test_mock_source_launches_and_words_only_what_the_mock_run_covers(mock_db, launched):
    database.upsert_catalog("F9", [(h, d) for h in ("PHL", "BWI") for d in ("MCO", "TPA") for h, d in ((h, d), (d, h))], "route-page")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG", "PHL"])
    _pick(at, "Airports", ["TPA", "MCO"])
    next(r for r in at.radio if r.label == "Source").set_value("Mock data").run()
    caption = _target_caption(at)
    assert "Frontier" not in caption and "Target: 4 routes" in caption and "Avelo 4" in caption and "ILG only" in caption
    next(b for b in at.button if "Execute Target Scrape" in b.label).click().run()
    assert not at.exception, at.exception
    cmd = launched[0]
    assert "--mock" in cmd and cmd[cmd.index("--airlines") + 1] == "XP" and cmd[cmd.index("--origins") + 1] == "ILG"
    import json
    import run_scraper
    assert run_scraper.main(cmd[2:] + ["--no-alerts"]) == 0           # the command the screen launched, run for real on tmp_path
    with database.connection() as c:
        runs = [(r["airline"], json.loads(r["routes"])) for r in c.execute("SELECT airline, routes FROM scrape_runs")]
    assert runs == [("XP", ["ILG-TPA", "TPA-ILG", "ILG-MCO", "MCO-ILG"])]    # 4 routes, as the caption said, all Avelo


def test_mock_source_without_the_primary_home_cannot_launch(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["PHL"])
    next(r for r in at.radio if r.label == "Source").set_value("Mock data").run()
    assert next(b for b in at.button if "Execute Target Scrape" in b.label).disabled
    assert any("Mock data only flies" in c.value for c in at.sidebar.caption)
    assert launched == []


def test_this_weekend_under_mock_data_launches_the_mock_selection(mock_db, launched):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Home airports", ["ILG", "PHL"])
    next(r for r in at.radio if r.label == "Source").set_value("Mock data").run()
    next(b for b in at.button if "This weekend" in b.label).click().run()
    assert not at.exception, at.exception
    cmd = launched[0]
    assert "--mock" in cmd and cmd[cmd.index("--airlines") + 1] == "XP" and cmd[cmd.index("--origins") + 1] == "ILG"


def test_airline_vs_airline_tab_compares_the_same_weekend(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if {"Avelo", "Frontier", "Cheaper"} <= set(df.value.columns)]
    assert tables and len(tables[0]) > 0
    both = tables[0].dropna(subset=["Avelo", "Frontier"])
    assert len(both) > 0 and (both["Saves"] >= 0).all()
    assert set(both["Cheaper"]) <= {"Avelo", "Frontier"}
    # every Frontier fare here came from a day search: nothing is marked, and the column is not drawn
    table = next(df for df in at.dataframe if "Cheaper" in df.value.columns)
    assert (tables[0]["Unverified"] == "").all() and "Unverified" not in table.proto.column_order
    caption = next(c.value for c in at.caption if c.value.startswith("Cost per package"))
    assert "Unverified" not in caption and "carry-on" not in caption


def test_airline_vs_airline_tab_explains_single_airline_data(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert any("Only one airline has packages" in i.value for i in at.info)


def test_airline_vs_airline_tab_does_not_say_scrape_when_the_airline_is_only_deselected(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Airlines", ["XP"])
    msgs = [i.value for i in at.info if "Only one airline" in i.value]
    assert msgs, "the tab should still explain why there is nothing to compare"
    assert not any("Scrape the other airline" in m for m in msgs)
    assert any("Select both" in m for m in msgs)


def test_airline_vs_airline_tab_budget_hint_mentions_filters(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(s for s in at.slider if s.label == "Max total getaway cost ($)").set_value(50).run()
    assert not at.exception, at.exception
    msgs = [i.value for i in at.info if "Only one airline" in i.value]
    assert msgs and all("raise the budget" in m for m in msgs)


def test_ntfy_topic_never_appears_on_screen(mock_db, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "super-secret-topic-123")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception
    next(t for t in at.text_input if t.label == "Rule name").set_value("Florida").run()
    next(b for b in at.button if b.label == "Save rule").click().run()        # the rule panel is where the topic used to show
    assert not at.exception
    rendered = _rendered(at)
    assert "super-secret-topic-123" not in rendered and "Push alerts are on" in rendered
    # the scan really covers the whole page: a table cell, a widget label, a chart, a sidebar caption, a help text
    for seen in ("Total Getaway Cost", "Rule name", "Lowest fare", "Target:", "Alert me when its total drops"):
        assert seen in rendered, seen


def test_rendered_scan_sees_every_kind_of_element():
    """The privacy test above is only as good as `_rendered`: plant a marker in element kinds the old scan skipped."""
    script = '''
import pandas as pd
import streamlit as st
st.code("in-code MARK1")
st.dataframe(pd.DataFrame({"col": ["in-table MARK2"]}))
st.sidebar.text_input("in-label MARK3", help="in-help MARK4")
with st.expander("in-expander MARK5"):
    st.json({"k": "in-json MARK6"})
st.error("in-error MARK7")
st.selectbox("pick", ["in-option MARK8"])
with st.sidebar.popover("pop"):
    st.metric("m", "in-metric MARK9")
'''
    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception, at.exception
    rendered = _rendered(at)
    assert [m for m in (f"MARK{i}" for i in range(1, 10)) if m not in rendered] == []


def test_discount_den_meter_appears_when_frontier_packages_are_listed(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert any("Discount Den would save" in m.value for m in at.markdown)
    database.set_membership("F9", True)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not any("Discount Den would save" in m.value for m in at.markdown)      # already a member


def _meter_pills(at) -> list[str]:
    import re
    return [re.sub(r"<[^>]+>", "", m.value) for m in at.markdown if "border-radius:999px" in m.value]


def test_meter_does_not_claim_zero_savings_when_the_program_fare_is_unknown(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy(member_expr="NULL")           # Frontier fares scraped without a Discount Den price
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if b.key == "save_booking").click().run()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert database.list_bookings()
    pills = _meter_pills(at)
    assert not any("$0 on" in p for p in pills), pills


def test_meter_does_not_claim_zero_savings_for_a_former_member(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if b.key == "save_booking").click().run()        # booked at the member fare
    next(c for c in at.checkbox if c.key == "member_XP").set_value(False).run()
    next(b for b in at.button if b.key == "save_settings").click().run()
    at.run()
    meter = [p for p in _meter_pills(at) if "Avelo PLUS would save" in p]
    assert not any("$0 on" in p for p in meter), meter


def test_map_and_calendar_render_with_two_airlines(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    database.upsert_catalog("F9", [("PHL", "TPA"), ("TPA", "PHL")], "route-page")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    assert any("Frontier" in m.value and "Avelo" in m.value and "home airport" in m.value for m in at.markdown)   # map legend
    rows = _heatmap_rows(at)
    assert "Avelo ILG→TPA" in rows and "Frontier PHL→TPA" in rows                    # fare calendar: one row per airline and route
    assert rows == sorted(rows)                                                      # grouped by airline


def _frontier_calendar_only(price: float = 30.0) -> None:
    """Frontier as the nightly job stores it: one calendar placeholder per day (no time), PHL⇄MCO, on Avelo's ILG⇄MCO days."""
    from datetime import datetime
    from models import Flight
    with database.connection() as conn:
        days = {route: sorted({date.fromisoformat(r[0]) for r in conn.execute(
            "SELECT DISTINCT substr(departure_datetime, 1, 10) FROM avelo_flights WHERE origin = ? AND destination = ?", route)})
            for route in (("ILG", "MCO"), ("MCO", "ILG"))}
    assert all(days.values())
    database.upsert_flights([
        Flight(flight_id=f"F9CAL-{o}-{d}", origin=o, destination=d, departure_datetime=datetime(day.year, day.month, day.day),
               price=price, base_price=None, member_price=price, airline="F9")
        for (o, d), key in ((("PHL", "MCO"), ("ILG", "MCO")), (("MCO", "PHL"), ("MCO", "ILG"))) for day in days[key]])


def test_airline_vs_airline_tab_marks_frontier_calendar_prices_as_unverified(mock_db, monkeypatch):
    """The nightly job stores Frontier's calendar price (lowest Discount Den total of any routing). The tab must not
    present it as Frontier's cost, or name Frontier the cheaper airline from it, without saying so."""
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    _frontier_calendar_only()
    database.set_home_cost("PHL", None, 0.0)          # no parking: the $30 calendar legs make Frontier look cheaper
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    _pick(at, "Airports", ["MCO"])
    table = next(df for df in at.dataframe if "Cheaper" in df.value.columns)
    rows = table.value.dropna(subset=["Frontier"])
    assert len(rows) > 0 and (rows["Unverified"] == "Frontier").all()
    assert "Unverified" in table.proto.column_order
    won = rows[rows["Cheaper"].str.startswith("Frontier")]
    assert len(won) > 0 and (won["Cheaper"] == "Frontier (unverified)").all()      # the verdict carries the mark too
    assert (table.value[table.value["Frontier"].isna()]["Unverified"] == "").all()  # Avelo's calendar fares are bookable
    caption = next(c.value for c in at.caption if c.value.startswith("Cost per package"))
    assert "Unverified" in caption and "calendar price" in caption and "Discount Den" in caption and "Frontier" in caption
    assert any("Frontier rows" in c.value and "Discount Den" in c.value for c in at.caption)   # the fare calendar says it too
    assert "carry-on" not in caption

    next(c for c in at.checkbox if c.label == "Count a carry-on bag").check().run()   # the figures now include bag fees
    assert not at.exception, at.exception
    caption = next(c.value for c in at.caption if c.value.startswith("Cost per package"))
    assert "carry-on bag on each flight" in caption and f"Frontier ${config.CARRY_ON_FEE['F9']:.0f}" in caption
    assert "Unverified" in caption


def test_airlines_filter_reaches_the_coverage_line_freshness_table_and_calendar(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    coverage = lambda: next(c.value for c in at.caption if "for this selection" in c.value)
    freshness = lambda: next(d.value for d in at.dataframe if "airline" in d.value.columns)
    span, dests = at.session_state["span"], _airports(at).value
    count = lambda codes: database.selection_stats(["ILG", "PHL"], dests, span[0], span[1], airlines=codes)["flights"]
    assert count(["XP"]) > 0 and count(["F9"]) > 0
    assert coverage().startswith(f"{count(['XP']) + count(['F9']):,} fares") and set(freshness()["airline"]) == {"XP", "F9"}
    assert {r.split(" ")[0] for r in _heatmap_rows(at)} == {"Avelo", "Frontier"}

    _pick(at, "Airlines", ["XP"])
    assert _airports(at).value == dests                                   # every default destination is an Avelo one
    assert coverage().startswith(f"{count(['XP']):,} fares")              # Frontier's fares are not "this selection"
    assert set(freshness()["airline"]) == {"XP"}
    assert {r.split(" ")[0] for r in _heatmap_rows(at)} == {"Avelo"}


def test_deselected_airlines_fares_do_not_hide_the_scrape_hint(mock_db):
    """Avelo-only data, Airlines = Frontier: nothing is stored for the selection, and the page must say so."""
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    _pick(at, "Airlines", ["F9"])
    assert _airports(at).value, "Frontier's fallback list shares destinations with the default selection"
    assert at.metric[2].value == "0"
    assert not any("for this selection" in c.value for c in at.caption)
    assert any("No fares stored yet on Frontier" in w.value and "Execute Target Scrape" in w.value for w in at.warning)
    assert _heatmap_rows(at) == [] and any("No fares in this span" in i.value for i in at.info)
    assert not any("airline" in d.value.columns for d in at.dataframe)    # freshness: "No routes scraped yet for this selection."


def test_scrape_by_another_process_reaches_every_cached_reader(mock_db):
    """The nightly job writes while the dashboard is open: coverage, freshness, calendar and signal badges must follow
    (their loaders are keyed on the database's version; an underscore parameter name would leave it out of the key)."""
    import os
    import time
    from signals import Event

    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    coverage = lambda: next(c.value for c in at.caption if "for this selection" in c.value)
    freshness = lambda: next(d.value for d in at.dataframe if "airline" in d.value.columns)
    before = (coverage(), _heatmap_cells(at), int(freshness()["Fares"].sum()))
    assert (_packages_table(at).value["Signals"] == "").all()

    with database.connection() as conn:                                # what another process's scrape does
        first = conn.execute("SELECT origin, destination, substr(departure_datetime, 1, 10) AS day FROM avelo_flights "
                             "WHERE origin = 'ILG' AND departure_datetime >= ? ORDER BY departure_datetime LIMIT 1",
                             (at.session_state["span"][0].isoformat(),)).fetchone()    # a day inside the search span
        conn.execute("UPDATE avelo_flights SET price = 1.0")
        conn.execute("DELETE FROM avelo_flights WHERE origin = ? AND destination = ? AND substr(departure_datetime, 1, 10) = ?",
                     (first["origin"], first["destination"], first["day"]))
        days = [r[0] for r in conn.execute("SELECT DISTINCT substr(departure_datetime, 1, 10) FROM avelo_flights")]
    run = database.start_run("calendar", [("ILG", "TPA")])
    database.add_events(run, [Event("sale_wave", o, d, date.fromisoformat(day), "sale", airline="XP")
                              for day in days for o, d in (("ILG", "TPA"), ("TPA", "ILG"))])
    database.finish_run(run, {})
    stamp = time.time() + 5
    os.utime(mock_db, (stamp, stamp))               # a later write, whatever the clock's resolution
    at.run()
    assert not at.exception, at.exception
    assert coverage() != before[0]                                      # one day's fares are gone
    cells = _heatmap_cells(at)
    assert cells != before[1] and {v for grid in cells for row in grid for v in row if v is not None} == {1.0}
    assert int(freshness()["Fares"].sum()) < before[2]
    packages = _packages_table(at).value
    tampa = packages[(packages["Out Route"] == "ILG→TPA") & (packages["Back Route"] == "TPA→ILG")]
    assert len(tampa) > 0 and tampa["Signals"].str.contains("sale").all()   # the badge map was re-read


def add_frontier_calendar_placeholders(price: float = 15.0) -> None:
    """Frontier calendar rows (day known, no time, Discount Den price only) on the Avelo mock dates, from PHL, very cheap."""
    with database.connection() as conn:
        conn.execute(f"""
            INSERT OR IGNORE INTO avelo_flights (flight_id, origin, destination, departure_datetime, arrival_datetime, price, currency,
                duration_minutes, flight_number, base_price, member_price, is_member_fare, promo_code, airline, timestamp_scraped)
            SELECT DISTINCT 'F9CAL-' || CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END || '-' || CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END,
                   CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END, CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END,
                   substr(departure_datetime, 1, 10) || 'T00:00', NULL, {price}, 'USD', NULL, NULL, NULL, {price}, 0, NULL, 'F9', timestamp_scraped
            FROM avelo_flights WHERE origin = 'ILG' OR destination = 'ILG'
        """)


def test_unverified_frontier_prices_are_marked_wherever_a_package_price_is_shown(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_calendar_placeholders()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    cheapest = next(m for m in at.metric if m.label == "Cheapest getaway")
    assert "unverified Frontier calendar price" in cheapest.delta                       # the headline number
    inspect = next(sb for sb in at.selectbox if sb.label == "Inspect a package")
    assert "unverified" in inspect.options[0]                                           # the package inspector
    season = [df.value for df in at.dataframe if {"Cost", "Fit", "Sun"} <= set(df.value.columns)]
    assert season and "Unverified" in season[0].columns and (season[0]["Unverified"] == "Frontier").any()   # the season plan
    next(b for b in at.button if "Wildcard weekend" in b.label).click().run()
    assert not at.exception, at.exception
    picks = [s.value for s in at.success if "all-in" in s.value]
    assert picks                                                                        # a pick is shown…
    tables = [df.value for df in at.dataframe if "Airlines" in df.value.columns]
    assert tables                                                                       # …and the page still renders


def test_no_unverified_marks_on_avelo_only_data(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert "unverified" not in next(m for m in at.metric if m.label == "Cheapest getaway").delta
    season = [df.value for df in at.dataframe if {"Cost", "Fit", "Sun"} <= set(df.value.columns)]
    assert season and "Unverified" not in season[0].columns
