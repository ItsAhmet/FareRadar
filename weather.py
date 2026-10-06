"""Destination weather outlook via Open-Meteo (free, no API key).

Only the next 16 days are forecastable; later dates return an empty list and
the UI says so. Network failures never raise: the caller gets None.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import date

import config
from netutil import get_json

log = logging.getLogger(__name__)

# WMO weather codes → short description.
WMO: dict[int, str] = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Snow", 73: "Snow", 75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


def parse_forecast(payload: dict, start: date, end: date) -> list[dict]:
    """Open-Meteo daily payload → rows for days within [start, end]."""
    daily = payload.get("daily") or {}
    rows = []
    for i, day in enumerate(daily.get("time", [])):
        d = date.fromisoformat(day)
        if not (start <= d <= end):
            continue

        def pick(key: str):
            values = daily.get(key) or []
            return values[i] if i < len(values) else None

        code = pick("weathercode")
        rows.append(
            {
                "day": d,
                "high_f": pick("temperature_2m_max"),
                "low_f": pick("temperature_2m_min"),
                "rain_pct": pick("precipitation_probability_max"),
                "sky": WMO.get(int(code), "—") if code is not None else "—",
            }
        )
    return rows


def forecast(code: str, start: date, end: date) -> list[dict] | None:
    """Daily outlook for an airport's city. None on any failure or offline."""
    airport = config.ALL_AIRPORTS.get(code.upper())
    if airport is None or config.OFFLINE:
        return None
    params = {
        "latitude": airport.lat,
        "longitude": airport.lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 16,
    }
    url = f"{config.WEATHER_URL}?{urllib.parse.urlencode(params)}"
    try:
        payload = get_json(url, config.WEATHER_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - weather is a nicety, never a blocker
        log.debug("weather lookup failed for %s: %s", code, exc)
        return None
    return parse_forecast(payload, start, end)


def forecast_all(code: str) -> list[dict] | None:
    """The full 16-day outlook for an airport's city (cache this one)."""
    return forecast(code, date.min, date.max)


def slice_days(rows: list[dict] | None, start: date, end: date) -> list[dict]:
    return [r for r in (rows or []) if start <= r["day"] <= end]


def score(rows: list[dict]) -> float | None:
    """0..1 'sun score': warm (60–90°F ramps 0→1) and dry (1 − rain chance),
    averaged over the trip days. None when no forecast covers the trip."""
    scored = []
    for r in rows:
        if r.get("high_f") is None:
            continue
        warmth = max(0.0, min(1.0, (r["high_f"] - 60.0) / 30.0))
        dry = 1.0 - (r.get("rain_pct") or 0) / 100.0
        scored.append(warmth * dry)
    return round(sum(scored) / len(scored), 3) if scored else None


def mean_high(rows: list[dict]) -> float | None:
    highs = [r["high_f"] for r in rows if r.get("high_f") is not None]
    return round(sum(highs) / len(highs), 1) if highs else None
