#!/usr/bin/env python
"""Assisted booking handoff.

Avelo legs are driven in a *visible* browser: the search form is filled (promo
applied if given) and the browser stops on the results page and is left to you.
Other airlines' legs (Frontier) open as links in your default browser, since
their sites take the search in the URL. It never selects a fare or books. Runs
detached from the dashboard so closing Streamlit does not close the browser.

    python handoff.py --legs ILG-TPA:2026-10-09,LAL-ILG:2026-10-12:XP [--promo CODE] [--hold 0]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import webbrowser
from datetime import date

from playwright.async_api import async_playwright

import airlines
import config
from scraper import AveloScraper

log = logging.getLogger("handoff")


Leg = tuple[str, str, date, str]  # origin, destination, day, airline code


def parse_legs(spec: str) -> list[Leg]:
    legs = []
    for item in spec.split(","):
        route, _, rest = item.strip().partition(":")
        day, _, airline = rest.partition(":")
        o, _, d = route.upper().partition("-")
        code = (airline or "XP").strip().upper()
        if not (o and d and day):
            raise SystemExit(f"Bad leg '{item}', expected ORG-DST:YYYY-MM-DD[:AIRLINE]")
        if code not in airlines.AIRLINES:
            raise SystemExit(f"Unknown airline in leg '{item}'")
        legs.append((o, d, date.fromisoformat(day), code))
    return legs


def handoff_command(legs: list[Leg], promo: str | None = None, python: str | None = None) -> list[str]:
    cmd = [python or sys.executable, str(config.PROJECT_ROOT / "handoff.py"),
           "--legs", ",".join(f"{o}-{d}:{day.isoformat()}:{code}" for o, d, day, code in legs)]
    if promo:
        cmd += ["--promo", promo.upper()]
    return cmd


def split_legs(legs: list[Leg]) -> tuple[list[tuple[str, str, date]], list[str]]:
    """(legs for the assisted Avelo browser, booking links for airlines whose site takes URL parameters)."""
    assisted, urls = [], []
    for o, d, day, code in legs:
        url = airlines.get(code).booking_url(o, d, day)
        if url is None:
            assisted.append((o, d, day))
        else:
            urls.append(url)
    return assisted, urls


def open_urls(urls: list[str], opener=None) -> int:
    opener = opener or webbrowser.open
    for url in urls:
        opener(url)
        log.info("opened %s", url.split("?")[0])
    return len(urls)


async def run(legs: list[Leg], promo: str | None, hold: float) -> int:
    assisted, urls = split_legs(legs)
    open_urls(urls)
    if not assisted:
        return 0
    scraper = AveloScraper(headless=False, promo_code=promo, screenshots=False)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=list(config.BROWSER_ARGS))
        await scraper._new_page(browser)
        pages = []
        for i, (origin, dest, day) in enumerate(assisted):
            if i > 0:
                scraper._page = await scraper._context.new_page()
            page = scraper.page
            try:
                await scraper._submit_search(origin, dest, day, lowfare=False)
                await page.wait_for_url(re.compile(r"/flight-search/select"), timeout=scraper.timeout_ms)
                await page.wait_for_selector(config.SELECTORS["flight_card"], timeout=scraper.timeout_ms)
                log.info("%s→%s %s: results ready — pick your fare in the browser", origin, dest, day)
            except Exception as exc:  # noqa: BLE001 - leave the tab where it got to; the user takes over
                log.warning("%s→%s %s: could not reach results (%s); tab left on the form", origin, dest, day, str(exc).splitlines()[0])
            pages.append(page)
        await pages[0].bring_to_front()
        await scraper._save_state()
        if hold > 0:
            await asyncio.sleep(hold)
        else:
            while any(not p.is_closed() for p in pages):
                await asyncio.sleep(1)
        await browser.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Open each leg on its airline's site (Avelo pre-filled in an assisted browser) and hand over.")
    p.add_argument("--legs", required=True, help="Comma list of ORG-DST:YYYY-MM-DD[:AIRLINE] (airline defaults to XP)")
    p.add_argument("--promo", help="Promo code to apply on Avelo's form")
    p.add_argument("--hold", type=float, default=0, help="Seconds to keep the browser open (0 = until you close it)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    return asyncio.run(run(parse_legs(args.legs), args.promo, args.hold))


if __name__ == "__main__":
    sys.exit(main())
