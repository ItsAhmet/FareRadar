"""Frontier Airlines: pure parsers over the JSON its booking site embeds, plus a thin browser driver
and the route-catalog refresh.

How Frontier's site works (probed 2026-10-06, fixtures in tests/fixtures/frontier/):
* `booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov 12, 2026&ADT=1&mon=true` renders a
  server-side page that embeds every flight as JSON in `window.FlightDataJson`: fares for Discount Den and
  standard, local times, stops. Most flights are connections; only `stopCount == 0` single-leg flights count.
* Adding `&c=true` shows a month calendar that POSTs `/Flight/GetLowFareCalendarAvailability`; the response
  holds the lowest **Discount Den** total per day (amount + taxesAndFees). It does not say whether that fare
  is a nonstop, so calendar rows are placeholders that a day search confirms.
* `flights.flyfrontier.com/en/flights-from-<city>` (plain HTTP) lists the destinations it sells from a city.
"""
from __future__ import annotations

import logging
import re
import urllib.request
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

import airlines
import config
import database
import persist
from browser import BaseScraper, BlockedError, ScrapeError, ScrapeReport, answerable_days, local_today
from models import Flight
from netutil import open_url

log = logging.getLogger(__name__)

AIRLINE = "F9"
_F9 = airlines.get(AIRLINE)
_CHALLENGE_MARKERS = ("px-captcha", "press &amp; hold", "press & hold", "access denied", "are you a robot", "perimeterx")
_ROUTE_PAIR = re.compile(r'"originAirportCode":"([A-Z]{3})","destinationAirportCode":"([A-Z]{3})"')


def _fare(value) -> float | None:
    """Frontier uses 0 or -1 for "not offered"."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return round(amount, 2) if amount > 0 else None


def _minutes(hms: str | None) -> int | None:
    """'02:35:00' → 155."""
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", hms or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _journey_flights(payload: dict) -> list[dict]:
    journeys = (payload or {}).get("journeys") or []
    return (journeys[0].get("flights") or []) if journeys else []


def select_has_flights(payload: dict) -> bool:
    """Did the day search return any flight at all (nonstop or connecting)?"""
    return bool(_journey_flights(payload))


def parse_select(payload: dict, origin: str, destination: str, day: date, member: bool = False,
                 scraped_at: datetime | None = None) -> list[Flight]:
    """`window.FlightDataJson` → one Flight per nonstop on `day` for this route."""
    scraped_at = scraped_at or datetime.now()
    origin, destination = origin.strip().upper(), destination.strip().upper()
    out: list[Flight] = []
    for item in _journey_flights(payload):
        legs = item.get("legs") or []
        if item.get("stopCount") != 0 or len(legs) != 1:
            continue
        leg = legs[0]
        if (_F9.app_code(leg.get("departureStation", "")), _F9.app_code(leg.get("arrivalStation", ""))) != (origin, destination):
            continue
        try:
            departs = datetime.fromisoformat(leg["departureDate"])
            arrives = datetime.fromisoformat(leg["arrivalDate"])
        except (KeyError, TypeError, ValueError):
            continue
        if departs.date() != day:
            continue
        standard, den = _fare(item.get("standardFare")), _fare(item.get("discountDenFare"))
        price, is_member = airlines.owner_price(member, den, standard)
        if price is None:
            continue
        number = str(leg.get("flightNumber") or "").strip() or None
        out.append(Flight(
            airline=AIRLINE, origin=origin, destination=destination,
            departure_datetime=departs, arrival_datetime=arrives,
            duration_minutes=_minutes(leg.get("duration")) or int((arrives - departs).total_seconds() // 60),
            flight_number=number, price=price, base_price=standard, member_price=den, is_member_fare=is_member,
            flight_id=f"F9-{number or 'UNK'}-{origin}-{destination}", timestamp_scraped=scraped_at,
        ))
    return out


def parse_calendar(payload: dict, origin: str, destination: str, member: bool = False,
                   scraped_at: datetime | None = None) -> tuple[list[Flight], list[date]]:
    """Calendar response → (one placeholder per priced day, the days the response answered for).

    A response without a single priced day is not an answer ("everything sold out" and "the
    calendar did not load its fares" look the same), so it answers for no day at all.
    """
    if not (payload or {}).get("success") or not payload.get("calendarSections"):
        raise ScrapeError("Frontier calendar response missing or unsuccessful")
    scraped_at = scraped_at or datetime.now()
    origin, destination = origin.strip().upper(), destination.strip().upper()
    section = payload["calendarSections"][0]
    # The response is a captured POST: a late reply for the previous route must not be stored under this one.
    stations = (_F9.app_code(section.get("departingStation") or ""), _F9.app_code(section.get("arrivalStation") or ""))
    if stations != (origin, destination):
        raise ScrapeError(f"Frontier calendar is for {stations[0] or '?'}-{stations[1] or '?'}, not {origin}-{destination}")
    flights: list[Flight] = []
    answered: list[date] = []
    for week in section.get("weeks") or []:
        for cell in week.get("days") or []:
            if cell.get("isEmpty"):
                continue
            try:
                day = date.fromisoformat(cell["dateFormatted"])
            except (KeyError, TypeError, ValueError):
                continue
            answered.append(day)
            amount = _fare(cell.get("amount"))
            if cell.get("isUnavailable") or amount is None:
                continue
            total = round(amount + float(cell.get("taxesAndFees") or 0.0), 2)
            den = total if cell.get("isDiscountDen") else None
            standard = None if cell.get("isDiscountDen") else total
            price, is_member = airlines.owner_price(member, den, standard)
            flights.append(Flight(
                airline=AIRLINE, origin=origin, destination=destination,
                departure_datetime=datetime.combine(day, time(0)), price=price, base_price=standard,
                member_price=den, is_member_fare=is_member,
                flight_id=f"F9CAL-{origin}-{destination}", timestamp_scraped=scraped_at,
            ))
    if not flights:
        return [], []
    return flights, sorted(set(answered))


def parse_routes(html: str, origin: str) -> set[str]:
    """Destinations (app codes) Frontier's public "flights from <city>" page lists for `origin`."""
    origin = origin.strip().upper()
    site_origin = _F9.site_code(origin)
    return {_F9.app_code(dest) for o, dest in _ROUTE_PAIR.findall(html or "") if o == site_origin and dest != site_origin}


def is_challenge_page(html: str) -> bool:
    low = (html or "").lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def probe_days(flights: list[Flight], limit: int) -> list[date]:
    """The cheapest distinct days among calendar placeholders, in date order: where to look for a nonstop."""
    cheapest: dict[date, float] = {}
    for f in flights:
        cheapest[f.departure_date] = min(f.price, cheapest.get(f.departure_date, float("inf")))
    return sorted(sorted(cheapest, key=lambda d: (cheapest[d], d))[:limit])


# --------------------------------------------------------------------------- #
# Route catalog
# --------------------------------------------------------------------------- #


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENTS[0], "Accept-Language": "en-US,en;q=0.9"})
    with open_url(request, 20) as response:
        return response.read().decode("utf-8", "replace")


