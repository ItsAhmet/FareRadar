"""Playwright scraper for Avelo Airlines fares.

How Avelo's site works (captured Sep 2026)
------------------------------------------
* `/flight-search/search` is a Blazor WebAssembly app. URL parameters are
  ignored and the fare search itself is gRPC-web (binary), so the scraper
  drives the real form with clicks and reads the rendered results.
* On load, the page fetches `/flight-search/services/routes/all`: JSON listing
  every origin → destination pair and the exact dates it operates. We capture
  that response and use it as the schedule, so we only search days that
  actually have a flight (and skip pairs Avelo doesn't fly at all).
* With the "Low Fare Calendar" toggle on, submitting lands on
  `/flight-search/lowfare`: a month grid with the lowest fare per day and tabs
  for the following months. One page load per route covers months of fares.
* Without the toggle, submitting lands on `/flight-search/select`: one card per
  flight with departure/arrival times, duration and both fare tiers. No flight
  number is shown anywhere, so `flight_number` stays NULL.
* Every fare block carries two prices: the Avelo PLUS member fare
  (`.price.subscription`) and the public fare (`.price.standard`). The parsers
  record the member fare as `price` (is_member_fare=True) and keep the public
  fare in `base_price`. If a block shows only one price it is stored as the
  public fare.

Modes
-----
calendar (default)  lowest fare per route per day; fast (~1 page per route)
detail              per-day search that adds times/durations; slow (~1 page per route-day)
full                calendar first, then detail for every scheduled day

Stealth
-------
One user-agent/viewport per run (a fingerprint that changes mid-session is
itself a tell), cookies persisted between runs in
`data/browser_state_XP.json`, `navigator.webdriver` masked, trackers blocked,
and randomised human-like pauses and mouse movement between form actions.
That plumbing is shared by every airline and lives in `browser.py`.

Parsing is split into pure functions that take HTML so it can be unit-tested
against captured fixtures without a browser.
"""
from __future__ import annotations

import asyncio  # noqa: F401 - tests patch scraper.asyncio.sleep
import logging
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

import config
import database
import persist
from browser import (BLOCKED_HOSTS, BaseScraper, BlockedError, DateUnavailable, Mode, ScrapeError,  # noqa: F401
                     ScrapeReport, answerable_days, local_today)
from models import Flight

log = logging.getLogger(__name__)

LOWFARE_PREFIX = "LOWFARE"  # flight_id prefix for calendar rows (no time known)
DETAIL_PREFIX = "AVELO"  # flight_id prefix for rows parsed from flight cards
SEL = config.SELECTORS
# What Avelo's Select Flights page says when a scheduled day has sold out (seen Sep 2 2026, SRQ→HVN Sep 7; no flight
# cards at all). The date picker still accepts such a day, so this answer only arrives on the results page.
NO_FLIGHTS_PATTERN = re.compile(r"no\s+flights\s+available", re.I)
SOLD_OUT_SETTLE_MS = 1_500  # re-check once before believing the message: cards may still be on their way


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #


def parse_schedule(payload: list[dict]) -> dict[tuple[str, str], set[date]]:
    """routes/all JSON → {(origin, destination): {operating dates}}."""
    schedule: dict[tuple[str, str], set[date]] = {}
    for origin in payload or []:
        o = (origin.get("Code") or "").upper()
        for loc in origin.get("Locations", {}).get("Items", []):
            d = (loc.get("Code") or "").upper()
            days = set()
            for item in loc.get("Days", {}).get("Items", []):
                try:
                    days.add(date.fromisoformat(item.get("Code", "")))
                except ValueError:
                    continue
            if o and d and days:
                schedule.setdefault((o, d), set()).update(days)
    return schedule


def _money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _clock(text: str) -> time | None:
    """'11:00 AM' / '1:59 pm' → time."""
    try:
        return datetime.strptime(text.strip().upper(), "%I:%M %p").time()
    except ValueError:
        return None


def _duration_minutes(text: str | None) -> int | None:
    """'2h 59m Nonstop' → 179."""
    if not text:
        return None
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    if not (h or m):
        return None
    return int(h.group(1) if h else 0) * 60 + int(m.group(1) if m else 0)


