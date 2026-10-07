"""Parser tests against HTML/JSON captured from the live Avelo site (Sep 2026)."""
import asyncio
import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

import config
import database as db
import persist
from models import Flight
from scraper import (
    AveloScraper,
    DateUnavailable,
    ScrapeError,
    _duration_minutes,
    _money,
    parse_lowfare_calendar,
    parse_schedule,
    parse_select_page,
    plan_targets,
    select_page_is_sold_out,
)

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


# --------------------------------------------------------------------------- #
# A scheduled day that sold out: Avelo's Select Flights page has no cards, only
# "There are no flights available for the selected date. Please select a
# different date." (screenshots data/screenshots/SRQ-HVN-2026-09-07-*, Sep 2 2026).
# The date picker still accepts such a day, so the answer arrives on the results page.
# --------------------------------------------------------------------------- #

# Hand-built from those screenshots (the page's HTML was not saved): the exact wording matters, the markup does not.
# Replace with a captured page during the live check.
SOLD_OUT_PAGE = (
    "<html><body><div class='select-flights'><h1>Select Flights</h1>"
    "<div class='route-header'><span>Depart: SRQ</span><span>To</span><span>HVN</span><p>Monday, September 07</p></div>"
    "<p class='no-flights'>There are no flights available for the selected date. Please select a different date.</p>"
    "<script>var copy = 'There are no flights available for the selected date';</script>"
    "</div></body></html>"
)


class FakeResultsPage:
    """Just enough of a Playwright page for scrape_day: the results page is `html`.

    Waiting for "a flight card, or the no-flights message" times out when neither is on the page,
    like the real 45 s wait does.
    """

    def __init__(self, html):
        self.html = html
        self.waits = 0

    async def wait_for_url(self, *_a, **_k):
        return None

    async def wait_for_timeout(self, *_a, **_k):
        return None

    async def content(self):
        return self.html

    def locator(self, selector):
        return _FakeLocator(self, [selector])

    def get_by_text(self, pattern):
        return _FakeLocator(self, [pattern])


class _FakeLocator:
    def __init__(self, page, alternatives):
        self.page, self.alternatives = page, alternatives

    def or_(self, other):
        return _FakeLocator(self.page, self.alternatives + other.alternatives)

    @property
    def first(self):
        return self

    async def wait_for(self, **_k):
        self.page.waits += 1
        for alt in self.alternatives:
            if isinstance(alt, str):
                if alt == config.SELECTORS["flight_card"] and "flight-block-wrapper-v2" in self.page.html:
                    return
            elif alt.search(self.page.html):
                return
        raise PlaywrightTimeout("neither a flight card nor the no-flights message appeared")


def _results_scraper(html):
    s = AveloScraper(screenshots=False)

    async def submitted(*_a, **_k):
        return None

    s._submit_search = submitted
    s._page = FakeResultsPage(html)
    return s


def test_select_page_is_sold_out_recognises_the_no_flights_answer():
    assert select_page_is_sold_out(SOLD_OUT_PAGE)
    assert select_page_is_sold_out("<p>THERE ARE NO FLIGHTS  AVAILABLE for the selected date</p>")
    assert not select_page_is_sold_out((FIXTURES / "select_page.html").read_text())  # a page with flight cards
    assert not select_page_is_sold_out("<html><body>Loading…</body></html>")
    # The phrase inside a script is not an answer the user can see.
    assert not select_page_is_sold_out("<body><script>var m='no flights available'</script></body>")


def test_scrape_day_raises_date_unavailable_when_only_the_no_flights_message_renders():
    s = _results_scraper(SOLD_OUT_PAGE)
    with pytest.raises(DateUnavailable, match="no flights"):
        asyncio.run(s.scrape_day("SRQ", "HVN", date(2026, 9, 7)))
    assert s.page.waits == 1  # one wait for "card or message", not a 45 s timeout per attempt


def test_scrape_day_still_returns_the_flight_cards():
    s = _results_scraper((FIXTURES / "select_page.html").read_text())
    flights = asyncio.run(s.scrape_day("HVN", "TPA", date(2026, 9, 16)))
    assert [f.departure_datetime for f in flights] == [datetime(2026, 9, 16, 11, 0)]


def test_scrape_day_cards_win_if_the_message_is_also_on_the_page():
    html = (FIXTURES / "select_page.html").read_text().replace("</body>", "<p>no flights available</p></body>")
    flights = asyncio.run(_results_scraper(html).scrape_day("HVN", "TPA", date(2026, 9, 16)))
    assert len(flights) == 1


def test_scrape_day_other_failures_stay_failures():
    # Neither cards nor message: a timeout, which _retry retries and the run records as a failure (never as sold out).
    with pytest.raises(PlaywrightTimeout):
        asyncio.run(_results_scraper("<html><body>Loading…</body></html>").scrape_day("ILG", "TPA", date(2026, 10, 10)))
    # A card-shaped page whose cards do not parse (selectors drifted) is a ScrapeError but not a sold-out answer.
    drifted = "<button class='flight-block-wrapper-v2'><span>no times here</span></button>"
    with pytest.raises(ScrapeError) as err:
        asyncio.run(_results_scraper(drifted).scrape_day("ILG", "TPA", date(2026, 10, 10)))
    assert not isinstance(err.value, DateUnavailable)


