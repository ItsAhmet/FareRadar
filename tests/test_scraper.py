"""Parser tests against HTML/JSON captured from the live Avelo site (Sep 2026)."""
import asyncio
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
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
                if BeautifulSoup(self.page.html, "html.parser").select(alt):  # the real selector, on the real markup
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
    """The date picker: `labels` are the aria-labels of the selectable cells it rendered, `disabled` those of
    the greyed-out cells. `td.selectable[...]` sees only the selectable ones; a bare `td[...]` sees both."""

    def __init__(self, labels, disabled=()):
        self.labels, self.disabled, self.clicked = list(labels), list(disabled), []

    def locator(self, selector):
        page = self
        tests = re.findall(r"\[aria-label([$*^]?)='([^']*)'\]", selector)
        pool = page.labels if selector.startswith("td.selectable") else page.labels + page.disabled
        ops = {"$": str.endswith, "*": str.__contains__, "^": str.startswith, "": str.__eq__}

        def matches():
            return [label for label in pool if all(ops[op](label, value) for op, value in tests)]

        class Loc:
            @property
            def first(self):
                return self

            def filter(self, **_k):
                return self

            async def count(self):
                return len(matches())

            async def wait_for(self, **_k):
                if not await self.count():
                    raise PlaywrightTimeout("cell never became visible")

            async def click(self):
                page.clicked.append(tests[0][1] if tests else "date button")

        return Loc()


def _picker_scraper(labels, disabled=()):
    s = AveloScraper(screenshots=False)
    s._page = FakePickerPage(labels, disabled)
    return s


OCTOBER_PICKER = [f"{date(2026, 10, d):%A}, October {d}, 2026" for d in range(1, 21)]


def test_pick_date_clicks_the_selectable_cell():
    s = _picker_scraper(["Monday, October 12, 2026", "Tuesday, October 13, 2026"])
    asyncio.run(s._pick_date(date(2026, 10, 12)))
    assert s.page.clicked == ["October 12, 2026"]


def test_pick_date_missing_cell_in_a_rendered_picker_is_date_unavailable():
    s = _picker_scraper(["Tuesday, October 13, 2026"])
    with pytest.raises(DateUnavailable):
        asyncio.run(s._pick_date(date(2026, 10, 12)))


def test_pick_date_missing_cell_beside_a_disabled_cell_of_its_month_is_date_unavailable():
    s = _picker_scraper(OCTOBER_PICKER, disabled=["Monday, November 2, 2026", "Tuesday, November 3, 2026"])
    with pytest.raises(DateUnavailable):
        asyncio.run(s._pick_date(date(2026, 11, 4)))


def test_pick_date_in_a_month_the_picker_is_not_showing_is_a_failure_not_a_sold_out_day():
    # The picker opened on October; November 2 is scheduled but simply off screen. Calling that sold out would
    # make the run loop clear the day's stored fares and record a no-fare sighting (a false sold_out event).
    s = _picker_scraper(OCTOBER_PICKER)
    with pytest.raises(ScrapeError, match="not showing November 2026") as err:
        asyncio.run(s._pick_date(date(2026, 11, 2)))
    assert not isinstance(err.value, DateUnavailable)


def test_pick_date_same_month_name_in_another_year_does_not_count_as_shown():
    s = _picker_scraper([f"Friday, November {d}, 2027" for d in (5, 6)])
    with pytest.raises(ScrapeError) as err:
        asyncio.run(s._pick_date(date(2026, 11, 2)))
    assert not isinstance(err.value, DateUnavailable)


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


def test_retry_does_not_retry_a_sold_out_day():
    s = AveloScraper(screenshots=False, max_retries=2)
    calls = []

    async def sold_out():
        calls.append(1)
        raise DateUnavailable("Oct 12 not selectable")

    with pytest.raises(DateUnavailable):
        asyncio.run(s._retry(sold_out, "t"))
    assert len(calls) == 1


