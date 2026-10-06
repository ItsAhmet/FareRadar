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
itself a tell), cookies and localStorage persisted between runs in
`data/browser_state.json`, `navigator.webdriver` masked, trackers blocked,
and randomised human-like pauses and mouse movement between form actions.

Parsing is split into pure functions that take HTML so it can be unit-tested
against captured fixtures without a browser.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

import config
import database
from models import Flight

log = logging.getLogger(__name__)

Mode = Literal["calendar", "detail", "full"]
LOWFARE_PREFIX = "LOWFARE"  # flight_id prefix for calendar rows (no time known)
DETAIL_PREFIX = "AVELO"  # flight_id prefix for rows parsed from flight cards
SEL = config.SELECTORS

# Third-party trackers the booking app embeds. Blocking them makes pages load
# faster and quieter; none of them affect fares.
_BLOCKED_HOSTS = re.compile(
    r"(tiktok|paypal|paypalobjects|doubleclick|google-analytics|googletagmanager|googleadservices|"
    r"criteo|quantummetric|braintree|slgnt|facebook|bing\.com|clarity\.ms|visualstudio\.com)"
)


class ScrapeError(RuntimeError):
    """A page loaded but did not contain what we expected."""


class DateUnavailable(ScrapeError):
    """The requested day has no selectable cell (sold out or not flown)."""


@dataclass
class ScrapeReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    flights_written: int = 0
    skipped_routes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failed += 1
        self.failures.append(message)
        log.error(message)


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
    """Flight kwargs for price/base_price/is_member_fare, or None if no fare."""
    member, public = _fares(container)
    if member is not None:
        return {"price": member, "base_price": public, "is_member_fare": True}
    if public is not None:
        return {"price": public, "base_price": public, "is_member_fare": False}
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
    cards = soup.select(SEL["flight_card"]) or soup.select(".flight-block")
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
        fare = _price_fields(card.select_one(".fare-prices") or card)
        if fare is None:
            continue
        flights.append(
            Flight(
                origin=origin,
                destination=destination,
                departure_datetime=dep_dt,
                arrival_datetime=arr_dt,
                duration_minutes=duration,
                flight_id=f"{DETAIL_PREFIX}-{origin.upper()}-{destination.upper()}",
                timestamp_scraped=scraped_at,
                **fare,
            )
        )
    return flights


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


# --------------------------------------------------------------------------- #
# Browser driver
# --------------------------------------------------------------------------- #


