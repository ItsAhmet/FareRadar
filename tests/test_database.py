import sqlite3
from datetime import date, datetime

import pytest

import config
import database as db
from models import Flight


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def flight(origin="ILG", dest="TPA", day=10, hour=9, price=99.0, number="XP101", base=None):
    return Flight(
        origin=origin,
        destination=dest,
        departure_datetime=datetime(2026, 10, day, hour, 0),
        price=price,
        base_price=base,
        is_member_fare=base is not None,
        duration_minutes=180,
        flight_number=number,
    )


def test_upsert_refreshes_price_for_same_flight(conn):
    db.upsert_flights([flight(price=120)], conn)
    db.upsert_flights([flight(price=89)], conn)
    rows = db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert len(rows) == 1
    assert rows[0].price == 89


def test_member_fare_fields_round_trip(conn):
    db.upsert_flights([flight(price=74, base=114)], conn)
    f = db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)[0]
    assert f.price == 74 and f.base_price == 114 and f.is_member_fare is True
    assert f.savings == 40


def test_same_flight_number_on_different_days_are_distinct(conn):
    db.upsert_flights([flight(day=10), flight(day=12)], conn)
    assert len(db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)) == 2


def test_query_filters_by_route_date_and_price(conn):
    db.upsert_flights(
        [
            flight(day=10, price=50),
            flight(day=20, price=60),
            flight(dest="LAL", day=11, price=70, number="XP202"),
            flight(day=12, price=300, number="XP303"),
            flight(origin="TPA", dest="ILG", day=12, price=40, number="XP404"),
        ],
        conn,
    )
    rows = db.query_flights(["ilg"], ["TPA", "LAL"], date(2026, 10, 9), date(2026, 10, 15), max_price=100, conn=conn)
    assert sorted(f.price for f in rows) == [50, 70]
    assert rows[0].price == 50


def test_end_date_is_inclusive(conn):
    db.upsert_flights([flight(day=15, hour=23)], conn)
    assert len(db.query_flights(["ILG"], ["TPA"], date(2026, 10, 15), date(2026, 10, 15), conn=conn)) == 1


def test_route_calendar_returns_min_price_per_day(conn):
    db.upsert_flights([flight(day=10, hour=7, price=80, number="XP1"), flight(day=10, hour=18, price=60, number="XP2")], conn)
    cal = db.route_calendar(date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert cal == [{"origin": "ILG", "destination": "TPA", "day": "2026-10-10", "min_price": 60, "flights": 2}]


def test_purge_departed_removes_only_past(conn):
    db.upsert_flights([flight(day=1), flight(day=20)], conn)
    assert db.purge_departed(conn, now=datetime(2026, 10, 5)) == 1
    assert db.stats(conn)["flights"] == 1


def test_delete_day_with_prefix_only_removes_placeholders(conn):
    placeholder = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10, 0, 0), price=99, flight_id="LOWFARE-ILG-TPA")
    other_day = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 11, 0, 0), price=99, flight_id="LOWFARE-ILG-TPA")
    db.upsert_flights([placeholder, flight(day=10, hour=11), other_day], conn)
    assert db.delete_day("ILG", "TPA", date(2026, 10, 10), flight_id_prefix="LOWFARE", conn=conn) == 1
    ids = {f.flight_id for f in db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)}
    assert ids == {"XP101-ILG-TPA", "LOWFARE-ILG-TPA"}


