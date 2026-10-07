"""Frontier Airlines: pure parsers over the JSON its booking site embeds, plus (Task 7) a thin browser driver.

How Frontier's site works (probed 2026-10-06, fixtures in tests/fixtures/frontier/):
* `booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov 12, 2026&ADT=1&mon=true` renders a
  server-side page that embeds every flight as JSON in `window.FlightDataJson`: fares for Discount Den and
  standard, local times, stops. Most flights are connections; only `stopCount == 0` single-leg flights count.
* Adding `&c=true` shows a month calendar that POSTs `/Flight/GetLowFareCalendarAvailability`; the response
  holds the lowest **Discount Den** total per day (amount + taxesAndFees). It does not say whether that fare
  is a nonstop, so calendar rows are placeholders that a day search confirms.
* `flights.flyfrontier.com/en/flights-from-<city>` (plain HTTP) lists the destinations it sells from a city.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time

import airlines
from browser import ScrapeError
from models import Flight

log = logging.getLogger(__name__)

AIRLINE = "F9"
_F9 = airlines.get(AIRLINE)
_CHALLENGE_MARKERS = ("px-captcha", "press &amp; hold", "press & hold", "access denied", "are you a robot", "perimeterx")
_ROUTE_PAIR = re.compile(r'"originAirportCode":"([A-Z]{3})","destinationAirportCode":"([A-Z]{3})"')


def _fare(value) -> float | None:
    """Frontier uses 0 or -1 for "not offered"."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return round(amount, 2) if amount > 0 else None


def _minutes(hms: str | None) -> int | None:
    """'02:35:00' → 155."""
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", hms or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _journey_flights(payload: dict) -> list[dict]:
    journeys = (payload or {}).get("journeys") or []
    return (journeys[0].get("flights") or []) if journeys else []


def select_has_flights(payload: dict) -> bool:
    """Did the day search return any flight at all (nonstop or connecting)?"""
    return bool(_journey_flights(payload))


def parse_select(payload: dict, origin: str, destination: str, day: date, member: bool = False,
                 scraped_at: datetime | None = None) -> list[Flight]:
    """`window.FlightDataJson` → one Flight per nonstop on `day` for this route."""
    scraped_at = scraped_at or datetime.now()
    origin, destination = origin.strip().upper(), destination.strip().upper()
    out: list[Flight] = []
    for item in _journey_flights(payload):
        legs = item.get("legs") or []
        if item.get("stopCount") != 0 or len(legs) != 1:
            continue
        leg = legs[0]
        if (_F9.app_code(leg.get("departureStation", "")), _F9.app_code(leg.get("arrivalStation", ""))) != (origin, destination):
            continue
        try:
            departs = datetime.fromisoformat(leg["departureDate"])
            arrives = datetime.fromisoformat(leg["arrivalDate"])
        except (KeyError, TypeError, ValueError):
            continue
        if departs.date() != day:
            continue
        standard, den = _fare(item.get("standardFare")), _fare(item.get("discountDenFare"))
        price, is_member = airlines.owner_price(member, den, standard)
        if price is None:
            continue
        number = str(leg.get("flightNumber") or "").strip() or None
        out.append(Flight(
            airline=AIRLINE, origin=origin, destination=destination,
            departure_datetime=departs, arrival_datetime=arrives,
            duration_minutes=_minutes(leg.get("duration")) or int((arrives - departs).total_seconds() // 60),
            flight_number=number, price=price, base_price=standard, member_price=den, is_member_fare=is_member,
            flight_id=f"F9-{number or 'UNK'}-{origin}-{destination}", timestamp_scraped=scraped_at,
        ))
    return out


def parse_calendar(payload: dict, origin: str, destination: str, member: bool = False,
                   scraped_at: datetime | None = None) -> tuple[list[Flight], list[date]]:
    """Calendar response → (one placeholder per priced day, the days the response answered for).

    A response without a single priced day is not an answer ("everything sold out" and "the
    calendar did not load its fares" look the same), so it answers for no day at all.
    """
    if not (payload or {}).get("success") or not payload.get("calendarSections"):
        raise ScrapeError("Frontier calendar response missing or unsuccessful")
    scraped_at = scraped_at or datetime.now()
    origin, destination = origin.strip().upper(), destination.strip().upper()
    flights: list[Flight] = []
    answered: list[date] = []
    for week in payload["calendarSections"][0].get("weeks") or []:
        for cell in week.get("days") or []:
            if cell.get("isEmpty"):
                continue
            try:
                day = date.fromisoformat(cell["dateFormatted"])
            except (KeyError, TypeError, ValueError):
                continue
            answered.append(day)
            amount = _fare(cell.get("amount"))
            if cell.get("isUnavailable") or amount is None:
                continue
            total = round(amount + float(cell.get("taxesAndFees") or 0.0), 2)
            den = total if cell.get("isDiscountDen") else None
            standard = None if cell.get("isDiscountDen") else total
            price, is_member = airlines.owner_price(member, den, standard)
            flights.append(Flight(
                airline=AIRLINE, origin=origin, destination=destination,
                departure_datetime=datetime.combine(day, time(0)), price=price, base_price=standard,
                member_price=den, is_member_fare=is_member,
                flight_id=f"F9CAL-{origin}-{destination}", timestamp_scraped=scraped_at,
            ))
    if not flights:
        return [], []
    return flights, sorted(set(answered))


def parse_routes(html: str, origin: str) -> set[str]:
    """Destinations (app codes) Frontier's public "flights from <city>" page lists for `origin`."""
    origin = origin.strip().upper()
    site_origin = _F9.site_code(origin)
    return {_F9.app_code(dest) for o, dest in _ROUTE_PAIR.findall(html or "") if o == site_origin and dest != site_origin}


def is_challenge_page(html: str) -> bool:
    low = (html or "").lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def probe_days(flights: list[Flight], limit: int) -> list[date]:
    """The cheapest distinct days among calendar placeholders, in date order: where to look for a nonstop."""
    cheapest: dict[date, float] = {}
    for f in flights:
        cheapest[f.departure_date] = min(f.price, cheapest.get(f.departure_date, float("inf")))
    return sorted(sorted(cheapest, key=lambda d: (cheapest[d], d))[:limit])