def test_retry_still_retries_other_scrape_errors(monkeypatch):
    s = AveloScraper(screenshots=False, max_retries=1)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ScrapeError("selector timeout")
        return "ok"

    async def no_sleep(_):
        return None

    async def no_page(*_a, **_k):
        return None

    monkeypatch.setattr("scraper.asyncio.sleep", no_sleep)
    monkeypatch.setattr(s, "_new_page", no_page)
    monkeypatch.setattr(AveloScraper, "page", property(lambda self: type("P", (), {"context": type("C", (), {"browser": None})()})()))
    assert asyncio.run(s._retry(flaky, "t")) == "ok" and len(calls) == 2


def _month_grid(month_name, cells):
    """A month grid. (day, price) is a selectable cell; (day, None) is a disabled cell with the same aria-label."""
    html = []
    for day, price in cells:
        label = f'aria-label="{month_name}, {day:02d} 2026"'
        if price is None:
            html.append(f'<td class="day disabled" {label}></td>')
        else:
            html.append(f'<td class="day selectable" {label}><div class="price subscription">${price}</div></td>')
    return "<table>" + "".join(html) + "</table>"


class FakeCalendarPage:
    """Just enough of a Playwright page to walk the Low Fare Calendar's month tabs."""

    def __init__(self, months, current):
        self.months, self.current = months, current  # {"10-2026": html}

    async def wait_for_url(self, *_a, **_k):
        return None

    async def wait_for_selector(self, *_a, **_k):
        return None

    async def wait_for_timeout(self, *_a, **_k):
        return None

    async def content(self):
        return self.months[self.current]

    def locator(self, selector):
        page = self
        cells = re.match(r"td\.day\[aria-label\^='(\w+), '\]", selector)
        tab = re.match(r"div\.month\[id='([\d-]+)'\]", selector)

        class Locator:
            async def count(self):
                if cells:
                    return page.months[page.current].count(f'aria-label="{cells.group(1)}, ')
                return int(tab.group(1) in page.months)

            async def click(self):
                page.current = tab.group(1)

        return Locator()


# October prices Oct 9 and has Oct 12 without a fare (sold out). November renders only disabled cells, as when its
# fares arrive after the 400 ms pause or the selectors drift: the cells exist, but nothing parses.
CAL_DAYS = [date(2026, 10, 9), date(2026, 10, 12), date(2026, 11, 5), date(2026, 11, 8)]
CAL_MONTHS = {
    "10-2026": _month_grid("October", [(9, 79), (12, None)]),
    "11-2026": _month_grid("November", [(5, None), (8, None)]),
}


def _calendar_scraper(monkeypatch):
    monkeypatch.setattr("scraper.local_today", lambda: date(2026, 10, 1))  # CAL_DAYS stay in the future
    s = AveloScraper(screenshots=False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(s, "_submit_search", noop)
    monkeypatch.setattr(s, "_polite_delay", noop)
    s._page = FakeCalendarPage(dict(CAL_MONTHS), current="10-2026")
    return s


def test_calendar_month_that_renders_without_fares_is_not_covered(monkeypatch, caplog):
    s = _calendar_scraper(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="scraper"):
        flights, covered = asyncio.run(s.scrape_lowfare_route("ILG", "TPA", CAL_DAYS))
    assert [f.departure_date for f in flights] == [date(2026, 10, 9)]
    assert covered == [date(2026, 10, 9), date(2026, 10, 12)]  # Oct 12 had no fare (sold out); November is not covered
    assert "Nov 2026" in caplog.text and "no fares parsed" in caplog.text


def test_empty_calendar_month_leaves_stored_rows_and_sightings_untouched(monkeypatch):
    conn = db.get_connection(":memory:")
    timed = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 11, 5, 9), price=70,
                   duration_minutes=150, flight_id="AVELO-ILG-TPA")
    db.upsert_flights([timed], conn)
    run1 = db.start_run("detail", [("ILG", "TPA")], conn=conn)
    db.record_seen(run1, "ILG", "TPA", [date(2026, 11, 5)], [timed], conn)

    run2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    flights, covered = asyncio.run(_calendar_scraper(monkeypatch).scrape_lowfare_route("ILG", "TPA", CAL_DAYS))
    persist.save_calendar(run2, "ILG", "TPA", covered, flights, conn)

    assert db.find_trip("XP|ILG|TPA|2026-11-05|09:00", conn).price == 70  # a pinned timed fare survives the empty month
    assert db.seen_in_run(run1, "ILG", "TPA", conn) == {date(2026, 11, 5): 70}
    assert db.seen_in_run(run2, "ILG", "TPA", conn) == {date(2026, 10, 9): 79, date(2026, 10, 12): None}  # no Nov 5 sold-out row
    conn.close()