def refresh_catalog(origins: Iterable[str], conn=None, max_age_days: int = 7, now: datetime | None = None,
                    fetch: Callable[[str], str] | None = None) -> int:
    """Re-read Frontier's public route pages for home airports whose catalog is older than `max_age_days`.

    Never removes routes: an unreachable or changed page writes nothing, and routes that stop being listed
    age out of `database.catalog_routes` on their own.
    """
    if fetch is None:
        if config.OFFLINE:
            return 0
        fetch = _http_get
    now = now or datetime.now()
    written = 0
    for origin in origins:
        origin = origin.strip().upper()
        slug = config.FRONTIER_ROUTE_PAGES.get(origin)
        if not slug:
            continue
        checked = database.catalog_checked(AIRLINE, origin, conn)
        if checked is not None and now - checked < timedelta(days=max_age_days):
            continue
        try:
            destinations = parse_routes(fetch(config.FRONTIER_ROUTES_URL.format(slug=slug)), origin)
        except Exception as exc:  # noqa: BLE001 - the catalog is best-effort; keep what we have
            log.warning("Frontier route page for %s unavailable: %s", origin, exc)
            continue
        if not destinations:
            log.warning("Frontier route page for %s listed no destinations; keeping the stored catalog", origin)
            continue
        pairs = [(origin, d) for d in destinations] + [(d, origin) for d in destinations]
        written += database.upsert_catalog(AIRLINE, pairs, "route-page", conn, now=now)
    return written


# --------------------------------------------------------------------------- #
# Browser driver
# --------------------------------------------------------------------------- #

_FLIGHT_DATA_READY = "window.FlightDataJson && window.FlightDataJson.journeys"
_FLIGHT_DATA_READ = "JSON.parse(JSON.stringify(window.FlightDataJson))"


