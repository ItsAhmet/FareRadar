"""Write one search result to SQLite consistently (shared by every scraper).

A search answers "what fares exist for these route-days right now". Anything
we stored earlier for a covered day that the search no longer shows is gone
(sold out, retimed, cancelled) and is deleted, so packages, rules and watches
never offer a fare that cannot be booked. Days the search did not cover (a
month that failed to render, or rendered without one parsed fare) are left
untouched and not recorded. Today and past days are never covered: Avelo shows
today as a disabled calendar cell even while its flights are bookable.

Days Avelo's schedule (routes/all) no longer lists are cleared by
`clear_unscheduled` without a sighting: nothing was searched, so they are not
reported as sold out, but a fare Avelo does not fly is never offered.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from datetime import date

import airlines
import database
from models import Flight

log = logging.getLogger(__name__)


def owner_priced(flights: Iterable[Flight], conn: sqlite3.Connection | None = None) -> list[Flight]:
    """The flights with `price` and `is_member_fare` set by the owner's current membership in each airline.

    A parser reports the fares the site shows; what the owner pays depends on membership (airlines.owner_price),
    which can change between scrapes, so every save applies it. Flights with no fare at all are left as parsed.
    """
    member = database.memberships(conn)
    out = []
    for f in flights:
        if f.member_price is None and f.base_price is None:
            out.append(f)
            continue
        price, is_member = airlines.owner_price(member.get(f.airline, False), f.member_price, f.base_price)
        out.append(f if (price, is_member) == (f.price, f.is_member_fare) else replace(f, price=price, is_member_fare=is_member))
    return out


def save_calendar(run_id: int | None, origin: str, dest: str, covered_days: Iterable[date], flights: list[Flight],
                  conn: sqlite3.Connection | None = None, airline: str = "XP") -> int:
    """Calendar result: one lowest fare per priced day; covered days without a fare are cleared.

    `covered_days` must hold only days the search positively answered for. A month
    that rendered but parsed to zero fares is not an answer, so callers leave it out.
    """
    covered = sorted(set(covered_days))
    flights = owner_priced(flights, conn)
    priced = {f.departure_date for f in flights}
    written = database.upsert_flights(flights, conn)
    for day in covered:
        if day not in priced:
            removed = database.delete_day(origin, dest, day, conn=conn, airline=airline)
            if removed:
                log.info("%s→%s %s: no longer offered, removed %d stored fare(s)", origin, dest, day, removed)
    if run_id is not None and covered:
        database.record_seen(run_id, origin, dest, covered, flights, conn)
    database.drop_shadowed_placeholders(origin, dest, conn, airline=airline)
    return written


def save_day(run_id: int | None, origin: str, dest: str, day: date, flights: list[Flight],
             conn: sqlite3.Connection | None = None, airline: str = "XP") -> int:
    """Detail result for one day: it replaces every stored row for that day."""
    flights = owner_priced(flights, conn)
    database.delete_day(origin, dest, day, conn=conn, airline=airline)
    written = database.upsert_flights(flights, conn)
    if run_id is not None:
        database.record_seen(run_id, origin, dest, [day], flights, conn)
    return written


def clear_unscheduled(origin: str, dest: str, days: Iterable[date], conn: sqlite3.Connection | None = None,
                      airline: str = "XP") -> int:
    """Delete one airline's stored fares on route-days its schedule no longer lists. Records no sighting."""
    days = sorted(set(days))
    removed = database.delete_days(origin, dest, days, conn, airline=airline)
    if removed:
        log.info("%s→%s: %d stored fare(s) on days no longer in the airline's schedule removed (%s..%s)",
                 origin, dest, removed, days[0], days[-1])
    return removed