# --------------------------------------------------------------------------- #
# AveloScraper.run itself: the wiring between the scrape and persist
# --------------------------------------------------------------------------- #


class _FakePlaywright:
    """`async with async_playwright() as pw: await pw.chromium.launch(...)` with nothing behind it."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def chromium(self):
        return self

    async def launch(self, **_k):
        class Browser:
            async def close(self):
                return None

        return Browser()


def _run_scraper(monkeypatch, tmp_path, *, mode, schedule, today, page=None, scrape_day=None):
    """An AveloScraper whose run() goes through its real loop against a temp database, with no browser."""
    import scraper as scraper_module

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "f.db")
    db.init_db()
    monkeypatch.setattr(scraper_module, "async_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(scraper_module, "local_today", lambda: today)
    s = AveloScraper(screenshots=False, mode=mode, max_retries=0)

    async def noop(*_a, **_k):
        return None

    async def fetch_schedule():
        return schedule

    for name in ("_new_page", "_save_state", "_polite_delay", "_submit_search"):
        monkeypatch.setattr(s, name, noop)
    monkeypatch.setattr(s, "fetch_schedule", fetch_schedule)
    if scrape_day is not None:
        monkeypatch.setattr(s, "scrape_day", scrape_day)
    s._page = page
    return s


def test_local_today_is_the_calendar_date():
    from scraper import local_today

    assert local_today() == date.today()


def test_scrape_lowfare_route_never_covers_todays_disabled_cell(monkeypatch):
    """The captured page renders today (Sep 2 2026) as `td.day.today.disabled`: not covered, so never cleared."""
    monkeypatch.setattr("scraper.local_today", lambda: date(2026, 9, 2))
    s = AveloScraper(screenshots=False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(s, "_submit_search", noop)
    monkeypatch.setattr(s, "_polite_delay", noop)
    s._page = FakeCalendarPage({"09-2026": (FIXTURES / "lowfare_page.html").read_text()}, current="09-2026")
    days = [date(2026, 9, d) for d in range(1, 10)]
    flights, covered = asyncio.run(s.scrape_lowfare_route("ILG", "TPA", days))
    assert covered == days[2:]  # Sep 1 (past) and Sep 2 (today) are left out; Sep 8 (disabled, future) is covered
    assert date(2026, 9, 2) not in {f.departure_date for f in flights}


def test_answerable_days_leave_out_today_and_the_past():
    from scraper import answerable_days

    today = date(2026, 9, 2)
    days = [date(2026, 9, 1), today, date(2026, 9, 3), date(2026, 9, 9)]
    assert answerable_days(days, days[:3], today) == [date(2026, 9, 3)]


def test_unscheduled_days_lists_future_requested_days_the_schedule_dropped():
    from scraper import unscheduled_days

    today = date(2026, 10, 8)
    wanted = [date(2026, 10, d) for d in range(8, 13)]
    schedule = {("ILG", "TPA"): {date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)}}
    assert unscheduled_days([("ilg", "tpa"), ("ILG", "LAL")], wanted, schedule, today) == {
        ("ILG", "TPA"): [date(2026, 10, 10), date(2026, 10, 11)],  # today (Oct 8) is never listed
        ("ILG", "LAL"): [date(2026, 10, d) for d in range(9, 13)],  # a route the schedule dropped entirely
    }


def test_nightly_calendar_on_the_day_of_travel_keeps_todays_fares_and_raises_no_alerts(monkeypatch, tmp_path):
    """The captured calendar renders today (Sep 2 2026) as `td.day.today.disabled` with no fare. That is not a
    sold-out answer: today's timed fare stays, no sold_out event is raised and the watched package still resolves."""
    import alerts
    import signals
    from optimizer import GetawayQuery, Window, find_itineraries

    today = date(2026, 9, 2)
    html = (FIXTURES / "lowfare_page.html").read_text()
    days = [date(2026, 9, d) for d in range(2, 12)]
    s = _run_scraper(monkeypatch, tmp_path, mode="calendar", today=today,
                     schedule={("ILG", "TPA"): set(days)}, page=FakeCalendarPage({"09-2026": html}, current="09-2026"))
    out = Flight("ILG", "TPA", datetime(2026, 9, 2, 19), 59, duration_minutes=150, flight_id="AVELO-ILG-TPA")
    back = Flight("TPA", "ILG", datetime(2026, 9, 5, 12), 69, duration_minutes=150, flight_id="AVELO-TPA-ILG")
    db.upsert_flights([out, back])
    r1 = db.start_run("full", [("ILG", "TPA")])
    db.record_seen(r1, "ILG", "TPA", days, parse_lowfare_calendar(html, "ILG", "TPA") + [out])
    db.finish_run(r1, {}, status="ok")
    window = Window(datetime(2026, 9, 2, 12), datetime(2026, 9, 6, 23))
    alerts.watch_package(find_itineraries(GetawayQuery({"TPA"}, [window], origins=["ILG"], min_nights=1))[0])
    alerts.evaluate_packages()

    r2 = db.start_run("calendar", [("ILG", "TPA")])
    s.run_id = r2
    report = asyncio.run(s.run([("ILG", "TPA")], days))

    assert report.failed == 0 and report.succeeded == 1
    assert [(f.departure_datetime, f.price) for f in db.query_flights(["ILG"], ["TPA"], today, today)] == [
        (datetime(2026, 9, 2, 19), 59)]
    seen = db.seen_in_run(r2, "ILG", "TPA")
    assert today not in seen and seen[date(2026, 9, 3)] == 178 and seen[date(2026, 9, 8)] is None
    events = signals.run_detection(r2, allow_sold_out=True) + alerts.evaluate_packages()
    assert not [e for e in events if e.kind in ("sold_out", "watch_unavailable") and e.day == today]
    assert not [e for e in events if e.kind == "watch_unavailable"]