def test_selection_stats_counts_both_directions(conn):
    db.upsert_flights([flight(day=10, base=140), flight(origin="TPA", dest="ILG", day=12, number="XP9"), flight(dest="CHS", day=10, number="XP8")], conn)
    s = db.selection_stats("ILG", ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert s["flights"] == 2 and s["member_fares"] == 1 and s["routes"] == 2
    assert db.selection_stats("ILG", [], date(2026, 10, 1), date(2026, 10, 31), conn=conn)["flights"] == 0


def test_migration_adds_member_fare_columns_to_old_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE avelo_flights (
            flight_id TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            departure_datetime TEXT NOT NULL, arrival_datetime TEXT, price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD', duration_minutes INTEGER, flight_number TEXT,
            timestamp_scraped TEXT NOT NULL, PRIMARY KEY (flight_id, departure_datetime));
        INSERT INTO avelo_flights VALUES ('LOWFARE-ILG-TPA','ILG','TPA','2026-10-10T00:00',NULL,114,'USD',NULL,NULL,'2026-09-01T00:00:00');
    """)
    old.commit(); old.close()

    conn = db.get_connection(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(avelo_flights)")}
    assert {"base_price", "is_member_fare"} <= cols
    f = db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)[0]
    assert f.is_member_fare is False and f.base_price is None
    db.upsert_flights([flight(price=74, base=114)], conn)  # writes work after migration
    conn.close()


def test_upsert_appends_price_history(conn):
    a = flight(price=120)
    b = Flight(origin="ILG", destination="TPA", departure_datetime=a.departure_datetime, price=89, flight_number="XP101",
               timestamp_scraped=a.timestamp_scraped + __import__("datetime").timedelta(hours=6))
    db.upsert_flights([a], conn)
    db.upsert_flights([b], conn)
    hist = db.price_history(a.flight_id, a.departure_datetime, conn=conn)
    assert [h["price"] for h in hist] == [120, 89]
    prev = db.previous_prices([(a.flight_id, a.departure_datetime)], conn=conn)
    assert prev == {(a.flight_id, "2026-10-10T09:00"): 120}


def test_route_medians_and_freshness(conn):
    db.upsert_flights([flight(day=10, price=50), flight(day=12, price=90, number="XP2"), flight(day=14, price=70, number="XP3"),
                       flight(origin="TPA", dest="ILG", day=13, price=40, number="XP4")], conn)
    med = db.route_medians("ILG", ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert med == {("ILG", "TPA"): 70, ("TPA", "ILG"): 40}
    fresh = db.route_freshness("ILG", ["TPA"], conn=conn)
    assert [(r["origin"], r["destination"], r["fares"]) for r in fresh] == [("ILG", "TPA", 3), ("TPA", "ILG", 1)]


def test_maintenance_purges_departed_and_prunes_history(conn):
    db.upsert_flights([flight(day=1), flight(day=20)], conn)
    result = db.maintenance(conn, now=datetime(2027, 3, 1))  # well past both departures + retention
    assert result == {"departed_flights_removed": 2, "history_rows_pruned": 2}
    assert db.stats(conn)["flights"] == 0


def test_drop_shadowed_placeholders(conn):
    detail = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10, 11, 0), price=74, flight_id="AVELO-ILG-TPA", duration_minutes=150)
    shadowed = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10, 0, 0), price=74, flight_id="LOWFARE-ILG-TPA")
    kept = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 11, 0, 0), price=80, flight_id="LOWFARE-ILG-TPA")
    db.upsert_flights([detail, shadowed, kept], conn)
    assert db.drop_shadowed_placeholders("ILG", "TPA", conn=conn) == 1
    rows = db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert sorted((f.flight_id, f.departure_datetime.day) for f in rows) == [("AVELO-ILG-TPA", 10), ("LOWFARE-ILG-TPA", 11)]


def test_promo_code_round_trip_and_history_seeding(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE avelo_flights (
            flight_id TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            departure_datetime TEXT NOT NULL, arrival_datetime TEXT, price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD', duration_minutes INTEGER, flight_number TEXT,
            timestamp_scraped TEXT NOT NULL, PRIMARY KEY (flight_id, departure_datetime));
        INSERT INTO avelo_flights VALUES ('LOWFARE-ILG-TPA','ILG','TPA','2026-10-10T00:00',NULL,114,'USD',NULL,NULL,'2026-09-01T00:00:00');
    """)
    old.commit(); old.close()
    conn = db.get_connection(path)
    assert db.price_history("LOWFARE-ILG-TPA", datetime(2026, 10, 10), conn=conn)[0]["price"] == 114  # seeded
    f = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 12), price=60, promo_code="SAVE20", flight_id="LOWFARE-ILG-TPA")
    db.upsert_flights([f], conn)
    assert db.query_flights(["ILG"], ["TPA"], date(2026, 10, 12), date(2026, 10, 12), conn=conn)[0].promo_code == "SAVE20"
    conn.close()


