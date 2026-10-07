from datetime import date, datetime

import pytest

import database as db
import persist
from models import Flight


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def cal(day, price, month=10):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, month, day), price=price,
                  flight_id="LOWFARE-ILG-TPA")


def detail(day, hour, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour), price=price,
                  duration_minutes=150, flight_id="AVELO-ILG-TPA")


def days_stored(conn):
    return sorted(f.departure_datetime for f in db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 11, 30), conn=conn))


def test_calendar_removes_days_that_lost_their_fare(conn):
    run1 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    persist.save_calendar(run1, "ILG", "TPA", [date(2026, 10, 9), date(2026, 10, 12)], [cal(9, 79), cal(12, 89)], conn)
    run2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    persist.save_calendar(run2, "ILG", "TPA", [date(2026, 10, 9), date(2026, 10, 12)], [cal(9, 79)], conn)
    assert days_stored(conn) == [datetime(2026, 10, 9)]
    assert db.seen_in_run(run2, "ILG", "TPA", conn) == {date(2026, 10, 9): 79, date(2026, 10, 12): None}


def test_calendar_leaves_uncovered_months_alone(conn):
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 9), date(2026, 11, 3)], [cal(9, 79), cal(3, 99, month=11)], conn)
    run = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    # November failed to render: only October days are covered this time
    persist.save_calendar(run, "ILG", "TPA", [date(2026, 10, 9)], [cal(9, 75)], conn)
    assert days_stored(conn) == [datetime(2026, 10, 9), datetime(2026, 11, 3)]
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 9): 75}


def test_day_search_replaces_every_row_for_that_day(conn):
    db.upsert_flights([cal(10, 70), detail(10, 10, 80)], conn)
    run = db.start_run("detail", [("ILG", "TPA")], conn=conn)
    written = persist.save_day(run, "ILG", "TPA", date(2026, 10, 10), [detail(10, 11, 85)], conn)
    assert written == 1
    assert days_stored(conn) == [datetime(2026, 10, 10, 11)]
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 10): 85}


def test_sold_out_day_search_clears_the_day_and_records_none(conn):
    db.upsert_flights([detail(10, 10, 80)], conn)
    run = db.start_run("detail", [("ILG", "TPA")], conn=conn)
    assert persist.save_day(run, "ILG", "TPA", date(2026, 10, 10), [], conn) == 0
    assert days_stored(conn) == []
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 10): None}


def f9_timed(day, hour, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour), price=price,
                  duration_minutes=150, airline="F9", flight_number="77", flight_id="F9-77-ILG-TPA")


def f9_cal(day, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day), price=price,
                  airline="F9", flight_id="F9CAL-ILG-TPA")


def airlines_on(conn, day):
    return sorted((f.airline, f.flight_id) for f in db.query_flights(["ILG"], ["TPA"], date(2026, 10, day), date(2026, 10, day), conn=conn))


def test_an_avelo_search_never_removes_frontier_fares(conn):
    db.upsert_flights([f9_timed(10, 9, 60), f9_cal(12, 55), detail(10, 10, 80), cal(12, 70)], conn)
    persist.save_day(None, "ILG", "TPA", date(2026, 10, 10), [], conn)                       # Avelo: sold out
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 12)], [], conn)                # Avelo: day lost its fare
    persist.clear_unscheduled("ILG", "TPA", [date(2026, 10, 10), date(2026, 10, 12)], conn)  # Avelo schedule dropped them
    assert airlines_on(conn, 10) == [("F9", "F9-77-ILG-TPA")]
    assert airlines_on(conn, 12) == [("F9", "F9CAL-ILG-TPA")]


def test_a_frontier_search_never_removes_avelo_fares(conn):
    db.upsert_flights([f9_timed(10, 9, 60), detail(10, 10, 80), cal(12, 70)], conn)
    persist.save_day(None, "ILG", "TPA", date(2026, 10, 10), [], conn, airline="F9")
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 12)], [], conn, airline="F9")
    assert airlines_on(conn, 10) == [("XP", "AVELO-ILG-TPA")]
    assert airlines_on(conn, 12) == [("XP", "LOWFARE-ILG-TPA")]


def test_placeholders_are_shadowed_only_by_their_own_airline(conn):
    db.upsert_flights([f9_cal(10, 55), detail(10, 10, 80)], conn)        # Avelo detail row, Frontier placeholder
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 10)], [f9_cal(10, 50)], conn, airline="F9")
    assert airlines_on(conn, 10) == [("F9", "F9CAL-ILG-TPA"), ("XP", "AVELO-ILG-TPA")]
    db.upsert_flights([f9_timed(10, 9, 60)], conn)
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 10)], [f9_cal(10, 50)], conn, airline="F9")
    assert airlines_on(conn, 10) == [("F9", "F9-77-ILG-TPA"), ("XP", "AVELO-ILG-TPA")]   # F9 placeholder shadowed