def _fares(container) -> tuple[float | None, float | None]:
    """(Avelo PLUS member fare, public fare) from a day cell or flight card."""
    member_el = container.select_one(SEL["member_fare"])
    public_el = container.select_one(SEL["public_fare"])
    member = _money(member_el.get_text(" ", strip=True)) if member_el else None
    public = _money(public_el.get_text(" ", strip=True)) if public_el else None
    if member is None and public is None:
        any_el = container.select_one(".price")
        public = _money(any_el.get_text(" ", strip=True)) if any_el else None
    return member, public


def _price_fields(container) -> dict | None:
    """Flight kwargs for price/base_price/member_price/is_member_fare, or None if no fare."""
    member, public = _fares(container)
    if member is not None:
        return {"price": member, "base_price": public, "member_price": member, "is_member_fare": True}
    if public is not None:
        return {"price": public, "base_price": public, "member_price": None, "is_member_fare": False}
    return None


def parse_lowfare_calendar(
    html: str,
    origin: str,
    destination: str,
    scraped_at: datetime | None = None,
) -> list[Flight]:
    """Low Fare Calendar page → one placeholder Flight per day with a fare.

    Departure time is unknown on this page, so rows are stored at 00:00 with a
    `LOWFARE-` flight_id; a later detail scrape replaces them.
    """
    scraped_at = scraped_at or datetime.now()
    soup = BeautifulSoup(html, "html.parser")
    flights: list[Flight] = []
    for cell in soup.select(SEL["lowfare_day"]):
        label = (cell.get("aria-label") or "").strip()
        try:
            day = datetime.strptime(label, "%B, %d %Y").date()  # "September, 03 2026"
        except ValueError:
            log.debug("unparsed calendar label %r", label)
            continue
        fare = _price_fields(cell)
        if fare is None:
            continue
        flights.append(
            Flight(
                origin=origin,
                destination=destination,
                departure_datetime=datetime.combine(day, time(0, 0)),
                flight_id=f"{LOWFARE_PREFIX}-{origin.upper()}-{destination.upper()}",
                timestamp_scraped=scraped_at,
                **fare,
            )
        )
    return flights


