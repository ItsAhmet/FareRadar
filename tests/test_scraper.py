"""Parser tests against HTML/JSON captured from the live Avelo site (Sep 2026)."""
import json
from datetime import date, datetime
from pathlib import Path

from scraper import _duration_minutes, _money, parse_lowfare_calendar, parse_schedule, parse_select_page, plan_targets

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_schedule_maps_route_pairs_to_dates():
    schedule = parse_schedule(json.loads((FIXTURES / "routes_all.json").read_text()))
    assert ("ILG", "SRQ") in schedule and ("SRQ", "ILG") in schedule
    assert date(2026, 11, 19) in schedule[("ILG", "SRQ")]
    assert ("ILG", "PGD") not in schedule


def test_parse_schedule_tolerates_garbage():
    assert parse_schedule([]) == {}
    assert parse_schedule([{"Code": "X", "Locations": {"Items": [{"Code": "Y", "Days": {"Items": [{"Code": "bad"}]}}]}}]) == {}


def test_lowfare_calendar_records_avelo_plus_member_fare():
    html = (FIXTURES / "lowfare_page.html").read_text()
    flights = parse_lowfare_calendar(html, "HVN", "TPA", scraped_at=datetime(2026, 9, 2, 12))
    by_day = {f.departure_date: f for f in flights}

    assert len(flights) == 24
    assert date(2026, 9, 8) not in by_day  # Tuesday: no flight
    f = by_day[date(2026, 9, 10)]
    assert f.price == 74.0  # Avelo PLUS
    assert f.base_price == 114.0  # public fare kept for comparison
    assert f.is_member_fare is True
    assert f.savings == 40.0
    assert f.flight_id == "LOWFARE-HVN-TPA"
    assert f.departure_datetime == datetime(2026, 9, 10, 0, 0)
    assert f.time_known is False


def test_select_page_reads_times_duration_and_member_fare():
    html = (FIXTURES / "select_page.html").read_text()
    flights = parse_select_page(html, "HVN", "TPA", date(2026, 9, 16))
    assert len(flights) == 1
    f = flights[0]
    assert f.departure_datetime == datetime(2026, 9, 16, 11, 0)
    assert f.arrival_datetime == datetime(2026, 9, 16, 13, 59)
    assert f.duration_minutes == 179
    assert f.price == 74.0 and f.base_price == 114.0 and f.is_member_fare
    assert f.flight_id == "AVELO-HVN-TPA"
    assert f.flight_number is None
    assert f.time_known


def test_public_fare_used_when_no_member_fare_shown():
    html = '<td class="day active selectable" aria-label="October, 03 2026"><div class="price standard">$97</div></td>'
    (f,) = parse_lowfare_calendar(html, "ILG", "TPA")
    assert f.price == 97 and f.base_price == 97 and f.is_member_fare is False


def test_parse_select_page_on_unrelated_html_returns_nothing():
    assert parse_select_page("<html><body>Nothing here</body></html>", "ILG", "TPA", date(2026, 9, 16)) == []


def test_helpers():
    assert _money("From $1,114.50") == 1114.5
    assert _money("no price") is None
    assert _duration_minutes("2h 59m Nonstop") == 179
    assert _duration_minutes(None) is None


def test_plan_targets_only_visits_requested_routes_and_dates():
    schedule = {
        ("ILG", "TPA"): {date(2026, 10, 2), date(2026, 10, 4), date(2026, 10, 30)},
        ("TPA", "ILG"): {date(2026, 10, 4)},
        ("ILG", "CHS"): {date(2026, 10, 3)},  # scheduled but NOT requested
        ("ILG", "SJU"): {date(2026, 11, 1)},  # requested but outside the dates
    }
    routes = [("ILG", "TPA"), ("TPA", "ILG"), ("ILG", "SJU"), ("ILG", "PGD")]
    dates = [date(2026, 10, 1) + __import__("datetime").timedelta(days=i) for i in range(7)]

    targets, skipped = plan_targets(routes, dates, schedule)

    visited = {(t.origin, t.destination): t.days for t in targets}
    assert visited == {("ILG", "TPA"): (date(2026, 10, 2), date(2026, 10, 4)), ("TPA", "ILG"): (date(2026, 10, 4),)}
    assert skipped == ["ILG-SJU", "ILG-PGD"]
    assert ("ILG", "CHS") not in visited
