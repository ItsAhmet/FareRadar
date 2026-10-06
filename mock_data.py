"""Synthetic ILG schedule for developing and demoing without scraping.

Avelo flies most leisure routes two to four days a week, so each destination
gets a weekday pattern (the return leg flies the same days, since the aircraft
turns around). Prices rise inside 45 days and on Fri/Sun; every fare carries
an Avelo PLUS member price about $40 below the public fare. Deterministic for
a given seed.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from config import DESTINATIONS, ORIGIN, default_routes
from models import Flight

# weekday numbers: Mon=0 … Sun=6
ROUTE_DAYS: dict[str, set[int]] = {
    "LAL": {3, 6},
    "SRQ": {0, 4},
    "TPA": {2, 4, 6},
    "MCO": {0, 3, 5, 6},
    "RSW": {3, 6},
    "DAB": {1, 5},
    "FLL": {2, 4, 6},
    "PBI": {3, 6},
    "MYR": {2, 5, 6},
    "ILM": {4, 0},
    "SAV": {3, 6},
    "CHS": {0, 4, 6},
    "GSP": {2, 5},
    "RDU": {1, 4, 6},
    "BNA": {3, 6},
    "SJU": {2, 5},
}
OUTBOUND_TIMES = [time(7, 30), time(14, 15), time(18, 40)]
RETURN_TIMES = [time(11, 5), time(17, 55)]
MEMBER_DISCOUNT = 40.0


def _duration(dest: str) -> int:
    from config import distance_miles

    return int(35 + distance_miles(ORIGIN, dest) / 7.6)  # taxi + ~455 mph


def _public_price(day: date, today: date, rng: random.Random) -> float:
    days_out = max((day - today).days, 0)
    base = 99 + max(0, 45 - days_out) * 2.2
    if day.weekday() in (4, 6):
        base += 30
    base += rng.choice([0, 0, 0, 20, 45, 80])
    return float(round(base + rng.uniform(-8, 8)))


def scheduled_days(origin: str, dest: str, dates: list[date]) -> list[date]:
    """Days in `dates` on which the mock schedule flies this route."""
    far = dest if origin == ORIGIN else origin
    weekdays = ROUTE_DAYS.get(far, {3, 6})
    return [d for d in dates if d.weekday() in weekdays]


def generate_mock_flights(
    start: date | None = None,
    days: int = 60,
    routes: list[tuple[str, str]] | None = None,
    seed: int = 42,
    discount: float = 0.0,
) -> list[Flight]:
    rng = random.Random(seed)
    start = start or date.today()
    routes = routes or default_routes()
    now = datetime.now()
    flights: list[Flight] = []

    for origin, dest in routes:
        outbound = origin == ORIGIN
        far = dest if outbound else origin
        if far not in DESTINATIONS:
            continue
        weekdays = ROUTE_DAYS.get(far, {3, 6})
        # Busier routes get a second daily departure.
        times = (OUTBOUND_TIMES if outbound else RETURN_TIMES)[: 2 if len(weekdays) >= 3 else 1]
        for offset in range(days):
            day = start + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            for dep in times:
                public = round((_public_price(day, start, rng) + (15 if dep.hour > 12 else 0)) * (1 - discount))
                flights.append(
                    Flight(
                        origin=origin,
                        destination=dest,
                        departure_datetime=datetime.combine(day, dep),
                        price=public - MEMBER_DISCOUNT,
                        base_price=public,
                        is_member_fare=True,
                        duration_minutes=_duration(far),
                        flight_id=f"MOCK{dep.hour:02d}-{origin}-{dest}",
                        timestamp_scraped=now,
                    )
                )
    return flights
