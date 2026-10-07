"""Browser plumbing shared by every airline scraper.

One fingerprint per run, cookies kept per airline, stealth init script,
trackers blocked, human pauses, retries with a snapshot per failure. Airline
scrapers subclass `BaseScraper` and add only their site's pages and parsers.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

import config
from models import Flight

log = logging.getLogger(__name__)

Mode = Literal["calendar", "detail", "full"]

# Third-party trackers the booking app embeds. Blocking them makes pages load
# faster and quieter; none of them affect fares.
BLOCKED_HOSTS = re.compile(
    r"(tiktok|paypal|paypalobjects|doubleclick|google-analytics|googletagmanager|googleadservices|"
    r"criteo|quantummetric|braintree|slgnt|facebook|bing\.com|clarity\.ms|visualstudio\.com)"
)


class ScrapeError(RuntimeError):
    """A page loaded but did not contain what we expected."""


class DateUnavailable(ScrapeError):
    """Avelo answered that the requested day has no flights: the picker offers no cell for it, or the results page
    says "no flights available". An answer, not a failure: it is not retried and the day is recorded as no-fare."""


class BlockedError(ScrapeError):
    """The site answered with a bot challenge instead of the page. Not retried within a run."""


@dataclass
class ScrapeReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    flights_written: int = 0
    skipped_routes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    blocked: bool = False  # the site challenged us; the rest of this airline was skipped

    def fail(self, message: str) -> None:
        self.failed += 1
        self.failures.append(message)
        log.error(message)


def local_today() -> date:
    """Today on the machine running the scrape (the owner's Mac, America/New_York). Tests replace it."""
    return date.today()


def answerable_days(days: Iterable[date], wanted: Iterable[date], today: date) -> list[date]:
    """The days a search can positively answer for: requested, and after today.

    Avelo's Low Fare Calendar renders today as a disabled cell with no fare
    (`td.day.today.disabled`) even while today's later flights are still
    bookable, and a past day is never offered. Neither is "no fare": treating
    it as one would delete today's stored fares and push sold-out and
    watch-unavailable alerts on the morning of travel.
    """
    wanted = set(wanted)
    return sorted(d for d in set(days) if d in wanted and d > today)


class BaseScraper:
    airline: str = "XP"

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

    @property
    def state_path(self) -> Path:
        return config.storage_state_path(self.airline)

    def _usable_state(self) -> str | None:
        """Path of this airline's saved cookies, or None when there are none or the file is damaged."""
        path = self.state_path
        legacy = config.STORAGE_STATE_PATH  # pre-phase-2 Avelo cookies: adopt them once
        if self.airline == "XP" and not path.exists() and legacy.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy, path)
        if not path.exists():
            return None
        try:
            json.loads(path.read_text())
        except (OSError, ValueError):
            log.warning("ignoring unreadable browser state %s", path.name)
            return None
        return str(path)

    async def _save_state(self) -> None:
        """Persist cookies so the next run looks like a returning visitor."""
        if self._context is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            state = await self._context.storage_state()
            # Cookies only. Avelo's app caches its route list in localStorage; replaying
            # that would stop it re-fetching the schedule feed we capture on load.
            state["origins"] = []
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)  # atomic: a crash never leaves half a file
        except Exception as exc:  # noqa: BLE001 - never fail a scrape over this
            log.debug("could not save browser state: %s", exc)

    async def _new_page(self, browser: Browser) -> Page:
        """Fresh context with this run's fingerprint, saved cookies, stealth
        init script; trackers and images blocked."""
        if self._context is not None:
            await self._save_state()
            await self._context.close()
        width, height = self.viewport
        state = self._usable_state()
        self._context = await browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": width, "height": height},
            locale="en-US",
            timezone_id="America/New_York",
            storage_state=state,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self._context.set_default_timeout(self.timeout_ms)
        await self._context.add_init_script(config.STEALTH_INIT_SCRIPT)
        await self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media") or BLOCKED_HOSTS.search(route.request.url)
            else route.continue_(),
        )
        self._page = await self._context.new_page()
        return self._page
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
            except (DateUnavailable, BlockedError):
                raise  # an answer or a block: retrying cannot help
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
            path.with_suffix(".html").write_text(await self._page.content())  # the page itself, for a new fixture
            log.debug("saved %s", path)
        except Exception:  # noqa: BLE001 - screenshots are best-effort
            pass
