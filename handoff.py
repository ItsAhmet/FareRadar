#!/usr/bin/env python
"""Assisted booking handoff.

Opens a *visible* browser, drives Avelo's search form for each leg (promo
applied if given), stops on the results page and leaves the browser to you.
It never selects a fare or books. Runs detached from the dashboard so closing
Streamlit does not close the browser.

    python handoff.py --legs ILG-TPA:2026-10-09,LAL-ILG:2026-10-12 [--promo CODE] [--hold 0]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import date

from playwright.async_api import async_playwright

import config
from scraper import AveloScraper

log = logging.getLogger("handoff")


def parse_legs(spec: str) -> list[tuple[str, str, date]]:
    legs = []
    for item in spec.split(","):
        route, _, day = item.strip().partition(":")
        o, _, d = route.upper().partition("-")
        if not (o and d and day):
            raise SystemExit(f"Bad leg '{item}', expected ORG-DST:YYYY-MM-DD")
        legs.append((o, d, date.fromisoformat(day)))
    return legs


def handoff_command(legs: list[tuple[str, str, date]], promo: str | None = None, python: str | None = None) -> list[str]:
    cmd = [python or sys.executable, str(config.PROJECT_ROOT / "handoff.py"),
           "--legs", ",".join(f"{o}-{d}:{day.isoformat()}" for o, d, day in legs)]
    if promo:
        cmd += ["--promo", promo.upper()]
    return cmd


async def run(legs: list[tuple[str, str, date]], promo: str | None, hold: float) -> int:
    scraper = AveloScraper(headless=False, promo_code=promo, screenshots=False)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=list(config.BROWSER_ARGS))
        await scraper._new_page(browser)
        pages = []
        for i, (origin, dest, day) in enumerate(legs):
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
    p = argparse.ArgumentParser(description="Open Avelo pre-filled for the given legs and hand over.")
    p.add_argument("--legs", required=True, help="Comma list of ORG-DST:YYYY-MM-DD")
    p.add_argument("--promo", help="Promo code to apply on the form")
    p.add_argument("--hold", type=float, default=0, help="Seconds to keep the browser open (0 = until you close it)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    return asyncio.run(run(parse_legs(args.legs), args.promo, args.hold))


if __name__ == "__main__":
    sys.exit(main())