class AveloScraper:
    def __init__(
        self,
        headless: bool | None = None,
        mode: Mode = "calendar",
        delay_range: tuple[float, float] = config.DELAY_RANGE,
        max_retries: int = config.MAX_RETRIES,
        timeout_ms: int = config.PAGE_TIMEOUT_MS,
        screenshots: bool = True,
        promo_code: str | None = None,
        run_id: int | None = None,
    ) -> None:
        self.headless = config.HEADLESS if headless is None else headless
        self.mode = mode
        self.promo_code = (promo_code or "").strip().upper() or None
        self.promo_applied = False
        self.run_id = run_id  # scrape_runs row to attach sightings to (None = don't record)
        # One fingerprint per run; retries reuse it together with saved cookies.
        self.user_agent = random.choice(config.USER_AGENTS)
        self.viewport = random.choice(config.VIEWPORTS)
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.timeout_ms = timeout_ms
        self.screenshots = screenshots
        self._context: BrowserContext | None = None
        self._page: Page | None = None

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
                targets, report.skipped_routes = plan_targets(routes, wanted, schedule)
                for skipped in report.skipped_routes:
                    log.info("%s: no scheduled flights in range, skipping", skipped)
                for target in targets:
                    origin, dest, days = target.origin, target.destination, list(target.days)
                    log.info("%s→%s: %d scheduled days between %s and %s", origin, dest, len(days), days[0], days[-1])

                    if self.mode in ("calendar", "full"):
                        report.attempted += 1
                        try:
                            flights = await self._retry(self.scrape_lowfare_route, f"{origin}-{dest}-calendar", origin, dest, days)
                            flights = [self._tag(f) for f in flights if f.departure_date in wanted]
                            report.flights_written += database.upsert_flights(flights)
                            if self.run_id is not None:
                                database.record_seen(self.run_id, origin, dest, days, flights)
                            shadowed = database.drop_shadowed_placeholders(origin, dest)
                            if shadowed:
                                log.debug("%s→%s: dropped %d calendar rows shadowed by detail rows", origin, dest, shadowed)
                            report.succeeded += 1
                            await self._save_state()
                            log.info("%s→%s calendar: %d fares", origin, dest, len(flights))
                        except Exception as exc:  # noqa: BLE001 - keep the loop alive
                            report.fail(f"{origin}-{dest} calendar: {exc}")
                        await self._polite_delay()

                    if self.mode in ("detail", "full"):
                        for day in days:
                            report.attempted += 1
                            try:
                                flights = await self._retry(self.scrape_day, f"{origin}-{dest}-{day}", origin, dest, day)
                                database.delete_day(origin, dest, day, flight_id_prefix=LOWFARE_PREFIX)
                                flights = [self._tag(f) for f in flights]
                                report.flights_written += database.upsert_flights(flights)
                                if self.run_id is not None:
                                    database.record_seen(self.run_id, origin, dest, [day], flights)
                                report.succeeded += 1
                                await self._save_state()
                                log.info("%s→%s %s: %d flight(s)", origin, dest, day, len(flights))
                            except Exception as exc:  # noqa: BLE001
                                report.fail(f"{origin}-{dest} {day}: {exc}")
                            await self._polite_delay()
            finally:
                await browser.close()
        return report

    async def _new_page(self, browser: Browser) -> Page:
        """Fresh context with this run's fingerprint, saved cookies, stealth
        init script; trackers and images blocked."""
        if self._context is not None:
            await self._save_state()
            await self._context.close()
        width, height = self.viewport
        state = config.STORAGE_STATE_PATH if config.STORAGE_STATE_PATH.exists() else None
        self._context = await browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": width, "height": height},
            locale="en-US",
            timezone_id="America/New_York",
            storage_state=str(state) if state else None,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self._context.set_default_timeout(self.timeout_ms)
        await self._context.add_init_script(config.STEALTH_INIT_SCRIPT)
        await self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media") or _BLOCKED_HOSTS.search(route.request.url)
            else route.continue_(),
        )
        self._page = await self._context.new_page()
        return self._page

    async def _save_state(self) -> None:
        """Persist cookies/localStorage so the next run looks like a returning visitor."""
        if self._context is None:
            return
        try:
            config.STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            state = await self._context.storage_state()
            # Keep cookies only. Avelo's app caches its route list in
            # localStorage; replaying that would stop it re-fetching the
            # schedule feed we capture on load.
            state["origins"] = []
            config.STORAGE_STATE_PATH.write_text(json.dumps(state))
        except Exception as exc:  # noqa: BLE001 - never fail a scrape over this
            log.debug("could not save browser state: %s", exc)

    def _tag(self, flight: Flight) -> Flight:
        """Record which promo (if any) was active when the fare was observed."""
        code = self.promo_code if self.promo_applied else None
        return dataclasses.replace(flight, promo_code=code) if code != flight.promo_code else flight

    async def _human_pause(self) -> None:
        lo, hi = config.HUMAN_PAUSE_S
        await asyncio.sleep(random.uniform(lo, hi))

    async def _wander(self) -> None:
        """A couple of idle mouse movements, like someone reading the page."""
        page = self.page
        width, height = self.viewport
        try:
            for _ in range(random.randint(1, 3)):
                await page.mouse.move(random.randint(40, width - 40), random.randint(80, height - 80), steps=random.randint(8, 20))
                await asyncio.sleep(random.uniform(0.05, 0.25))
            if random.random() < 0.3:
                await page.mouse.wheel(0, random.randint(60, 240))
        except Exception:  # noqa: BLE001
            pass

    @property
    def page(self) -> Page:
        assert self._page is not None, "scraper not started"
        return self._page

    async def _polite_delay(self, short: bool = False) -> None:
        lo, hi = self.delay_range
        if short:
            lo, hi = lo / 3, hi / 3
        await asyncio.sleep(random.uniform(lo, hi))

    async def _retry(self, fn: Callable[..., Awaitable], tag: str, *args):
        """Run `fn` with retries, exponential backoff and a screenshot per failure."""
        for attempt in range(self.max_retries + 1):
            try:
                return await fn(*args)
            except (PlaywrightTimeout, ScrapeError) as exc:
                await self._snapshot(tag, attempt)
                if attempt >= self.max_retries:
                    raise
                wait = 2 ** attempt * random.uniform(2, 4)
                log.warning("%s attempt %d failed (%s); retrying in %.0fs", tag, attempt + 1, str(exc).splitlines()[0], wait)
                await asyncio.sleep(wait)
                # A fresh page clears any stuck Blazor state.
                await self._new_page(self.page.context.browser)  # type: ignore[arg-type]

    async def _snapshot(self, tag: str, attempt: int) -> None:
        if not self.screenshots or self._page is None:
            return
        try:
            config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = config.SCREENSHOT_DIR / f"{tag}-{attempt}-{datetime.now():%Y%m%d%H%M%S}.png"
            await self._page.screenshot(path=str(path), full_page=True)
            log.debug("saved %s", path)
        except Exception:  # noqa: BLE001 - screenshots are best-effort
            pass

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
            raise DateUnavailable(f"no selectable calendar cell for {day} (sold out or not flown)") from exc
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

    async def scrape_lowfare_route(self, origin: str, dest: str, days: list[date]) -> list[Flight]:
        """Low Fare Calendar for a route: every month that `days` touches."""
        page = self.page
        # The form needs *a* flight day to submit; if the first is sold out try the next.
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
            flights.extend(in_month)
            await self._polite_delay(short=True)
        if not flights:
            raise ScrapeError("calendar rendered but no fares parsed (selectors may have changed)")
        return flights

    async def scrape_day(self, origin: str, dest: str, day: date) -> list[Flight]:
        """Standard search for one day: every flight card with times and fare."""
        page = self.page
        await self._submit_search(origin, dest, day, lowfare=False)
        await page.wait_for_url(re.compile(r"/flight-search/select"))
        await page.wait_for_selector(SEL["flight_card"])
        flights = parse_select_page(await page.content(), origin, dest, day)
        if not flights:
            raise ScrapeError("results rendered but no flight cards parsed (selectors may have changed)")
        return flights
