from datetime import date, datetime

import pytest

import trips
from models import Flight


def cal(day=8):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day), price=79,
                  flight_id="LOWFARE-ILG-TPA")


def timed(day=8, hour=8, minute=5):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour, minute), price=79,
                  duration_minutes=150, flight_id="AVELO-ILG-TPA")


def test_flight_airline_defaults_to_avelo_and_normalises():
    assert cal().airline == "XP"
    assert Flight(origin="phl", destination="mco", departure_datetime=datetime(2026, 11, 12), price=1, airline="f9").airline == "F9"


def test_trip_and_day_keys():
    assert trips.trip_key(cal()) == "XP|ILG|TPA|2026-10-08|*"
    assert trips.trip_key(timed()) == "XP|ILG|TPA|2026-10-08|08:05"
    assert trips.day_key(timed()) == trips.day_key(cal()) == "XP|ILG|TPA|2026-10-08"


def test_parse_round_trips_and_rejects_garbage():
    k = trips.parse("XP|ILG|TPA|2026-10-08|08:05")
    assert (k.airline, k.origin, k.destination, k.day, k.time) == ("XP", "ILG", "TPA", date(2026, 10, 8), "08:05")
    assert str(k) == "XP|ILG|TPA|2026-10-08|08:05" and k.day_key == "XP|ILG|TPA|2026-10-08"
    with pytest.raises(ValueError):
        trips.parse("nonsense")


def test_matches_treats_unknown_time_as_wildcard():
    assert trips.matches("XP|ILG|TPA|2026-10-08|*", "XP|ILG|TPA|2026-10-08|08:05")
    assert trips.matches("XP|ILG|TPA|2026-10-08|08:05", "XP|ILG|TPA|2026-10-08|08:05")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|08:05", "XP|ILG|TPA|2026-10-08|09:00")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|*", "F9|ILG|TPA|2026-10-08|*")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|*", "XP|ILG|TPA|2026-10-09|*")


def test_legacy_payloads_convert_to_trip_keys():
    assert trips.legacy_key("LOWFARE-ILG-TPA", "2026-10-08T00:00") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.legacy_key("AVELO-TPA-ILG", "2026-10-11T15:30") == "XP|TPA|ILG|2026-10-11|15:30"
    assert trips.legacy_key("T-ILG-TPA-8", "2026-10-08T08:00") == "XP|ILG|TPA|2026-10-08|08:00"
    assert trips.legacy_key("AVELO-ILG-TPA", "2026-10-08T00:00") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.legacy_key("garbage", "2026-10-08T08:00") is None
    old = {"out_id": "LOWFARE-ILG-TPA", "out_dep": "2026-10-08T00:00", "in_id": "AVELO-TPA-ILG", "in_dep": "2026-10-11T15:30"}
    assert trips.payload_trip(old, "out") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.payload_trip({**old, "in_trip": "XP|TPA|ILG|2026-10-11|16:00"}, "in") == "XP|TPA|ILG|2026-10-11|16:00"


def test_upgrade_pins_a_wildcard_key_to_the_found_flight():
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|*", timed()) == "XP|ILG|TPA|2026-10-08|08:05"
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|*", cal()) == "XP|ILG|TPA|2026-10-08|*"
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|07:00", timed()) == "XP|ILG|TPA|2026-10-08|07:00"
