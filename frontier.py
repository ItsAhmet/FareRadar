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
from browser import BaseScraper, BlockedError, EmptyResults, ScrapeError, ScrapeReport, answerable_days, local_today
from models import Flight
from netutil import open_url

log = logging.getLogger(__name__)

AIRLINE = "F9"
_F9 = airlines.get(AIRLINE)
# UNVERIFIED: no real Frontier challenge page has been captured (the 2026-10-06 spike saw none on 5 loads), so these
# are guesses and tests/fixtures/frontier/ has no challenge fixture. A missed marker is safe for the data (the page
# raises a plain ScrapeError: nothing deleted, no sighting) but the run no longer stops after two blocks. When a
# challenge is seen live (plan Task 9 Step 2), save its HTML as a fixture and correct this tuple.
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


def check_select(payload: dict, origin: str, destination: str, day: date) -> None:
    """Raise unless the day-search payload says it answers for this route on this day.

    `parse_select` drops flights of another route or day, so a page for the wrong search (a redirect to a
    default search, a late page for the previous one) would otherwise read as "no nonstop that day": the
    stored fare deleted and an unknown route marked connecting-only. A payload that does not name its
    route and day proves nothing, so it is rejected too.
    """
    origin, destination = origin.strip().upper(), destination.strip().upper()
    journeys = (payload or {}).get("journeys") or []
    journey = journeys[0] if journeys and isinstance(journeys[0], dict) else {}
    stations = (_F9.app_code(journey.get("departureStation") or ""), _F9.app_code(journey.get("arrivalStation") or ""))
    try:
        answered = datetime.fromisoformat(journey["departureDate"]).date()
    except (KeyError, TypeError, ValueError):
        answered = None
    if stations != (origin, destination) or answered != day:
        raise ScrapeError(f"Frontier results are for {stations[0] or '?'}-{stations[1] or '?'} on {answered or '?'}, "
                          f"not {origin}-{destination} on {day}")


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
    """Where to look for a nonstop among calendar placeholders: up to `limit` days, in date order, spread over
    as many weekdays as possible (the cheapest day of each weekday first, then the cheapest days left).

    The calendar's lowest fare is often a connection (MCO→PHL: connections from $139, nonstops from $204), so
    the cheapest days overall can all be connection-only on a route flown nonstop a few days a week.
    Schedules repeat weekly; one day per weekday sees every such route.
    """
    cheapest: dict[date, float] = {}
    for f in flights:
        cheapest[f.departure_date] = min(f.price, cheapest.get(f.departure_date, float("inf")))
    by_price = sorted(cheapest, key=lambda d: (cheapest[d], d))
    per_weekday: dict[int, date] = {}
    for d in by_price:
        per_weekday.setdefault(d.weekday(), d)
    first = list(per_weekday.values())                       # already cheapest-first
    rest = [d for d in by_price if d not in set(first)]
    return sorted((first + rest)[:limit])


def no_nonstop_proven(planned: Iterable[date], searched: Iterable[date], priced: Iterable[date] = (),
                      answered: Iterable[date] = ()) -> bool:
    """Is "this route has no nonstop" safe to record (the route is then skipped for a month)?

    Only when every planned day search answered and, between them, all seven weekdays are accounted for:
    searched, or shown by the calendar (`answered` days with no `priced` day on that weekday) to have no
    fare of any kind. Anything less (a failed probe, a request shorter than a week) can miss a route
    flown a few days a week, so the route stays unknown and is probed again on the next run.
    """
    def weekdays(days: Iterable[date]) -> set[int]:
        return {d.weekday() for d in days}

    planned = set(planned)
    if not planned or planned - set(searched):
        return False
    return len(weekdays(planned) | (weekdays(answered) - weekdays(priced))) == 7


# --------------------------------------------------------------------------- #
# Route catalog
# --------------------------------------------------------------------------- #


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENTS[0], "Accept-Language": "en-US,en;q=0.9"})
    with open_url(request, 20) as response:
        return response.read().decode("utf-8", "replace")


