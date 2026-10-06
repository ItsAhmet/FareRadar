"""Trip identity: what a flight *is*, independent of which database row holds it.

A trip key is `AIRLINE|ORIGIN|DESTINATION|YYYY-MM-DD|HH:MM`, with `*` for the
time when only the day is known (calendar scrapes). Watches, rules and bookings
store trip keys so a calendar row being replaced by a detail row (different
flight_id, real departure time) is still the same trip.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from models import Flight

ANY_TIME = "*"


@dataclass(frozen=True)
class TripKey:
    airline: str
    origin: str
    destination: str
    day: date
    time: str  # "HH:MM" or ANY_TIME

    def __str__(self) -> str:
        return f"{self.day_key}|{self.time}"

    @property
    def day_key(self) -> str:
        return f"{self.airline}|{self.origin}|{self.destination}|{self.day.isoformat()}"


def trip_key(f: Flight) -> str:
    clock = f.departure_datetime.strftime("%H:%M") if f.time_known else ANY_TIME
    return f"{day_key(f)}|{clock}"


def day_key(f: Flight) -> str:
    return f"{f.airline}|{f.origin}|{f.destination}|{f.departure_date.isoformat()}"


def parse(key: str) -> TripKey:
    parts = (key or "").split("|")
    if len(parts) != 5:
        raise ValueError(f"not a trip key: {key!r}")
    airline, origin, destination, day, clock = parts
    return TripKey(airline, origin, destination, date.fromisoformat(day), clock)


def matches(a: str, b: str) -> bool:
    """Same airline, route and day, and the same time unless either is unknown."""
    ka, kb = parse(a), parse(b)
    if ka.day_key != kb.day_key:
        return False
    return ANY_TIME in (ka.time, kb.time) or ka.time == kb.time


def legacy_key(flight_id: str, departure_iso: str, airline: str = "XP") -> str | None:
    """Trip key for payloads saved before trip keys existed (flight_id + departure)."""
    airports = [p for p in (flight_id or "").split("-") if len(p) == 3 and p.isalpha()]
    if len(airports) < 2 or len(departure_iso or "") < 10:
        return None
    origin, destination = airports[-2].upper(), airports[-1].upper()
    clock = departure_iso[11:16] if len(departure_iso) >= 16 else ANY_TIME
    if flight_id.upper().startswith("LOWFARE") or clock in ("", "00:00"):
        clock = ANY_TIME
    return f"{airline}|{origin}|{destination}|{departure_iso[:10]}|{clock}"


def payload_trip(payload: dict, leg: str) -> str | None:
    """Trip key for the 'out' or 'in' leg of a watch/booking payload, new or legacy."""
    return payload.get(f"{leg}_trip") or legacy_key(payload.get(f"{leg}_id", ""), payload.get(f"{leg}_dep", ""))


def upgrade(stored: str, found: Flight) -> str:
    """Pin a day-only key to the flight that now answers it, once its time is known."""
    if parse(stored).time == ANY_TIME and found.time_known:
        return trip_key(found)
    return stored
