import json
from datetime import date, datetime
from pathlib import Path

import pytest

import frontier
from browser import ScrapeError

FX = Path(__file__).parent / "fixtures" / "frontier"


def load(name):
    return json.loads((FX / name).read_text())


def test_select_keeps_only_nonstops_with_both_fares():
    payload = load("select_MCO_PHL_2026-11-15.json")          # 25 flights, 5 nonstop
    flights = frontier.parse_select(payload, "MCO", "PHL", date(2026, 11, 15))
    assert len(flights) == 5 and frontier.select_has_flights(payload)
    first = min(flights, key=lambda f: f.departure_datetime)
    assert first.departure_datetime == datetime(2026, 11, 15, 6, 55) and first.arrival_datetime == datetime(2026, 11, 15, 9, 30)
    assert (first.airline, first.flight_number, first.flight_id) == ("F9", "2506", "F9-2506-MCO-PHL")
    assert first.duration_minutes == 155 and first.time_known
    assert (first.price, first.base_price, first.member_price, first.is_member_fare) == (203.98, 203.98, 202.98, False)
    member = frontier.parse_select(payload, "MCO", "PHL", date(2026, 11, 15), member=True)
    assert (min(member, key=lambda f: f.departure_datetime).price, member[0].is_member_fare) == (202.98, True)
    assert len({f.flight_id for f in flights}) == 5           # one row per flight number


def test_select_handles_next_day_arrival_and_single_nonstop():
    (f,) = frontier.parse_select(load("select_BWI_TPA_2026-11-13.json"), "BWI", "TPA", date(2026, 11, 13))
    assert f.departure_datetime == datetime(2026, 11, 13, 21, 59) and f.arrival_datetime == datetime(2026, 11, 14, 0, 34)
    assert (f.base_price, f.member_price) == (71.98, 70.98)


def test_select_rejects_other_routes_days_and_empty_payloads():
    payload = load("select_PHL_MCO_2026-11-12.json")
    assert frontier.parse_select(payload, "PHL", "TPA", date(2026, 11, 12)) == []        # wrong route
    assert frontier.parse_select(payload, "PHL", "MCO", date(2026, 11, 13)) == []        # wrong day
    assert frontier.parse_select({"journeys": []}, "PHL", "MCO", date(2026, 11, 12)) == []
    assert frontier.parse_select({"journeys": [{"flights": None}]}, "PHL", "MCO", date(2026, 11, 12)) == []
    assert not frontier.select_has_flights({"journeys": [{"flights": []}]})
    connecting_only = {"journeys": [{"flights": [f for f in payload["journeys"][0]["flights"] if f["stopCount"] > 0]}]}
    assert frontier.parse_select(connecting_only, "PHL", "MCO", date(2026, 11, 12)) == []
    assert frontier.select_has_flights(connecting_only)                                   # flights, but none nonstop


def test_calendar_gives_discount_den_placeholders_and_answered_days():
    flights, answered = frontier.parse_calendar(load("lowfare_calendar_PHL_MCO_2026-11.json"), "PHL", "MCO")
    assert len(answered) == 30 and answered[0] == date(2026, 11, 1) and answered[-1] == date(2026, 11, 30)
    nov1 = next(f for f in flights if f.departure_date == date(2026, 11, 1))
    assert (nov1.price, nov1.member_price, nov1.base_price) == (155.96, 155.96, None)   # 79.16 + 76.80 taxes
    assert (nov1.flight_id, nov1.airline, nov1.time_known, nov1.is_member_fare) == ("F9CAL-PHL-MCO", "F9", False, False)
    assert all(f.departure_datetime.hour == 0 for f in flights)


def test_calendar_that_is_not_an_answer_covers_nothing():
    with pytest.raises(ScrapeError):
        frontier.parse_calendar({"success": False}, "PHL", "MCO")
    with pytest.raises(ScrapeError):
        frontier.parse_calendar({"success": True, "calendarSections": []}, "PHL", "MCO")
    payload = load("lowfare_calendar_PHL_MCO_2026-11.json")
    for week in payload["calendarSections"][0]["weeks"]:
        for day in week["days"]:
            day["isUnavailable"] = True
    assert frontier.parse_calendar(payload, "PHL", "MCO") == ([], [])    # nothing priced: not "everything sold out"


def test_calendar_for_another_route_is_rejected():
    payload = load("lowfare_calendar_PHL_MCO_2026-11.json")
    with pytest.raises(ScrapeError):
        frontier.parse_calendar(payload, "BWI", "SFO")                # a late reply for the previous route
    with pytest.raises(ScrapeError):
        frontier.parse_calendar(payload, "MCO", "PHL")                # reversed
    del payload["calendarSections"][0]["departingStation"]
    with pytest.raises(ScrapeError):
        frontier.parse_calendar(payload, "PHL", "MCO")                # cannot prove which route it answers
    ok = load("lowfare_calendar_PHL_MCO_2026-11.json")
    assert frontier.parse_calendar(ok, " phl", "mco ")[1]             # same route, any case/whitespace


def test_calendar_unavailable_day_is_answered_without_a_fare():
    payload = load("lowfare_calendar_PHL_MCO_2026-11.json")
    day3 = next(d for w in payload["calendarSections"][0]["weeks"] for d in w["days"] if d["dateFormatted"] == "2026-11-03")
    day3["isUnavailable"] = True
    flights, answered = frontier.parse_calendar(payload, "PHL", "MCO")
    assert date(2026, 11, 3) in answered and date(2026, 11, 3) not in {f.departure_date for f in flights}


def test_route_pages_list_destinations_in_app_codes():
    phl = frontier.parse_routes((FX / "routes_from_PHL.html").read_text(), "PHL")
    assert {"MCO", "TPA", "DEN", "SJU", "PBI"} <= phl and "DJT" not in phl and "PHL" not in phl and len(phl) == 20
    bwi = frontier.parse_routes((FX / "routes_from_BWI.html").read_text(), "bwi")
    assert {"MCO", "DEN", "SFO"} <= bwi and len(bwi) == 20
    assert frontier.parse_routes("<html>maintenance</html>", "PHL") == set()
    assert frontier.parse_routes((FX / "routes_from_BWI.html").read_text(), "PHL") == set()   # another airport's page


def test_challenge_pages_are_recognised():
    assert frontier.is_challenge_page('<div id="px-captcha"></div><p>Press &amp; Hold to confirm you are a human</p>')
    assert frontier.is_challenge_page("<h1>Access Denied</h1>")
    assert not frontier.is_challenge_page("<html><title>Select | Frontier Airlines</title></html>")


def test_probe_days_picks_the_cheapest_distinct_days():
    flights, _ = frontier.parse_calendar(load("lowfare_calendar_PHL_MCO_2026-11.json"), "PHL", "MCO")
    days = frontier.probe_days(flights, 3)
    cheapest = sorted(flights, key=lambda f: (f.price, f.departure_date))[:3]
    assert days == sorted(f.departure_date for f in cheapest) and len(set(days)) == 3
    assert frontier.probe_days([], 3) == []
