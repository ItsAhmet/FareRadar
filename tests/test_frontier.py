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


# --------------------------------------------------------------------------- #
# Driver (Task 7): the route loop with scripted page methods, and the catalog refresh
# --------------------------------------------------------------------------- #

import asyncio

import config
import database as db
from browser import BlockedError, ScrapeReport
from models import Flight

TODAY = date(2026, 11, 1)
WANTED = {date(2026, 11, d) for d in range(10, 16)}          # Nov 10-15


@pytest.fixture
def f9db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "f.db")
    db.init_db()
    return tmp_path


def cal_flight(day, price):
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(2026, 11, day), price=price,
                  member_price=price, flight_id="F9CAL-PHL-MCO")


def timed_flight(day, hour=8, price=90.0):
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(2026, 11, day, hour), price=price,
                  base_price=price, member_price=price - 1, duration_minutes=166, flight_number="2415", flight_id="F9-2415-PHL-MCO")


def scraper_with(monkeypatch, mode, calendar=None, days=None):
    """A FrontierScraper whose two page methods are scripted: `calendar` = (flights, answered) or an exception;
    `days` maps a date to a list of flights or an exception (missing date → no nonstop)."""
    s = frontier.FrontierScraper(mode=mode, screenshots=False, max_retries=0,
                                 run_id=db.start_run(mode, [("PHL", "MCO")], airline="F9"))
    s.calls = []

    async def fake_calendar(origin, dest, anchor):
        s.calls.append(("cal", anchor))
        if isinstance(calendar, Exception):
            raise calendar
        return calendar

    async def fake_day(origin, dest, day):
        s.calls.append(("day", day))
        result = (days or {}).get(day, [])
        if isinstance(result, Exception):
            raise result
        return result

    async def no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(s, "scrape_calendar_month", fake_calendar)
    monkeypatch.setattr(s, "scrape_day", fake_day)
    monkeypatch.setattr(s, "_polite_delay", no_wait)
    monkeypatch.setattr(s, "_save_state", no_wait)
    monkeypatch.setattr(s, "_snapshot", no_wait)
    return s


def run_routes(s, routes=(("PHL", "MCO"),)):
    report = ScrapeReport()
    asyncio.run(s._run_routes(list(routes), set(WANTED), TODAY, report))
    return report


def stored():
    return sorted((f.flight_id, f.departure_datetime) for f in db.query_flights(["PHL"], ["MCO"], date(2026, 11, 1), date(2026, 11, 30), airlines=["F9"]))


CAL = ([cal_flight(10, 80), cal_flight(11, 60), cal_flight(12, 70), cal_flight(14, 95)], [date(2026, 11, d) for d in range(1, 31)])


def test_unknown_route_is_probed_then_gets_its_placeholders(f9db, monkeypatch):
    s = scraper_with(monkeypatch, "calendar", calendar=CAL, days={date(2026, 11, 11): [timed_flight(11)]})
    report = run_routes(s)
    assert [c for c in s.calls if c[0] == "day"] == [("day", date(2026, 11, d)) for d in (10, 11, 12)]   # 3 cheapest days
    assert db.route_nonstop("F9", "PHL", "MCO") is True and report.failed == 0
    assert ("F9-2415-PHL-MCO", datetime(2026, 11, 11, 8)) in stored()
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 14)) in stored()                                        # placeholder kept
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 11)) not in stored()                                    # shadowed by the timed row
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 10)) not in stored()                                    # searched: no nonstop that day
    assert db.seen_in_run(s.run_id, "PHL", "MCO")[date(2026, 11, 10)] is None


def test_connecting_only_route_stores_nothing_and_is_skipped_next_time(f9db, monkeypatch):
    s = scraper_with(monkeypatch, "calendar", calendar=CAL, days={})          # every probed day: flights, none nonstop
    run_routes(s)
    assert stored() == [] and db.route_nonstop("F9", "PHL", "MCO") is False
    again = scraper_with(monkeypatch, "calendar", calendar=CAL)
    report = run_routes(again)
    assert again.calls == [] and report.attempted == 0