def test_runs_sightings_floors_and_watchlist(conn):
    run = db.start_run("calendar", [("ILG", "TPA")], promo_code="X", conn=conn)
    n = db.record_seen(run, "ILG", "TPA", [date(2026, 10, 10), date(2026, 10, 12)], [flight(day=10, price=50), flight(day=10, hour=18, price=40, number="XP2")], conn)
    assert n == 2
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 10): 40, date(2026, 10, 12): None}
    assert db.routes_in_run(run, conn) == [("ILG", "TPA")]
    assert db.previous_run_for_route(run, "ILG", "TPA", conn) is None
    run2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(run2, "ILG", "TPA", [date(2026, 10, 10)], [], conn)
    assert db.previous_run_for_route(run2, "ILG", "TPA", conn) == run
    db.finish_run(run2, {"flights_written": 3}, conn)
    assert db.last_runs(1, conn)[0]["summary"] == {"flights_written": 3}

    assert db.get_floor("ILG", "TPA", conn) is None
    db.set_floor("ILG", "TPA", 40, conn)
    db.set_floor("ILG", "TPA", 38, conn)
    assert db.get_floor("ILG", "TPA", conn) == 38

    wid = db.add_watch("rule", "r", {"destinations": ["TPA"]}, conn)
    db.update_watch_state(wid, {"matched": ["a"]}, conn)
    (w,) = db.list_watches(conn=conn)
    assert w["payload"] == {"destinations": ["TPA"]} and w["state"] == {"matched": ["a"]}
    assert db.remove_watch(wid, conn) == 1 and db.list_watches(conn=conn) == []

    db.upsert_flights([flight(day=10, price=50)], conn)
    assert db.get_flight("XP101-ILG-TPA", datetime(2026, 10, 10, 9), conn).price == 50
    assert db.get_flight("nope", datetime(2026, 10, 10, 9), conn) is None


def test_route_queries_accept_several_origins(conn):
    db.upsert_flights([flight(day=10, price=80), flight(origin="PHL", day=10, price=60, number="XP7"),
                       flight(origin="TPA", dest="PHL", day=12, price=50, number="XP8")], conn)
    med = db.route_medians(["ILG", "PHL"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert med == {("ILG", "TPA"): 80, ("PHL", "TPA"): 60, ("TPA", "PHL"): 50}
    s = db.selection_stats(["ILG", "PHL"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert s["flights"] == 3 and s["routes"] == 3
    fresh = db.route_freshness(["ILG", "PHL"], ["TPA"], conn=conn)
    assert {(r["origin"], r["destination"]) for r in fresh} == {("ILG", "TPA"), ("PHL", "TPA"), ("TPA", "PHL")}


def test_settings_and_home_costs(conn):
    assert db.get_setting("nope", 7, conn) == 7
    db.set_setting("x", {"a": 1}, conn)
    assert db.get_setting("x", None, conn) == {"a": 1}
    costs = db.home_costs(conn)
    assert costs["PHL"].parking_per_day == 17.0 and costs["PHL"].drive_min is None
    db.set_home_cost("phl", 40, 20.0, conn)
    assert db.home_costs(conn)["PHL"] == config.HomeCost(40, 20.0)
    assert db.home_costs(conn)["ILG"] == config.DEFAULT_HOME_COSTS["ILG"]


def test_find_trip_resolves_day_and_timed_keys(conn):
    placeholder = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10), price=90,
                         flight_id="LOWFARE-ILG-TPA")
    db.upsert_flights([placeholder], conn)
    assert db.find_trip("XP|ILG|TPA|2026-10-10|*", conn).price == 90
    assert db.find_trip("XP|ILG|TPA|2026-10-10|08:00", conn) is None
    # a detail run replaces the placeholder with timed flights
    db.delete_day("ILG", "TPA", date(2026, 10, 10), conn=conn)
    db.upsert_flights([flight(day=10, hour=8, price=95, number="XP1"), flight(day=10, hour=18, price=85, number="XP2")], conn)
    assert db.find_trip("XP|ILG|TPA|2026-10-10|*", conn).departure_datetime.hour == 18   # cheapest that day
    assert db.find_trip("XP|ILG|TPA|2026-10-10|08:00", conn).price == 95
    assert db.find_trip("F9|ILG|TPA|2026-10-10|*", conn) is None
    assert db.find_trip("XP|ILG|TPA|2026-10-11|*", conn) is None


def test_payload_updates(conn):
    wid = db.add_watch("package", "w", {"out_trip": "a"}, conn)
    db.update_watch_payload(wid, {"out_trip": "b"}, conn)
    assert db.list_watches("package", conn)[0]["payload"] == {"out_trip": "b"}
    bid = db.add_booking("b", {"out_trip": "a"}, 100, conn=conn)
    db.update_booking_payload(bid, {"out_trip": "c"}, conn)
    assert db.list_bookings(conn)[0]["payload"] == {"out_trip": "c"}
