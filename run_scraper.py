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
    python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full
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
    unknown = [c for c in codes if c not in config.DESTINATIONS and c not in config.FRONTIER_AIRPORTS]
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


def parse_airlines(spec: str | None) -> list[str]:
    if not spec:
        return list(config.ACTIVE_AIRLINES)
    codes = [c.strip().upper() for c in spec.split(",") if c.strip()]
    unknown = [c for c in codes if c not in config.KNOWN_AIRLINES]
    if unknown:
        raise SystemExit(f"Unknown airline(s): {', '.join(unknown)} (known: {', '.join(config.KNOWN_AIRLINES)})")
    return list(dict.fromkeys(codes))


def parse_promos(values: list[str] | None) -> dict[str, str]:
    """--promo CODE applies to Avelo; --promo F9=CODE to that airline."""
    promos: dict[str, str] = {}
    for value in values or []:
        airline, sep, code = value.strip().partition("=")
        airline, code = (airline.upper(), code) if sep else ("XP", airline)
        if airline not in config.KNOWN_AIRLINES:
            raise SystemExit(f"Unknown airline in --promo: {value}")
        if code.strip():
            promos[airline] = code.strip().upper()
    return promos


def scraper_class(airline: str):
    """Imported lazily so --mock works without Playwright."""
    if airline == "F9":
        from frontier import FrontierScraper
        return FrontierScraper
    from scraper import AveloScraper
    return AveloScraper


def can_fly(airline: str, route: tuple[str, str]) -> bool:
    """Could this airline serve the route at all? Avelo: one end is one of its destinations.
    Frontier: one end is a home airport Frontier flies from (the other end may be off-catalog)."""
    served = config.FRONTIER_ROUTE_PAGES if airline == "F9" else config.DESTINATIONS
    return any(code in served for code in route)


def routes_for(airline: str, args: argparse.Namespace, airlines: list[str] | None = None) -> list[tuple[str, str]]:
    """The directional routes one airline's scraper should visit for this request. `airlines` is every
    airline in the request: with one, --routes is taken as given; with several, each airline gets only
    the listed routes it could fly, so one airline's route is not a failed search for the other."""
    if args.routes:
        routes = parse_routes(args.routes)
        if airlines and len(airlines) > 1:
            return [r for r in routes if can_fly(airline, r)]
        return routes
    destinations = parse_destinations(args.destinations) or None
    origins = parse_origins(args.origins)
    if airline == "F9":
        import frontier

        frontier.refresh_catalog(origins)
        return database.catalog_routes("F9", origins=origins, destinations=destinations)
    avelo = [d for d in destinations if d in config.DESTINATIONS] if destinations else None
    if destinations and not avelo:
        return []
    return config.default_routes(avelo, origins)


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
    airlines: list[str] | None = None,
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
    if airlines:
        cmd += ["--airlines", ",".join(a.upper() for a in airlines)]
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
    p = argparse.ArgumentParser(description="Scrape fares from your home airports (AVELO_HOME_AIRPORTS) into SQLite.")
    p.add_argument("--start", type=date.fromisoformat, default=date.today(), help="First departure date (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, help="Last departure date, inclusive (overrides --days)")
    p.add_argument("--days", type=int, default=None, help=f"How many days to cover (default {config.DAYS_AHEAD})")
    p.add_argument("--destinations", help="Comma list of destination codes, e.g. TPA,LAL (both directions)")
    p.add_argument("--routes", help="Comma list like ILG-TPA,TPA-ILG (overrides --destinations)")
    p.add_argument("--origins", help="Comma list of home airports, e.g. ILG,PHL (default: AVELO_HOME_AIRPORTS)")
    p.add_argument("--airlines", help="Comma list of airline codes: XP (Avelo), F9 (Frontier). Default: AVELO_AIRLINES")
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
    p.add_argument("--promo", action="append", help="Promo code for every search: CODE (Avelo) or F9=CODE; repeatable")
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
    dates = build_dates(args.start, args.end, args.days)
    promos = parse_promos(args.promo)
    airline_codes = ["XP"] if args.mock else parse_airlines(args.airlines)  # the mock schedule is Avelo's

    work = [(code, routes_for(code, args, airline_codes)) for code in airline_codes]
    for code, routes in work:
        if not routes:
            log.info("%s: no routes for this request, skipping", code)
    work = [(code, routes) for code, routes in work if routes]

    exit_code, signal_events, run_ids = 0, [], []
    for code, routes in work:
        log.info("%s: routes=%d (%s) dates=%s..%s", code, len(routes), ",".join(f"{o}-{d}" for o, d in routes), dates[0], dates[-1])
        exit_code = max(exit_code, _run_airline(code, routes, dates, promos.get(code), args, log, signal_events, run_ids))

    # Alerts once per invocation, outside every airline's try block: a crash in one airline's scrape
    # must not cost the other airlines' signals or the watch/rule/booking checks.
    if run_ids and not args.no_alerts:
        try:
            database.annotate_run(run_ids[-1], {"alerts": alerts.evaluate_and_deliver(run_ids[-1], signal_events)})
        except Exception as exc:  # noqa: BLE001 - the scrape results are already stored
            log.exception("alerts failed: %s", exc)
            database.annotate_run(run_ids[-1], {"alerts_error": repr(exc)[:500]})
            exit_code = 1
    return exit_code


def _run_airline(code: str, routes: list[tuple[str, str]], dates: list[date], promo: str | None,
                 args: argparse.Namespace, log: logging.Logger, signal_events: list, run_ids: list[int]) -> int:
    """One airline's scrape = one scrape_runs row; its run id is appended to `run_ids` and its signal
    events to `signal_events`. Alerts are evaluated by `_run`, once, after every airline."""
    run_id = database.start_run("mock" if args.mock else args.mode, routes, promo, airline=code)
    run_ids.append(run_id)
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
            scraper = scraper_class(code)(headless=not args.headful and config.HEADLESS, mode=args.mode,
                                          promo_code=promo, run_id=run_id)
            started = datetime.now()
            report = asyncio.run(scraper.run(routes, dates))
            log.info(
                "%s done in %s: %d flights written, %d/%d searches ok, %d failed, %d routes skipped (not scheduled)",
                code, datetime.now() - started, report.flights_written, report.succeeded, report.attempted,
                report.failed, len(report.skipped_routes),
            )
            if report.skipped_routes:
                log.info("skipped (no flights in range): %s", ", ".join(report.skipped_routes))
            for failure in report.failures:
                log.warning("failed: %s", failure)
            summary = {
                "flights_written": report.flights_written, "attempted": report.attempted, "succeeded": report.succeeded,
                "failed": report.failed, "skipped": report.skipped_routes, "failures": report.failures,
                "blocked": report.blocked,
            }
            if report.failed == 0 and not report.blocked:
                status, exit_code = "ok", 0
            else:
                status, exit_code = ("partial" if report.succeeded else "failed"), 1

        if not args.no_alerts:
            events = signals.run_detection(run_id, allow_sold_out=(status == "ok"))
            signal_events.extend(events)
            summary["signals"] = len(events)
    except Exception as exc:  # noqa: BLE001 - always close the run row
        log.exception("%s scrape crashed: %s", code, exc)
        summary["error"] = repr(exc)[:500]
        status, exit_code = "failed", 1
    finally:
        database.finish_run(run_id, summary, status=status)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