def parse_select_page(
    html: str,
    origin: str,
    destination: str,
    day: date,
    scraped_at: datetime | None = None,
) -> list[Flight]:
    """Select Flights page → one Flight per card with times and duration."""
    scraped_at = scraped_at or datetime.now()
    soup = BeautifulSoup(html, "html.parser")
    # One card per flight. Try each layout in turn rather than the combined wait selector: the Sep 2026 button
    # also contains a div.main-block, so matching both at once would count every flight twice.
    cards = (soup.select("button.flight-block-wrapper-v2") or soup.select("div.main-block")
             or soup.select(".flight-block"))
    flights: list[Flight] = []
    for card in cards:
        times = [_clock(t.get_text(strip=True)) for t in card.select(".scheduled-time")]
        times = [t for t in times if t]
        if not times:
            continue
        dep_dt = datetime.combine(day, times[0])
        arr_dt = datetime.combine(day, times[-1]) if len(times) > 1 else None
        if arr_dt is not None and arr_dt < dep_dt:
            arr_dt += timedelta(days=1)  # red-eye crossing midnight
        dur_el = card.select_one(".flight-block-duration")
        duration = _duration_minutes(dur_el.get_text(" ", strip=True) if dur_el else None)
        if duration is None and arr_dt is not None:
            duration = int((arr_dt - dep_dt).total_seconds() // 60)
        # Bundles layout: the first bundle is "Basic fare only"; the others show "+$61"-style upsells, not fares.
        fare = _price_fields(card.select_one(".fare-prices") or card.select_one("button.bundle") or card)
        if fare is None:
            continue
        number_el = card.select_one(".flight-number")
        number = re.search(r"\d+", number_el.get_text(" ", strip=True)) if number_el else None
        flights.append(
            Flight(
                origin=origin,
                destination=destination,
                departure_datetime=dep_dt,
                arrival_datetime=arr_dt,
                duration_minutes=duration,
                flight_number=number.group(0) if number else None,
                flight_id=f"{DETAIL_PREFIX}-{origin.upper()}-{destination.upper()}",
                timestamp_scraped=scraped_at,
                **fare,
            )
        )
    return flights


def select_page_is_sold_out(html: str) -> bool:
    """True when the Select Flights page answers "There are no flights available for the selected date"."""
    soup = BeautifulSoup(html, "html.parser")
    for hidden in soup(["script", "style"]):  # only text the user can read counts
        hidden.decompose()
    return bool(NO_FLIGHTS_PATTERN.search(soup.get_text(" ", strip=True)))


@dataclass(frozen=True)
class ScrapeTarget:
    origin: str
    destination: str
    days: tuple[date, ...]


def plan_targets(
    routes: Iterable[tuple[str, str]],
    dates: Iterable[date],
    schedule: dict[tuple[str, str], set[date]],
) -> tuple[list[ScrapeTarget], list[str]]:
    """Intersect the requested routes/dates with Avelo's real schedule.

    Returns the targets to scrape and the routes skipped because Avelo has no
    flight on any requested day. This is the only place the UI's filters meet
    the site, so it is pure and unit-tested: nothing outside the request is
    ever visited.
    """
    wanted = set(dates)
    targets: list[ScrapeTarget] = []
    skipped: list[str] = []
    for origin, dest in routes:
        origin, dest = origin.upper(), dest.upper()
        days = sorted(schedule.get((origin, dest), set()) & wanted)
        if days:
            targets.append(ScrapeTarget(origin, dest, tuple(days)))
        else:
            skipped.append(f"{origin}-{dest}")
    return targets, skipped


def unscheduled_days(
    routes: Iterable[tuple[str, str]],
    dates: Iterable[date],
    schedule: dict[tuple[str, str], set[date]],
    today: date,
) -> dict[tuple[str, str], list[date]]:
    """Requested route-days after today that Avelo's schedule (routes/all) does not list.

    The schedule is authoritative (an empty payload is a ScrapeError, never an
    empty schedule), so a fare stored earlier on one of these days is a flight
    Avelo no longer operates: the run deletes it instead of offering it until
    it departs. Routes that `plan_targets` skips entirely are included.
    """
    future = {d for d in dates if d > today}
    out: dict[tuple[str, str], list[date]] = {}
    for origin, dest in routes:
        key = (origin.upper(), dest.upper())
        days = sorted(future - schedule.get(key, set()))
        if days:
            out[key] = days
    return out


# --------------------------------------------------------------------------- #
# Browser driver
# --------------------------------------------------------------------------- #


class AveloScraper(BaseScraper):
    airline = "XP"

    # ---- lifecycle ------------------------------------------------------- #

    async def run(self, routes: Iterable[tuple[str, str]], dates: Iterable[date]) -> ScrapeReport:
        """Scrape every route for every date, writing to SQLite as it goes."""
        wanted = set(dates)
        report = ScrapeReport()
        if not wanted:
            return report

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless, args=list(config.BROWSER_ARGS))
            try:
                await self._new_page(browser)
                if self.promo_code:
                    log.info("promo code %s will be applied to every search", self.promo_code)
                schedule = await self._retry(self.fetch_schedule, "schedule")
                database.upsert_catalog("XP", schedule.keys(), "routes/all")
                today = local_today()
                routes = list(routes)
                targets, report.skipped_routes = plan_targets(routes, wanted, schedule)
                for (origin, dest), gone in unscheduled_days(routes, wanted, schedule, today).items():
                    persist.clear_unscheduled(origin, dest, gone)
                for skipped in report.skipped_routes:
                    log.info("%s: no scheduled flights in range, skipping", skipped)
                for target in targets:
                    origin, dest, days = target.origin, target.destination, list(target.days)
                    log.info("%s→%s: %d scheduled days between %s and %s", origin, dest, len(days), days[0], days[-1])

                    if self.mode in ("calendar", "full"):
                        report.attempted += 1
                        try:
                            flights, covered = await self._retry(self.scrape_lowfare_route, f"{origin}-{dest}-calendar", origin, dest, days)
                            flights = [self._tag(f) for f in flights if f.departure_date in wanted]
                            covered = answerable_days(covered, wanted, today)
                            report.flights_written += persist.save_calendar(self.run_id, origin, dest, covered, flights)
                            report.succeeded += 1
                            await self._save_state()
                            log.info("%s→%s calendar: %d fares over %d days", origin, dest, len(flights), len(covered))
                        except Exception as exc:  # noqa: BLE001 - keep the loop alive
                            report.fail(f"{origin}-{dest} calendar: {exc}")
                        await self._polite_delay()

                    if self.mode in ("detail", "full"):
                        for day in days:
                            report.attempted += 1
                            try:
                                try:
                                    flights = await self._retry(self.scrape_day, f"{origin}-{dest}-{day}", origin, dest, day)
                                except DateUnavailable:
                                    if day <= today:
                                        # Today (or a past day) not being offered is not a sold-out answer:
                                        # keep its stored fares and record no sighting.
                                        log.info("%s→%s %s: not offered any more today; stored fares kept", origin, dest, day)
                                        report.succeeded += 1
                                        await self._polite_delay()
                                        continue
                                    log.info("%s→%s %s: not selectable (sold out)", origin, dest, day)
                                    flights = []
                                flights = [self._tag(f) for f in flights]
                                report.flights_written += persist.save_day(self.run_id, origin, dest, day, flights)
                                report.succeeded += 1
                                await self._save_state()
                                log.info("%s→%s %s: %d flight(s)", origin, dest, day, len(flights))
                            except Exception as exc:  # noqa: BLE001
                                report.fail(f"{origin}-{dest} {day}: {exc}")
                            await self._polite_delay()
            finally:
                await browser.close()
        return report

    # ---- schedule --------------------------------------------------------- #

    async def fetch_schedule(self) -> dict[tuple[str, str], set[date]]:
        """Load the search page and capture the routes/all JSON it requests."""
        page = self.page
        try:
            async with page.expect_response(
                lambda r: r.url.endswith(config.ROUTES_ENDPOINT_SUFFIX) and r.ok, timeout=self.timeout_ms
            ) as info:
                await page.goto(config.AVELO_SEARCH_URL, wait_until="domcontentloaded")
            payload = await (await info.value).json()
        except PlaywrightTimeout:
            # Fallback: ask the page itself (same origin, cookies included).
            log.warning("routes/all not observed on load; fetching from page context")
            payload = await page.evaluate(
                "fetch('services/routes/all', {headers: {Accept: 'application/json'}}).then(r => r.json())"
            )
        schedule = parse_schedule(payload)
        if not schedule:
            raise ScrapeError("schedule payload was empty")
        log.info("schedule loaded: %d directional routes", len(schedule))
        return schedule

    # ---- form driving ----------------------------------------------------- #

    async def _submit_search(self, origin: str, dest: str, day: date, lowfare: bool) -> None:
        page = self.page
        await page.goto(config.AVELO_SEARCH_URL, wait_until="domcontentloaded")
        await page.wait_for_selector(SEL["form_ready"], state="visible")
        await page.wait_for_timeout(600)  # let Blazor finish wiring handlers
        await self._wander()

        # Trip-type radios are visually hidden behind styled labels, so Playwright's
        # actionability checks refuse them; a DOM click still fires Blazor's handler.
        await page.evaluate(f"document.querySelectorAll(\"{SEL['one_way_radio']}\").forEach(i => i.click())")
        await page.evaluate(
            f"(() => {{ const c = document.querySelector('{SEL['lowfare_checkbox']}');"
            f" if (c && c.checked !== {str(lowfare).lower()}) c.click(); }})()"
        )
        await self._pick_location(0, origin)
        await self._human_pause()
        await self._pick_location(1, dest)
        await self._human_pause()
        await self._pick_date(day)
        if self.promo_code:
            await self._apply_promo()
        await self._human_pause()

        submit = page.locator(SEL["submit"]).first
        await page.wait_for_function(
            f"(() => {{ const b = document.querySelector('{SEL['submit']}'); return b && !b.classList.contains('disabled'); }})()",
            timeout=10_000,
        )
        try:
            await submit.click(timeout=8_000)
        except PlaywrightTimeout:
            # An overlay is still intercepting pointer events; Blazor handles a DOM click just the same.
            log.debug("submit click intercepted; dispatching DOM click")
            await submit.dispatch_event("click")

    async def _pick_location(self, index: int, code: str) -> None:
        page = self.page
        button = page.locator(SEL["location_button"]).nth(index)
        # Right after load, Blazor may not have attached its click handler yet, so
        # a click can silently do nothing. Click until the dropdown reports open.
        for _ in range(4):
            await button.click()
            try:
                await page.wait_for_function(
                    f"[...document.querySelectorAll('{SEL['location_button']}')][{index}]?.getAttribute('aria-expanded') === 'true'",
                    timeout=2_500,
                )
                break
            except PlaywrightTimeout:
                await page.wait_for_timeout(500)
        else:
            raise ScrapeError(f"airport dropdown {index} never opened")
        # The open list already contains every airport, so click it directly. Typing
        # fires a server search per keystroke and can leave the list stuck on
        # "Loading Locations…", so it's only a fallback.
        pattern = re.compile(rf"-\s*{code}\s*$")
        option = page.get_by_text(pattern).filter(visible=True).first
        try:
            await option.wait_for(state="visible", timeout=6_000)
        except PlaywrightTimeout:
            log.debug("%s not in the open list; typing to filter", code)
            await page.keyboard.type(code, delay=random.randint(150, 260))
            await option.wait_for(state="visible", timeout=15_000)
        await option.click()
        await page.wait_for_function(
            f"[...document.querySelectorAll('{SEL['location_button']}')][{index}]?.innerText.includes('{code}')",
            timeout=10_000,
        )

    async def _pick_date(self, day: date) -> None:
        page = self.page
        if not await page.locator(SEL["date_cell"]).count():
            await page.locator(SEL["date_button"]).first.click()
        suffix = f"{day:%B} {day.day}, {day.year}"  # aria-label ends "September 16, 2026"
        cell = page.locator(f"{SEL['date_cell']}[aria-label$='{suffix}']").first
        try:
            await cell.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeout as exc:
            if not await page.locator(SEL["date_cell"]).filter(visible=True).count():
                # Not one selectable day: the picker did not render. That is a failure to retry, not an
                # answer, and must not clear this day's stored fares as if it had sold out.
                raise ScrapeError(f"date picker showed no selectable days (wanted {day})") from exc
            # Only a missing cell inside the wanted month says anything about that day. If the picker is showing
            # another month (a detail run spanning October and November opens on October), the day is simply
            # off screen: that is a failure to retry, never a sell-out that would clear its stored fares.
            month = page.locator(f"td[aria-label*='{day:%B} '][aria-label$=', {day.year}']").filter(visible=True)
            if not await month.count():
                raise ScrapeError(f"date picker is not showing {day:%B %Y} (wanted {day})") from exc
            raise DateUnavailable(f"no selectable calendar cell for {day} (not flown)") from exc
        await cell.click()

    async def _apply_promo(self) -> None:
        """Open the promo popover, type the code, apply it, close the popover.
        A rejected code is logged and the search continues without it."""
        page = self.page
        toggle = page.locator(SEL["promo_toggle"]).first
        if await toggle.count() and await toggle.is_visible():
            await toggle.click()
            await self._human_pause()
        # Opening the popover renders a second #promo; only the popover's copy is visible.
        box = page.locator(SEL["promo_input"]).filter(visible=True).first
        try:
            await box.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeout:
            log.warning("promo field not found on the search form; continuing without %s", self.promo_code)
            self.promo_applied = False
            return
        await box.click()
        await box.fill("")
        await page.keyboard.type(self.promo_code, delay=random.randint(60, 140))
        apply = page.locator(SEL["promo_apply"]).filter(visible=True).first
        if await apply.count():
            await apply.click()
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(1_200)

        popover = page.locator(SEL["promo_popover"]).filter(visible=True).first
        note = (await popover.inner_text()).strip().replace("\n", " ") if await popover.count() else ""
        lowered = note.lower()
        rejected = any(w in lowered for w in ("invalid", "not valid", "not recognized", "expired", "unable"))
        self.promo_applied = not rejected
        if rejected:
            log.warning("Avelo rejected promo code %s (%s); scraping without it", self.promo_code, note[:120])
        else:
            log.info("promo code %s entered%s", self.promo_code, f" ({note[:80]})" if note else "")
        await self._close_popovers()

    async def _close_popovers(self) -> None:
        """Avelo leaves an invisible overlay behind open popovers that swallows
        clicks on the rest of the form. Escape, then click the overlay away."""
        page = self.page
        overlay = page.locator(SEL["popover_overlay"])
        for _ in range(3):
            if not await overlay.count() or not await overlay.first.is_visible():
                return
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            if await overlay.count() and await overlay.first.is_visible():
                try:
                    await overlay.first.click(timeout=2_000, position={"x": 5, "y": 5})
                except PlaywrightTimeout:
                    pass
                await page.wait_for_timeout(300)

    # ---- results pages ---------------------------------------------------- #

    async def scrape_lowfare_route(self, origin: str, dest: str, days: list[date]) -> tuple[list[Flight], list[date]]:
        """Low Fare Calendar for a route: every month that `days` touches.

        Returns the fares and the covered days: those of `days` after today in a month that parsed at least one fare.
        """
        page = self.page
        # The form needs *a* flight day to submit; if the picker offers no cell for the first, try the next.
        last_error: Exception | None = None
        for candidate in days[:3]:
            try:
                await self._submit_search(origin, dest, candidate, lowfare=True)
                break
            except DateUnavailable as exc:
                last_error = exc
                log.info("%s→%s: %s not selectable, trying another day", origin, dest, candidate)
        else:
            raise ScrapeError(f"none of the first days were selectable: {last_error}")
        await page.wait_for_url(re.compile(r"/flight-search/lowfare"))
        await page.wait_for_selector(SEL["lowfare_day"])

        flights: list[Flight] = []
        covered: list[date] = []
        for month in sorted({date(d.year, d.month, 1) for d in days}):
            month_cells = f"td.day[aria-label^='{month:%B}, ']"
            if not await page.locator(month_cells).count():
                tab = page.locator(f"{SEL['lowfare_month_tab']}[id='{month:%m-%Y}']")
                if not await tab.count():
                    log.warning("%s→%s: month %s not offered on the calendar", origin, dest, f"{month:%b %Y}")
                    continue
                await tab.click()
                await page.wait_for_selector(month_cells, timeout=15_000)
                await page.wait_for_timeout(400)
            parsed = parse_lowfare_calendar(await page.content(), origin, dest)
            in_month = [f for f in parsed if (f.departure_date.year, f.departure_date.month) == (month.year, month.month)]
            if in_month:
                flights.extend(in_month)
                covered.extend(d for d in days if (d.year, d.month) == (month.year, month.month))
            else:
                # Rendered but nothing parsed (fares arrived late, markup drift): that is not
                # "sold out", so the month stays uncovered and its stored fares are left alone.
                log.warning("%s→%s: month %s rendered but no fares parsed; leaving its stored fares untouched",
                            origin, dest, f"{month:%b %Y}")
            await self._polite_delay(short=True)
        if not flights:
            raise ScrapeError("calendar rendered but no fares parsed (selectors may have changed)")
        return flights, answerable_days(covered, days, local_today())  # today's disabled cell is not "sold out"

    async def scrape_day(self, origin: str, dest: str, day: date) -> list[Flight]:
        """Standard search for one day: every flight card with times and fare."""
        page = self.page
        await self._submit_search(origin, dest, day, lowfare=False)
        await page.wait_for_url(re.compile(r"/flight-search/select"))
        # A sold-out day shows a message instead of cards: wait for either rather than timing out on the cards.
        await page.locator(SEL["flight_card"]).or_(page.get_by_text(NO_FLIGHTS_PATTERN)).first.wait_for(state="visible")
        html = await page.content()
        flights = parse_select_page(html, origin, dest, day)
        if not flights and select_page_is_sold_out(html):
            await page.wait_for_timeout(SOLD_OUT_SETTLE_MS)
            html = await page.content()
            flights = parse_select_page(html, origin, dest, day)
            if not flights and select_page_is_sold_out(html):
                raise DateUnavailable(f"{origin}→{dest} {day}: Avelo reports no flights available for that date")
        if not flights:
            raise ScrapeError("results rendered but no flight cards parsed (selectors may have changed)")
        return flights
