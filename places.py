"""Place-based mode: where you're *actually* going, and how far each airport is.

* Geocoding: Open-Meteo's keyless geocoder.
* Drive time: the public OSRM demo router, with a road-factor estimate from
  straight-line distance when it is unreachable. Both cached in SQLite so a
  place is resolved once.
"""
from __future__ import annotations

import logging
import math
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import config
import database
from netutil import get_json

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
ROAD_FACTOR = 1.25  # road miles ≈ straight-line × 1.25
AVG_MPH = 45.0
AIRPORT_OVERHEAD_MIN = 10.0  # parking lot to rental car, etc.


@dataclass(frozen=True)
class Place:
    query: str
    name: str
    lat: float
    lon: float


def _miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b, c, d = map(math.radians, (lat1, lon1, lat2, lon2))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def estimate_minutes(straight_miles: float) -> float:
    return round(straight_miles * ROAD_FACTOR / AVG_MPH * 60 + AIRPORT_OVERHEAD_MIN, 1)


def parse_geocode(payload: dict, query: str) -> Place | None:
    for r in payload.get("results") or []:
        if r.get("latitude") is None:
            continue
        parts = [r.get("name"), r.get("admin1"), r.get("country_code")]
        return Place(query, ", ".join(p for p in parts if p), float(r["latitude"]), float(r["longitude"]))
    return None


def geocode(query: str) -> Place | None:
    query = " ".join(query.split()).strip()
    if not query:
        return None
    cached = database.get_place(query)
    if cached:
        return Place(query, cached["name"], cached["lat"], cached["lon"])
    if config.OFFLINE:
        return None
    url = f"{GEOCODE_URL}?{urllib.parse.urlencode({'name': query, 'count': 5, 'language': 'en', 'format': 'json'})}"
    try:
        place = parse_geocode(get_json(url, config.WEATHER_TIMEOUT_S), query)
    except Exception as exc:  # noqa: BLE001
        log.warning("geocode failed for %r: %s", query, exc)
        return None
    if place:
        database.set_place(query, place.name, place.lat, place.lon)
    return place


def osrm_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
    if config.OFFLINE:
        return None
    url = f"{OSRM_URL}/{lon1:.5f},{lat1:.5f};{lon2:.5f},{lat2:.5f}?overview=false"
    try:
        payload = get_json(url, config.WEATHER_TIMEOUT_S)
        routes = payload.get("routes") or []
        return round(routes[0]["duration"] / 60 + AIRPORT_OVERHEAD_MIN, 1) if routes else None
    except Exception as exc:  # noqa: BLE001
        log.debug("osrm failed: %s", exc)
        return None


def drive_minutes(place: Place, airport: str) -> tuple[float, str]:
    """Minutes from an airport to the place, and how we know ('osrm' | 'estimate')."""
    a = config.ALL_AIRPORTS.get(airport.upper())
    if a is None:
        return float("inf"), "unknown"
    cached = database.get_drive(place.query, a.code)
    if cached:
        return cached["minutes"], cached["source"]
    minutes = osrm_minutes(a.lat, a.lon, place.lat, place.lon)
    source = "osrm"
    if minutes is None:
        minutes, source = estimate_minutes(_miles(a.lat, a.lon, place.lat, place.lon)), "estimate"
    database.set_drive(place.query, a.code, minutes, source)
    return minutes, source


def drive_table(place: Place, airports: list[str]) -> dict[str, tuple[float, str]]:
    return {code: drive_minutes(place, code) for code in airports}
