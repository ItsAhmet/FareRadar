"""Fare signals derived from consecutive scrape runs.

Each run records, per route, the lowest fare seen on every scheduled day (or
NULL when the day was not selectable). Comparing a run with the previous run
of the same route yields:

sale_wave   many days on the route dropped at once (a sale is on)
sold_out    a day that was priced last time is no longer selectable
new_floor   the route hit a lower fare than ever recorded
floor_fare  a day sits at the route's lowest-ever fare (informational)
floor_gone  a day that was at the floor moved up (cheap seats gone)

Pure functions here; `run_detection` is the thin DB wrapper.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import config
import database
from config import airport_name

log = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str
    origin: str | None
    destination: str | None
    day: date | None
    message: str
    detail: dict = field(default_factory=dict)
    notify: bool = True

    @property
    def route(self) -> str:
        return f"{self.origin}→{self.destination}" if self.origin and self.destination else ""


def _label(origin: str, destination: str) -> str:
    return f"{origin}→{destination} ({airport_name(destination) if origin == config.ORIGIN else airport_name(origin)})"


def detect_route(
    origin: str,
    destination: str,
    prev: dict[date, float | None] | None,
    cur: dict[date, float | None],
    floor: float | None,
) -> tuple[list[Event], float | None]:
    """Compare this run's sightings with the previous run's for one route.

    Returns the events raised and the route's new floor price.
    """
    events: list[Event] = []
    cur_priced = {d: p for d, p in cur.items() if p is not None}
    if not cur_priced and not prev:
        return events, floor

    prices = list(cur_priced.values())
    new_floor = min(prices + ([floor] if floor is not None else [])) if prices else floor
    label = _label(origin, destination)

    if floor is not None and new_floor is not None and new_floor < floor:
        days = sorted(d for d, p in cur_priced.items() if p == new_floor)
        events.append(Event("new_floor", origin, destination, days[0], f"{label}: new lowest fare ${new_floor:.0f} on {days[0]:%a %b %d}",
                            {"floor": new_floor, "previous_floor": floor, "days": [d.isoformat() for d in days]}))

    if prev:
        prev_priced = {d: p for d, p in prev.items() if p is not None}
        common = sorted(set(prev_priced) & set(cur_priced))
        if len(common) >= config.SALE_WAVE_MIN_DAYS:
            drops = [d for d in common if cur_priced[d] <= prev_priced[d] * (1 - config.SALE_WAVE_DROP)]
            if len(drops) / len(common) >= config.SALE_WAVE_SHARE:
                avg = sum(1 - cur_priced[d] / prev_priced[d] for d in drops) / len(drops)
                events.append(Event("sale_wave", origin, destination, drops[0],
                                    f"{label}: sale wave — {len(drops)} of {len(common)} days down {avg:.0%} on average (from ${min(cur_priced[d] for d in drops):.0f})",
                                    {"days": [d.isoformat() for d in drops], "avg_drop": round(avg, 3)}))

        for d in sorted(set(prev_priced) & {d for d, p in cur.items() if p is None}):
            events.append(Event("sold_out", origin, destination, d,
                                f"{label}: {d:%a %b %d} no longer bookable (was ${prev_priced[d]:.0f})",
                                {"last_price": prev_priced[d]}))

        if floor is not None:
            for d in sorted(d for d in common if prev_priced[d] <= floor and cur_priced[d] > floor):
                events.append(Event("floor_gone", origin, destination, d,
                                    f"{label}: {d:%a %b %d} left the ${floor:.0f} floor, now ${cur_priced[d]:.0f}",
                                    {"floor": floor, "now": cur_priced[d]}))

    if new_floor is not None:
        prev_priced = {d: p for d, p in (prev or {}).items() if p is not None}
        at_floor = sorted(d for d, p in cur_priced.items() if p <= new_floor and prev_priced.get(d) != p)
        for d in at_floor:
            events.append(Event("floor_fare", origin, destination, d,
                                f"{label}: {d:%a %b %d} at the ${new_floor:.0f} floor fare",
                                {"floor": new_floor}, notify=False))

    return events, new_floor


def run_detection(run_id: int, conn: sqlite3.Connection | None = None) -> list[Event]:
    """Detect signals for every route this run saw and store them."""
    all_events: list[Event] = []
    for origin, dest in database.routes_in_run(run_id, conn):
        cur = database.seen_in_run(run_id, origin, dest, conn)
        prev_id = database.previous_run_for_route(run_id, origin, dest, conn)
        prev = database.seen_in_run(prev_id, origin, dest, conn) if prev_id is not None else None
        floor = database.get_floor(origin, dest, conn)
        events, new_floor = detect_route(origin, dest, prev, cur, floor)
        if new_floor is not None and new_floor != floor:
            database.set_floor(origin, dest, new_floor, conn)
        all_events.extend(events)
    database.add_events(run_id, all_events, conn)
    if all_events:
        log.info("signals: %s", ", ".join(f"{e.kind}×{sum(1 for x in all_events if x.kind == e.kind)}"
                                          for e in {x.kind: x for x in all_events}.values()))
    return all_events
