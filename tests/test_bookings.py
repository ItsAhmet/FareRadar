from datetime import date, datetime

import pytest

import bookings
import config
import database as db
from models import Flight
from optimizer import GetawayQuery, Window, build_itineraries


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def f(origin, dest, day, hour, price, base):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour), price=price, base_price=base,
                  is_member_fare=True, duration_minutes=150, flight_id=f"AVELO-{origin}-{dest}")


def package(conn):
    out, back = f("ILG", "TPA", 8, 8, 74, 114), f("TPA", "ILG", 11, 15, 61, 101)
    db.upsert_flights([out, back], conn)
    return build_itineraries([out], [back], GetawayQuery({"TPA"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))]))[0]


def test_booking_payload_and_roi(conn, monkeypatch):
    monkeypatch.setattr(config, "PLUS_ANNUAL_COST", 99.0)
    monkeypatch.setattr(config, "PLUS_VALUE_PER_LEG", 50.0)
    it = package(conn)
    payload = bookings.booking_payload(it)
    assert bookings.fare_savings(payload) == 80  # (114-74) + (101-61)
    assert bookings.plus_roi(conn)["bookings"] == 0
    db.add_booking("Tampa", payload, 135, conn=conn)
    roi = bookings.plus_roi(conn)
    assert roi == {"bookings": 1, "fare_savings": 80, "perk_value": 100, "total_value": 180, "annual_cost": 99, "paid_off": True, "ratio": 1.82}


def test_booked_trip_tracks_market_and_alerts_once_per_drop(conn):
    it = package(conn)
    bid = db.add_booking("Tampa", bookings.booking_payload(it), 135, conn=conn)

    assert bookings.evaluate_bookings(conn) == []
    state = db.list_bookings(conn)[0]["state"]
    assert state["current_total"] == 135 and state["delta"] == 0

    db.upsert_flights([f("ILG", "TPA", 8, 8, 120, 160)], conn)  # fares rose: you beat the market
    assert bookings.evaluate_bookings(conn) == []
    assert db.list_bookings(conn)[0]["state"]["delta"] == 46

    db.upsert_flights([f("ILG", "TPA", 8, 8, 50, 90)], conn)  # now $111, $24 below what you paid
    events = bookings.evaluate_bookings(conn)
    assert [e.kind for e in events] == ["booked_drop"] and "$24 cheaper" in events[0].message
    assert bookings.evaluate_bookings(conn) == []  # same level: no repeat
    db.upsert_flights([f("ILG", "TPA", 8, 8, 30, 90)], conn)  # dropped further
    assert [e.kind for e in bookings.evaluate_bookings(conn)] == ["booked_drop"]

    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    assert bookings.evaluate_bookings(conn) == []
    assert db.list_bookings(conn)[0]["state"]["current_total"] is None
    assert db.remove_booking(bid, conn) == 1


def test_booking_follows_its_trip_through_a_detail_upgrade(conn):
    out = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 8), price=100, flight_id="LOWFARE-ILG-TPA")
    back = Flight(origin="TPA", destination="ILG", departure_datetime=datetime(2026, 10, 11), price=80, flight_id="LOWFARE-TPA-ILG")
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))], origins=["ILG"]))
    db.add_booking("trip", bookings.booking_payload(it), 180.0, conn=conn)
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.upsert_flights([Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 8, 19), price=60,
                              duration_minutes=150, flight_id="AVELO-ILG-TPA")], conn)
    events = bookings.evaluate_bookings(conn)
    b = db.list_bookings(conn)[0]
    assert b["state"]["current_total"] == 140.0
    assert b["payload"]["out_trip"] == "XP|ILG|TPA|2026-10-08|19:00"
    assert [e.kind for e in events] == ["booked_drop"]