def test_confirmed_route_in_calendar_mode_needs_no_day_searches(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    s = scraper_with(monkeypatch, "calendar", calendar=CAL)
    run_routes(s)
    assert [c[0] for c in s.calls] == ["cal"]
    assert [d for _id, d in stored()] == [datetime(2026, 11, d) for d in (10, 11, 12, 14)]
    seen = db.seen_in_run(s.run_id, "PHL", "MCO")
    assert seen[date(2026, 11, 13)] is None and seen[date(2026, 11, 10)] == 80 and date(2026, 11, 20) not in seen


def test_full_mode_searches_only_the_priced_days(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    s = scraper_with(monkeypatch, "full", calendar=CAL, days={date(2026, 11, 10): [timed_flight(10)]})
    run_routes(s)
    assert [c[1].day for c in s.calls if c[0] == "day"] == [10, 11, 12, 14]


def test_block_keeps_stored_fares_and_stops_the_airline(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    db.upsert_flights([timed_flight(10), timed_flight(11)])
    s = scraper_with(monkeypatch, "detail", days={d: BlockedError("challenge") for d in WANTED})
    report = run_routes(s, routes=[("PHL", "MCO"), ("MCO", "PHL")])
    assert report.blocked and report.failed == 2 and report.succeeded == 0
    assert len([c for c in s.calls if c[0] == "day"]) == 2                    # two in a row, then stop
    assert len(stored()) == 2 and db.seen_in_run(s.run_id, "PHL", "MCO") == {}


def test_failed_calendar_month_covers_nothing(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    db.upsert_flights([cal_flight(13, 99)])
    s = scraper_with(monkeypatch, "calendar", calendar=frontier.ScrapeError("calendar response missing"))
    report = run_routes(s)
    assert report.failed == 1 and ("F9CAL-PHL-MCO", datetime(2026, 11, 13)) in stored()
    empty = scraper_with(monkeypatch, "calendar", calendar=([], []))            # answered nothing
    run_routes(empty)
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 13)) in stored() and db.seen_in_run(empty.run_id, "PHL", "MCO") == {}


def test_bad_results_pages_on_an_unknown_route_decide_nothing(f9db, monkeypatch):
    db.upsert_flights([timed_flight(10), cal_flight(14, 95)])                  # stored by an earlier run
    bad = frontier.ScrapeError("results page lists no flights at all on a day the calendar priced")
    s = scraper_with(monkeypatch, "calendar", calendar=CAL, days={date(2026, 11, d): bad for d in (10, 11, 12)})
    report = run_routes(s)
    assert (report.attempted, report.succeeded, report.failed) == (4, 1, 3)    # the calendar worked, the 3 probes did not
    assert db.route_nonstop("F9", "PHL", "MCO") is None and not report.blocked  # still unknown: probed again next run
    assert stored() == [("F9-2415-PHL-MCO", datetime(2026, 11, 10, 8)), ("F9CAL-PHL-MCO", datetime(2026, 11, 14))]
    assert db.seen_in_run(s.run_id, "PHL", "MCO") == {}


class FakeResultsPage:
    """Stands in for the Playwright page on Frontier's results view: `payload` is window.FlightDataJson
    (None = it never appears) and `html` is what the page shows instead."""

    def __init__(self, payload, html="<html><title>Select | Frontier Airlines</title></html>"):
        self.payload, self.html, self.urls = payload, html, []

    async def goto(self, url, **_kwargs):
        self.urls.append(url)

    async def wait_for_function(self, _expression):
        if self.payload is None:
            raise frontier.PlaywrightTimeout("Timeout 30000ms exceeded")

    async def evaluate(self, _expression):
        return self.payload

    async def content(self):
        return self.html


def day_search(page, day=date(2026, 11, 12)):
    s = frontier.FrontierScraper(member=False, screenshots=False, max_retries=0)
    s._page = page
    return asyncio.run(s.scrape_day("PHL", "MCO", day))


def test_scrape_day_tells_no_nonstop_from_a_page_without_flights():
    payload = load("select_PHL_MCO_2026-11-12.json")
    page = FakeResultsPage(payload)
    flights = day_search(page)
    assert flights and all(f.flight_id.startswith("F9-") and f.departure_date == date(2026, 11, 12) for f in flights)
    assert "o1=PHL&d1=MCO" in page.urls[0] and "c=true" not in page.urls[0]
    connecting_only = {"journeys": [{"flights": [f for f in payload["journeys"][0]["flights"] if f["stopCount"] > 0]}]}
    assert day_search(FakeResultsPage(connecting_only)) == []                  # an answer: no nonstop that day
    with pytest.raises(ScrapeError, match="no flights at all"):
        day_search(FakeResultsPage({"journeys": [{"flights": []}]}))           # not an answer: a bad page


def test_scrape_day_without_flight_data_is_a_block_or_a_layout_change():
    with pytest.raises(BlockedError):
        day_search(FakeResultsPage(None, html='<div id="px-captcha"></div>'))
    with pytest.raises(ScrapeError, match="FlightDataJson") as caught:
        day_search(FakeResultsPage(None))
    assert not isinstance(caught.value, BlockedError)


def test_refresh_catalog_writes_both_directions_and_survives_failures(f9db, monkeypatch):
    pages = {"philadelphia": (FX / "routes_from_PHL.html").read_text(), "baltimore": "<html>maintenance</html>"}

    def fetch(url):
        return pages[url.rsplit("flights-from-", 1)[1]]

    now = datetime(2026, 10, 7, 9)
    assert frontier.refresh_catalog(["PHL", "BWI", "ILG"], now=now, fetch=fetch) == 40          # 20 destinations × 2 directions
    assert ("MCO", "PHL") in db.catalog_routes("F9", now=now) and db.catalog_routes("F9", origins=["BWI"], now=now) == []
    assert frontier.refresh_catalog(["PHL"], now=now, fetch=lambda url: 1 / 0) == 0             # fresh: not fetched at all

    def boom(url):
        raise OSError("offline")

    later = datetime(2026, 10, 20, 9)
    assert frontier.refresh_catalog(["PHL"], now=later, fetch=boom) == 0                        # stale + failure: keep what we have
    assert ("PHL", "MCO") in db.catalog_routes("F9", now=later)
    monkeypatch.setattr(config, "OFFLINE", True)
    assert frontier.refresh_catalog(["BWI"], now=later) == 0                                    # offline, no injected fetch