def test_detail_run_clears_a_sold_out_future_day_but_skips_today(monkeypatch, tmp_path):
    today = date(2026, 10, 8)
    later = date(2026, 10, 10)

    async def scrape_day(origin, dest, day):
        raise DateUnavailable(f"{origin}→{dest} {day}: no flights available")

    s = _run_scraper(monkeypatch, tmp_path, mode="detail", today=today,
                     schedule={("ILG", "TPA"): {today, later}}, scrape_day=scrape_day)
    stored = [Flight("ILG", "TPA", datetime(2026, 10, 8, 19), 59, duration_minutes=150, flight_id="AVELO-ILG-TPA"),
              Flight("ILG", "TPA", datetime(2026, 10, 10, 9), 79, duration_minutes=150, flight_id="AVELO-ILG-TPA")]
    db.upsert_flights(stored)
    s.run_id = db.start_run("detail", [("ILG", "TPA")])

    report = asyncio.run(s.run([("ILG", "TPA")], [today, later]))

    assert report.failed == 0 and report.succeeded == 2
    left = db.query_flights(["ILG"], ["TPA"], today, later)
    assert [f.departure_datetime for f in left] == [datetime(2026, 10, 8, 19)]  # today's fare kept, Oct 10 cleared
    assert db.seen_in_run(s.run_id, "ILG", "TPA") == {later: None}  # no sighting for today


