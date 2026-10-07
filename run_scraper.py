#!/usr/bin/env python
"""Command-line entry point for the scraping loop.

The dashboard's "Execute Target Scrape" button builds exactly this command from
the sidebar filters (see `build_command`), so only the requested destinations
and dates are ever visited.

Examples
--------
    python run_scraper.py                                  # every home-airport route, next 60 days
    python run_scraper.py --destinations TPA,LAL --days 30 # two destinations, both directions
    python run_scraper.py --destinations TPA --origins ILG,PHL # only these home airports
    python run_scraper.py --start 2026-10-01 --end 2026-11-15 --mode full
    python run_scraper.py --routes ILG-CHS --headful       # one direction, watch the browser
    python run_scraper.py --mock                           # synthetic data, no network
    python run_scraper.py --destinations CHS --promo SAVE20 # apply a promo code on every search
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

import alerts
import config
import database
import runlock
import signals
from mock_data import generate_mock_flights, scheduled_days


def parse_routes(spec: str | None) -> list[tuple[str, str]]:
    if not spec:
        return config.default_routes()
    routes = []
    for item in spec.split(","):
        o, _, d = item.strip().upper().partition("-")
        if not (o and d):
            raise SystemExit(f"Bad route '{item}', expected ORG-DST")
        routes.append((o, d))
    return routes


def parse_destinations(spec: str | None) -> list[str]:
    codes = [c.strip().upper() for c in (spec or "").split(",") if c.strip()]
    unknown = [c for c in codes if c not in config.DESTINATIONS]
    if unknown:
        raise SystemExit(f"Unknown destination(s): {', '.join(unknown)}")
    return codes


def parse_origins(spec: str | None) -> list[str]:
    if not spec:
        return list(config.HOME_CODES)
    codes = [c.strip().upper() for c in spec.split(",") if c.strip()]
    unknown = [c for c in codes if c not in config.HOME_AIRPORT_INFO]
    if unknown:
        raise SystemExit(f"Unknown home airport(s): {', '.join(unknown)}")
    return list(dict.fromkeys(codes))


def build_dates(start: date, end: date | None, days: int | None) -> list[date]:
    """Inclusive date list from --start to --end, or --days long."""
    if end is not None:
        if end < start:
            raise SystemExit("--end must be on or after --start")
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [start + timedelta(days=i) for i in range(days or config.DAYS_AHEAD)]


def build_command(
    destinations: list[str],
    start: date,
    end: date,
    mode: str = "calendar",
    purge: bool = True,
    mock: bool = False,
    headful: bool = False,
    python: str | None = None,
    promo: str | None = None,
    origins: list[str] | None = None,
) -> list[str]:
    """argv for a targeted run. Used by the dashboard; tested so the UI's
    filters provably reach the scraper unchanged."""
    cmd = [
        python or sys.executable,
        str(config.PROJECT_ROOT / "run_scraper.py"),
        "--destinations", ",".join(c.upper() for c in destinations),
        "--start", start.isoformat(),
        "--end", end.isoformat(),
        "--mode", mode,
    ]
    if origins:
        cmd += ["--origins", ",".join(o.upper() for o in origins)]
    if purge:
        cmd.append("--purge")
    if mock:
        cmd.append("--mock")
    if headful:
        cmd.append("--headful")
    if promo and promo.strip():
        cmd += ["--promo", promo.strip().upper()]
    return cmd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scrape Avelo fares from your home airports (AVELO_HOME_AIRPORTS) into SQLite.")
    p.add_argument("--start", type=date.fromisoformat, default=date.today(), help="First departure date (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, help="Last departure date, inclusive (overrides --days)")
    p.add_argument("--days", type=int, default=None, help=f"How many days to cover (default {config.DAYS_AHEAD})")
    p.add_argument("--destinations", help="Comma list of destination codes, e.g. TPA,LAL (both directions)")
    p.add_argument("--routes", help="Comma list like ILG-TPA,TPA-ILG (overrides --destinations)")
    p.add_argument("--origins", help="Comma list of home airports, e.g. ILG,PHL (default: AVELO_HOME_AIRPORTS)")
    p.add_argument(
        "--mode",
        choices=["calendar", "detail", "full"],
        default="calendar",
        help="calendar: lowest fare per day (fast). detail: per-day search with times (slow). full: both",
    )
    p.add_argument("--headful", action="store_true", help="Show the browser window (overrides AVELO_HEADLESS)")
    p.add_argument("--mock", action="store_true", help="Write synthetic flights instead of scraping")
    p.add_argument("--mock-seed", type=int, default=42, help="Seed for --mock prices")
    p.add_argument("--mock-sale", type=float, default=0.0, help="With --mock: cut every fare by this fraction (0.2 = 20%% sale)")
    p.add_argument("--no-alerts", action="store_true", help="Skip signal detection and notifications after the run")
    p.add_argument("--purge", action="store_true", help="(always on) delete departed flights and prune history first")
    p.add_argument("--promo", help="Promo code to apply on Avelo's search form for every search")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    log = logging.getLogger("run_scraper")
    if not runlock.acquire():
        log.error("another scrape is already running (pid %s); not starting", runlock.holder_pid())
        return runlock.BUSY_EXIT
    try:
        return _run(args, log)
    finally:
        runlock.release()


def _run(args: argparse.Namespace, log: logging.Logger) -> int:
    database.init_db()
    housekeeping = database.maintenance()
    log.info("maintenance: %s", ", ".join(f"{k}={v}" for k, v in housekeeping.items()))

    if args.routes:
        routes = parse_routes(args.routes)
    else:
        routes = config.default_routes(parse_destinations(args.destinations) or None, parse_origins(args.origins))
    dates = build_dates(args.start, args.end, args.days)
    log.info("routes=%d (%s) dates=%s..%s", len(routes), ",".join(f"{o}-{d}" for o, d in routes), dates[0], dates[-1])

    run_id = database.start_run("mock" if args.mock else args.mode, routes, args.promo)
    summary: dict = {}
    status, exit_code = "failed", 1
    try:
        if args.mock:
            flights = generate_mock_flights(dates[0], len(dates), routes, seed=args.mock_seed, discount=args.mock_sale)
            written = database.upsert_flights(flights)
            for origin, dest in routes:
                if config.PRIMARY_HOME in (origin, dest):  # the mock schedule only flies PRIMARY_HOME routes
                    database.record_seen(run_id, origin, dest, scheduled_days(origin, dest, dates), flights)
            log.info("mock mode: wrote %d flights", written)
            summary = {"flights_written": written, "mock": True}
            status, exit_code = "ok", 0
        else:
            from scraper import AveloScraper  # imported lazily so --mock works without Playwright

            scraper = AveloScraper(headless=not args.headful and config.HEADLESS, mode=args.mode, promo_code=args.promo, run_id=run_id)
            started = datetime.now()
            report = asyncio.run(scraper.run(routes, dates))
            log.info(
                "done in %s: %d flights written, %d/%d searches ok, %d failed, %d routes skipped (not scheduled)",
                datetime.now() - started, report.flights_written, report.succeeded, report.attempted,
                report.failed, len(report.skipped_routes),
            )
            if report.skipped_routes:
                log.info("skipped (Avelo has no flights in range): %s", ", ".join(report.skipped_routes))
            for failure in report.failures:
                log.warning("failed: %s", failure)
            summary = {
                "flights_written": report.flights_written, "attempted": report.attempted, "succeeded": report.succeeded,
                "failed": report.failed, "skipped": report.skipped_routes, "failures": report.failures,
            }
            if report.failed == 0:
                status, exit_code = "ok", 0
            else:
                status, exit_code = ("partial" if report.succeeded else "failed"), 1

        if not args.no_alerts:
            signal_events = signals.run_detection(run_id, allow_sold_out=(status == "ok"))
            summary["signals"] = len(signal_events)
            summary["alerts"] = alerts.evaluate_and_deliver(run_id, signal_events)
    except Exception as exc:  # noqa: BLE001 - always close the run row
        log.exception("scrape crashed: %s", exc)
        summary["error"] = repr(exc)[:500]
        status, exit_code = "failed", 1
    finally:
        database.finish_run(run_id, summary, status=status)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