class FrontierScraper(BaseScraper):
    airline = AIRLINE

    def __init__(self, *args, member: bool | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.member = database.memberships().get(AIRLINE, False) if member is None else member
        self._blocked_streak = 0

    async def run(self, routes: Iterable[tuple[str, str]], dates: Iterable[date]) -> ScrapeReport:
        """Scrape every route for every future date, writing to SQLite as it goes."""
        wanted, today, report = set(dates), local_today(), ScrapeReport()
        if not any(d > today for d in wanted):
            return report
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless, args=list(config.BROWSER_ARGS))
            try:
                await self._new_page(browser)
                await self.warm_up()
                await self._run_routes(list(routes), wanted, today, report)
            finally:
                await browser.close()
        return report

    async def warm_up(self) -> None:
        """Land on the home page first, as a person would, so the booking site sees a normal referrer and cookies."""
        await self.page.goto(config.FRONTIER_HOME_URL, wait_until="domcontentloaded")
        await self._human_pause()

    async def _run_routes(self, routes: list[tuple[str, str]], wanted: set[date], today: date, report: ScrapeReport) -> None:
        future = sorted(d for d in wanted if d > today)
        for origin, dest in routes:
            origin, dest = origin.upper(), dest.upper()
            if self._blocked_streak >= 2:
                report.blocked = True
                log.error("Frontier is challenging us; stopping this airline for the run")
                break
            known = database.route_nonstop(AIRLINE, origin, dest)
            if known is False:
                log.info("%s→%s: no Frontier nonstop (rechecked monthly), skipping", origin, dest)
                continue

            months: list[tuple[list[date], list[Flight]]] = []
            calendar_flights: list[Flight] = []
            if self.mode in ("calendar", "full"):
                for anchor in sorted({min(d for d in future if (d.year, d.month) == ym) for ym in {(d.year, d.month) for d in future}}):
                    flights, answered = await self._attempt(report, f"{origin}-{dest} calendar {anchor:%b %Y}",
                                                            self.scrape_calendar_month, origin, dest, anchor) or ([], [])
                    flights = [self._tag(f) for f in flights if f.departure_date in wanted and f.departure_date > today]
                    months.append((answerable_days(answered, wanted, today), flights))
                    calendar_flights.extend(flights)
                    if self._blocked_streak >= 2:
                        break

            if self.mode == "detail":
                days = future
            elif self.mode == "full":
                days = sorted({f.departure_date for f in calendar_flights})
            else:
                days = probe_days(calendar_flights, config.FRONTIER_PROBE_DAYS) if known is None else []

            searched, no_nonstop = 0, set()
            for day in days:
                if self._blocked_streak >= 2:
                    break
                flights = await self._attempt(report, f"{origin}-{dest} {day}", self.scrape_day, origin, dest, day)
                if flights is None:
                    continue
                searched += 1
                flights = [self._tag(f) for f in flights]
                report.flights_written += persist.save_day(self.run_id, origin, dest, day, flights, airline=AIRLINE)
                if not flights:
                    no_nonstop.add(day)  # searched just now: the calendar's fare that day is a connection
                elif known is not True:
                    database.set_route_nonstop(AIRLINE, origin, dest, True)
                    known = True
                log.info("F9 %s→%s %s: %d nonstop(s)", origin, dest, day, len(flights))

            if known is None and searched:
                database.set_route_nonstop(AIRLINE, origin, dest, False)
                log.info("F9 %s→%s: %d day(s) searched, no nonstop — treated as connecting-only", origin, dest, searched)
            if known is True:
                for covered, flights in months:
                    flights = [f for f in flights if f.departure_date not in no_nonstop]
                    report.flights_written += persist.save_calendar(self.run_id, origin, dest, covered, flights, airline=AIRLINE)

    async def _attempt(self, report: ScrapeReport, tag: str, fn, *args):
        """One counted search: returns its result, or None after recording the failure."""
        report.attempted += 1
        try:
            result = await self._retry(fn, tag.replace(" ", "-"), *args)
            report.succeeded += 1
            self._blocked_streak = 0
            await self._save_state()
            return result
        except BlockedError as exc:
            self._blocked_streak += 1
            await self._snapshot(tag.replace(" ", "-"), 0)
            report.fail(f"F9 {tag}: blocked ({exc})")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            self._blocked_streak = 0
            report.fail(f"F9 {tag}: {exc}")
        finally:
            await self._polite_delay()
        return None

    async def scrape_calendar_month(self, origin: str, dest: str, anchor: date) -> tuple[list[Flight], list[date]]:
        """The month calendar containing `anchor`: lowest Discount Den total per day."""
        page = self.page
        url = airlines.frontier_search_url(origin, dest, anchor, calendar=True, promo=self.promo_code or "")
        try:
            async with page.expect_response(lambda r: config.FRONTIER_CALENDAR_ENDPOINT in r.url) as info:
                await page.goto(url, wait_until="domcontentloaded")
            payload = await (await info.value).json()
        except PlaywrightTimeout:
            if is_challenge_page(await page.content()):
                raise BlockedError("challenge page instead of the calendar") from None
            raise
        return parse_calendar(payload, origin, dest, member=self.member)

    async def scrape_day(self, origin: str, dest: str, day: date) -> list[Flight]:
        """One day's search: every nonstop with its times and both fares ([] when the day has none)."""
        page = self.page
        await page.goto(airlines.frontier_search_url(origin, dest, day, promo=self.promo_code or ""), wait_until="domcontentloaded")
        try:
            await page.wait_for_function(_FLIGHT_DATA_READY)
        except PlaywrightTimeout:
            if is_challenge_page(await page.content()):
                raise BlockedError("challenge page instead of results") from None
            raise ScrapeError("results page has no FlightDataJson (layout may have changed)") from None
        payload = await page.evaluate(_FLIGHT_DATA_READ)
        if not select_has_flights(payload):
            # A results page with no flight of any kind is not "no nonstop today": every day we search was priced
            # by the calendar, so this is a bad page. Raising makes _attempt count a failure: nothing is deleted
            # and an unknown route is not marked connecting-only. Only a page WITH flights, none nonstop, gives [].
            raise ScrapeError("results page lists no flights at all on a day the calendar priced")
        if self.promo_code:
            self.promo_applied = True
        return parse_select(payload, origin, dest, day, member=self.member)