def test_run_clears_fares_on_days_and_routes_the_schedule_no_longer_lists(monkeypatch, tmp_path):
    """routes/all dropped Oct 10 (a frequency cut) and ILG-LAL entirely: their stored fares go, without a sold-out
    sighting, while scheduled days are still searched normally."""
    today = date(2026, 10, 7)
    wanted = [date(2026, 10, d) for d in range(9, 14)]
    page = FakeCalendarPage({"10-2026": _month_grid("October", [(9, 79), (10, None), (12, 89)])}, current="10-2026")
    s = _run_scraper(monkeypatch, tmp_path, mode="calendar", today=today, page=page,
                     schedule={("ILG", "TPA"): {date(2026, 10, 9), date(2026, 10, 12)}})
    lowfare = lambda o, d, day, p: Flight(o, d, datetime(2026, 10, day), p, flight_id=f"LOWFARE-{o}-{d}")  # noqa: E731
    db.upsert_flights([lowfare("ILG", "TPA", 9, 79), lowfare("ILG", "TPA", 10, 59), lowfare("ILG", "TPA", 12, 89),
                       lowfare("ILG", "LAL", 11, 49), lowfare("ILG", "LAL", 20, 49)])
    s.run_id = db.start_run("calendar", [("ILG", "TPA"), ("ILG", "LAL")])

    report = asyncio.run(s.run([("ILG", "TPA"), ("ILG", "LAL")], wanted))

    assert report.skipped_routes == ["ILG-LAL"]
    tpa = {f.departure_date: f.price for f in db.query_flights(["ILG"], ["TPA"], wanted[0], wanted[-1])}
    assert tpa == {date(2026, 10, 9): 79, date(2026, 10, 12): 89}
    lal = [f.departure_date for f in db.query_flights(["ILG"], ["LAL"], date(2026, 10, 1), date(2026, 10, 31))]
    assert lal == [date(2026, 10, 20)]  # Oct 11 was requested and is gone; Oct 20 was not requested and stays
    assert db.seen_in_run(s.run_id, "ILG", "TPA") == {date(2026, 10, 9): 79, date(2026, 10, 12): 89}
    assert db.seen_in_run(s.run_id, "ILG", "LAL") == {}


# --- Avelo's Oct 2026 Select Flights layout: one div.main-block per flight with fare bundles --------------------


def test_select_page_with_bundles_reads_basic_fare_times_and_flight_number():
    html = (FIXTURES / "select_page_bundles.html").read_text()  # captured live: TPA→ILG Mon Oct 12 2026
    (f,) = parse_select_page(html, "TPA", "ILG", date(2026, 10, 12))
    assert f.departure_datetime == datetime(2026, 10, 12, 7, 0) and f.arrival_datetime == datetime(2026, 10, 12, 9, 24)
    assert f.duration_minutes == 144 and f.time_known
    assert (f.price, f.base_price, f.is_member_fare) == (110.0, 147.0, True)  # Basic bundle, not the +$61 upsell
    assert f.flight_number == "533"
    assert f.flight_id == "AVELO-TPA-ILG"  # unchanged id keeps price history continuous


def test_bundle_upsell_prices_are_never_taken_as_the_fare():
    html = (FIXTURES / "select_page_bundles.html").read_text()
    stripped = re.sub(r'<div class="price subscription".*?<div class="divider"></div>', "", html, count=1, flags=re.S)
    (f,) = parse_select_page(stripped, "TPA", "ILG", date(2026, 10, 12))
    assert (f.price, f.is_member_fare) == (147.0, False)  # public Basic fare, never "+$61"


def test_bundles_page_is_not_mistaken_for_sold_out():
    assert not select_page_is_sold_out((FIXTURES / "select_page_bundles.html").read_text())


def test_scrape_day_returns_flights_from_the_bundles_layout():
    s = _results_scraper((FIXTURES / "select_page_bundles.html").read_text())
    (f,) = asyncio.run(s.scrape_day("TPA", "ILG", date(2026, 10, 12)))
    assert f.price == 110.0 and f.flight_number == "533"


def test_avelo_parsers_keep_the_member_price():
    (f,) = parse_select_page((FIXTURES / "select_page_bundles.html").read_text(), "TPA", "ILG", date(2026, 10, 12))
    assert (f.price, f.member_price, f.base_price, f.airline) == (110.0, 110.0, 147.0, "XP")
