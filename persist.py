"""Write one search result to SQLite consistently (shared by every scraper).

A search answers "what fares exist for these route-days right now". Anything
we stored earlier for a covered day that the search no longer shows is gone
(sold out, retimed, cancelled) and is deleted, so packages, rules and watches
never offer a fare that cannot be booked. Days the search did not cover (a
month that failed to render, or rendered without one parsed fare) are left
untouched and not recorded.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import date

import database
from models import Flight

log = logging.getLogger(__name__)


def save_calendar(run_id: int | None, origin: str, dest: str, covered_days: Iterable[date], flights: list[Flight],
                  conn: sqlite3.Connection | None = None) -> int:
    """Calendar result: one lowest fare per priced day; covered days without a fare are cleared.

    `covered_days` must hold only days the search positively answered for. A month
    that rendered but parsed to zero fares is not an answer, so callers leave it out.
    """
    covered = sorted(set(covered_days))
    priced = {f.departure_date for f in flights}
    written = database.upsert_flights(flights, conn)
    for day in covered:
        if day not in priced:
            removed = database.delete_day(origin, dest, day, conn=conn)
            if removed:
                log.info("%s→%s %s: no longer offered, removed %d stored fare(s)", origin, dest, day, removed)
    if run_id is not None and covered:
        database.record_seen(run_id, origin, dest, covered, flights, conn)
    database.drop_shadowed_placeholders(origin, dest, conn)
    return written


def save_day(run_id: int | None, origin: str, dest: str, day: date, flights: list[Flight],
             conn: sqlite3.Connection | None = None) -> int:
    """Detail result for one day: it replaces every stored row for that day."""
    database.delete_day(origin, dest, day, conn=conn)
    written = database.upsert_flights(flights, conn)
    if run_id is not None:
        database.record_seen(run_id, origin, dest, [day], flights, conn)
    return written