def refresh_catalog(origins: Iterable[str], conn=None, max_age_days: int = config.CATALOG_REFRESH_DAYS, now: datetime | None = None,
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
        # The code travels in the URL of every search, calendar and day alike, so every fare this run
        # observes was observed under it. (Avelo applies its code in a form step and sets this there.)
        self.promo_applied = bool(self.promo_code)
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

            searched: set[date] = set()
            found_nonstop = False
            for day in days:
                if self._blocked_streak >= 2:
                    break
                flights = await self._attempt(report, f"{origin}-{dest} {day}", self.scrape_day, origin, dest, day)
                if flights is None:
                    continue
                searched.add(day)
                flights = [self._tag(f) for f in flights]
                report.flights_written += persist.save_day(self.run_id, origin, dest, day, flights, airline=AIRLINE)
                if flights and known is not True:
                    database.set_route_nonstop(AIRLINE, origin, dest, True)
                    known = True
                found_nonstop = found_nonstop or bool(flights)
                log.info("F9 %s→%s %s: %d nonstop(s)", origin, dest, day, len(flights))
                if flights and self.mode == "calendar":
                    break  # the probes only ask "is there a nonstop?"; it is answered

            if known is None and searched and not found_nonstop:
                priced = {f.departure_date for f in calendar_flights}
                answered = {d for covered, _flights in months for d in covered}
                if no_nonstop_proven(days, searched, priced, answered):
                    database.set_route_nonstop(AIRLINE, origin, dest, False)
                    log.info("F9 %s→%s: %d day(s) searched, no nonstop — treated as connecting-only", origin, dest, len(searched))
                else:
                    log.info("F9 %s→%s: no nonstop on %d of %d planned day(s); not enough to rule the route out, "
                             "it will be probed again", origin, dest, len(searched), len(days))
            if known is True:
                # A day searched in this run already has its answer from save_day: its real nonstops (or none) and
                # the matching sighting. The calendar's fare for that day may be a connection, so the day is left
                # out of the calendar save entirely; otherwise its sighting would be replaced by the calendar price.
                for covered, flights in months:
                    covered = [d for d in covered if d not in searched]
                    flights = [f for f in flights if f.departure_date not in searched]
                    report.flights_written += persist.save_calendar(self.run_id, origin, dest, covered, flights, airline=AIRLINE)

        # Checked after the loop, not at the top of the next route: the two blocks may fall on the last or only route.
        if self._blocked_streak >= 2:
            report.blocked = True
            log.error("Frontier is challenging us; stopping this airline for the run")

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
            flights, answered = parse_calendar(payload, origin, dest, member=self.member)
        except Exception:
            # No usable calendar: the request never came, or its answer was not calendar JSON (the bot wall answers
            # the XHR too). If the page shows the challenge this is a block, which must count towards stopping the run.
            if await self._challenged():
                raise BlockedError("challenge page instead of the calendar") from None
            raise
        if not flights and await self._challenged():
            raise BlockedError("challenge page instead of the calendar")
        return flights, answered

    async def _challenged(self) -> bool:
        """Is the page showing a bot challenge right now? (False when the page cannot be read.)"""
        try:
            return is_challenge_page(await self.page.content())
        except Exception:  # noqa: BLE001 - mid-navigation or closed page: cannot tell, so not a block
            return False

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
            # A results page with no flight of any kind is not taken as "no nonstop today". Raising makes _attempt
            # count a failure: nothing is deleted and an unknown route is not marked connecting-only. Only a page
            # WITH flights, none nonstop, gives [].
            if self.mode == "detail":
                # No calendar ran, so the day may simply not be on sale; the same page would come back: no retry.
                raise EmptyResults("results page lists no flights at all (the day may not be on sale); nothing changed")
            raise ScrapeError("results page lists no flights at all on a day the calendar priced")
        check_select(payload, origin, dest, day)  # a page for another route or day is not an answer either
        return parse_select(payload, origin, dest, day, member=self.member)