def test_sold_out_answer_clears_the_day_and_records_none():
    """scrape_day's DateUnavailable is what the run loop turns into save_day(..., []): stale fares go, a None sighting stays."""
    conn = db.get_connection(":memory:")
    day = date(2026, 10, 10)
    stale = [
        Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10), price=70, flight_id="LOWFARE-ILG-TPA"),
        Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10, 10), price=80,
               duration_minutes=150, flight_id="AVELO-ILG-TPA"),
    ]
    db.upsert_flights(stale, conn)
    run = db.start_run("detail", [("ILG", "TPA")], conn=conn)

    try:
        flights = asyncio.run(_results_scraper(SOLD_OUT_PAGE).scrape_day("ILG", "TPA", day))
    except DateUnavailable:
        flights = []
    persist.save_day(run, "ILG", "TPA", day, flights, conn)

    assert db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn) == []
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {day: None}
    conn.close()


class FakePickerPage:
    """The date picker: `labels` are the aria-labels of the selectable cells it rendered."""

    def __init__(self, labels):
        self.labels, self.clicked = labels, []

    def locator(self, selector):
        page = self
        wanted = re.search(r"aria-label\$='(.+)'\]$", selector)

        class Loc:
            @property
            def first(self):
                return self

            def filter(self, **_k):
                return self

            async def count(self):
                if wanted:
                    return int(any(label.endswith(wanted.group(1)) for label in page.labels))
                return len(page.labels)

            async def wait_for(self, **_k):
                if not await self.count():
                    raise PlaywrightTimeout("cell never became visible")

            async def click(self):
                page.clicked.append(wanted.group(1) if wanted else "date button")

        return Loc()


def _picker_scraper(labels):
    s = AveloScraper(screenshots=False)
    s._page = FakePickerPage(labels)
    return s


def test_pick_date_clicks_the_selectable_cell():
    s = _picker_scraper(["Monday, October 12, 2026", "Tuesday, October 13, 2026"])
    asyncio.run(s._pick_date(date(2026, 10, 12)))
    assert s.page.clicked == ["October 12, 2026"]


def test_pick_date_missing_cell_in_a_rendered_picker_is_date_unavailable():
    s = _picker_scraper(["Tuesday, October 13, 2026"])
    with pytest.raises(DateUnavailable):
        asyncio.run(s._pick_date(date(2026, 10, 12)))


def test_pick_date_picker_that_never_rendered_is_a_failure_not_a_sold_out_day():
    # No selectable cell anywhere means the picker did not render (slow Blazor), not that this day sold out:
    # it must be retried, and must not clear the day's stored fares.
    s = _picker_scraper([])
    with pytest.raises(ScrapeError) as err:
        asyncio.run(s._pick_date(date(2026, 10, 12)))
    assert not isinstance(err.value, DateUnavailable)


def test_scrape_day_in_a_real_browser(monkeypatch):
    """The "flight card or no-flights message" wait is a Playwright expression a fake page cannot prove.
    Pages are served through page.route, so nothing touches the network."""
    from playwright.async_api import async_playwright

    cards = (FIXTURES / "select_page.html").read_text()
    # The message renders first and the cards 300 ms later: the settle re-check must not call that sold out.
    cards_after_message = SOLD_OUT_PAGE.replace(
        "</body>", f"<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', {json.dumps(cards)}), 300)</script></body>"
    )
    monkeypatch.setattr("scraper.SOLD_OUT_SETTLE_MS", 1_000)

    async def outcome(browser, html):
        context = await browser.new_context()
        context.set_default_timeout(1_000)
        page = await context.new_page()

        async def serve(route):
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("**/*", serve)
        scraper = AveloScraper(screenshots=False, timeout_ms=1_000)
        scraper._page = page

        async def submitted(*_a, **_k):
            await page.goto("https://www.aveloair.com/flight-search/select")

        scraper._submit_search = submitted
        try:
            return await scraper.scrape_day("HVN", "TPA", date(2026, 9, 16))
        except Exception as exc:  # noqa: BLE001 - the exception type is the outcome
            return exc
        finally:
            await context.close()

    async def go():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"chromium is not available: {str(exc).splitlines()[0]}")
            try:
                return [await outcome(browser, html) for html in (cards, SOLD_OUT_PAGE, "<body>Loading…</body>", cards_after_message)]
            finally:
                await browser.close()

    with_cards, sold_out, stuck, late_cards = asyncio.run(go())
    assert len(with_cards) == 1
    assert isinstance(sold_out, DateUnavailable)
    assert isinstance(stuck, PlaywrightTimeout)  # neither cards nor message: a failure to retry, never a sold-out answer
    assert len(late_cards) == 1
