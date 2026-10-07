"""Flexible Getaway optimizer: ILG out, any nonstop city, back within your window.

Inputs
------
* A weekly `Availability` ("free from Wednesday 18:00 to Sunday 23:59") that is
  expanded over a date span into concrete `Window`s, or explicit windows.
* The destinations the user cares about, a minimum number of nights, a total
  budget, and how far apart two airports may be to count as one getaway.

Algorithm
---------
1. Pull every ILG→destination flight and every destination→ILG flight in the
   span from SQLite.
2. For each window, take outbound flights departing inside it and pair them
   with return flights that depart after the outbound lands and land before
   the window closes. Returns may leave from a different airport than the
   outbound arrived at, as long as both are in the same region and within
   `max_pair_miles` of each other (TPA out / LAL back; CHS out / SAV back).
3. Enforce minimum nights and budget, score each pair, rank.

Calendar scrapes only know the day, not the clock. Those flights are matched
by date and the itinerary is flagged `time_verified=False`; a detail scrape
upgrades them.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import random

import pandas as pd

import config
from config import DESTINATIONS, HomeCost, airport_name, distance_miles
from holidays import holidays_between
from database import previous_prices, query_flights, route_medians
from models import Flight

DEFAULT_DRIVE_VALUE_PER_HOUR = 15.0  # $/hour of your time until a place sets the rate

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# --------------------------------------------------------------------------- #
# Temporal constraints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Window:
    """One concrete block of free time, e.g. Wed Oct 7 18:00 → Sun Oct 11 23:59."""

    start: datetime
    end: datetime
    holiday: str | None = None  # set when the window was stretched around a holiday

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("window end must be after start")

    @property
    def label(self) -> str:
        base = f"{self.start:%a %b} {self.start.day} → {self.end:%a %b} {self.end.day}"
        return f"{base} 🎉 {self.holiday}" if self.holiday else base

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class Availability:
    """A weekly recurring free block: default Wednesday 18:00 → Sunday 23:59."""

    start_weekday: int = 2
    start_time: time = time(18, 0)
    end_weekday: int = 6
    end_time: time = time(23, 59)

    def windows(self, span_start: date, span_end: date, holiday_extend: bool = False) -> list[Window]:
        """Every occurrence of the block that fits entirely inside the span.

        With `holiday_extend`, a federal holiday on the day after a window's
        end extends the window through that day (Monday holidays), and a
        holiday on the window's first day pulls the start back one evening
        (Friday holidays). Consecutive holidays chain.
        """
        first = span_start + timedelta(days=(self.start_weekday - span_start.weekday()) % 7)
        ahead = (self.end_weekday - self.start_weekday) % 7
        if ahead == 0 and self.end_time <= self.start_time:
            ahead = 7
        hols = holidays_between(span_start - timedelta(days=7), span_end + timedelta(days=7)) if holiday_extend else {}
        out: list[Window] = []
        day = first
        while day <= span_end:
            start = datetime.combine(day, self.start_time)
            end = datetime.combine(day + timedelta(days=ahead), self.end_time)
            if end.date() <= span_end:
                holiday = None
                while (end.date() + timedelta(days=1)) in hols:
                    end = datetime.combine(end.date() + timedelta(days=1), self.end_time)
                    holiday = hols[end.date()]
                while start.date() in hols:
                    holiday = holiday or hols[start.date()]
                    start = datetime.combine(start.date() - timedelta(days=1), self.start_time)
                out.append(Window(start, end, holiday))
            day += timedelta(days=7)
        return out


def intersect_windows(mine: list[Window], theirs: list[Window]) -> list[Window]:
    """Overlaps between two people's windows: when you're *both* free."""
    out: list[Window] = []
    for a in mine:
        for b in theirs:
            start, end = max(a.start, b.start), min(a.end, b.end)
            if end > start:
                out.append(Window(start, end, a.holiday or b.holiday))
    return sorted(set(out), key=lambda w: w.start)


# --------------------------------------------------------------------------- #
# Avelo PLUS member benefits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemberBenefits:
    """Add-ons that Avelo PLUS includes. Locked to $0 — there are deliberately
    no sliders for these: the member pays fares only."""

    carry_on_bag: float = 0.0
    seat_selection: float = 0.0

    @property
    def total(self) -> float:
        return round(self.carry_on_bag + self.seat_selection, 2)

    @property
    def summary(self) -> str:
        return "Free carry-on bag · Free seat selection"


BENEFITS = MemberBenefits(**config.MEMBER_BENEFITS)


# --------------------------------------------------------------------------- #
# Query and result types
# --------------------------------------------------------------------------- #


@dataclass
class GetawayQuery:
    destinations: set[str]
    windows: list[Window]
    origins: list[str] = field(default_factory=lambda: list(config.HOME_CODES))
    min_nights: int = 1
    max_budget: float | None = None
    allow_mix: bool = True
    max_pair_miles: float = 100.0
    allow_open_jaw: bool = False  # may a trip return to a different home airport?

    def __post_init__(self) -> None:
        self.destinations = {d.upper() for d in self.destinations}
        self.origins = list(dict.fromkeys(o.strip().upper() for o in self.origins))
        if not self.origins:
            raise ValueError("at least one origin (home airport) is required")
        if self.min_nights < 0:
            raise ValueError("min_nights cannot be negative")
        if not self.windows:
            raise ValueError("at least one window is required")

    @property
    def span(self) -> tuple[date, date]:
        return min(w.start for w in self.windows).date(), max(w.end for w in self.windows).date()


@dataclass
class Itinerary:
    outbound: Flight
    inbound: Flight
    window: Window
    fit: float = field(default=0.0)  # share of the window actually used, 0..1
    # Filled by `enrich()` from the database; None until then.
    typical_total: float | None = None  # sum of route medians for this pairing
    previous_total: float | None = None  # total at the previous scrape of both legs
    # Filled by `enrich_weather()` (only inside the 16-day forecast horizon).
    sun_score: float | None = None
    temp_delta_f: float | None = None  # destination high minus primary-home high
    # Filled by `enrich_drives()` in place-based mode.
    drive_out_min: float | None = None  # arrival airport → your place
    drive_back_min: float | None = None  # your place → departure airport
    drive_value_per_hour: float = DEFAULT_DRIVE_VALUE_PER_HOUR
    # Filled by `enrich_home()`: getting to and from the home airports.
    home_drive_min: float | None = None  # home→departure airport + return airport→home (+ inter-home if open-jaw)
    parking_cost: float = 0.0            # parking at the departure home airport for the trip

    @property
    def drive_total_min(self) -> float | None:
        if self.drive_out_min is None or self.drive_back_min is None:
            return None
        return round(self.drive_out_min + self.drive_back_min, 1)

    @property
    def home_side_cost(self) -> float:
        """Parking plus your time driving to/from the home airports (0 for unknown drives)."""
        return round(self.parking_cost + (self.home_drive_min or 0.0) / 60 * self.drive_value_per_hour, 2)

    @property
    def door_to_door_cost(self) -> float | None:
        """Fares, your time in the car at the destination, and home-side parking/drive."""
        if self.drive_total_min is None:
            return None
        return round(self.total_price + self.drive_total_min / 60 * self.drive_value_per_hour + self.home_side_cost, 2)

    @property
    def fare_total(self) -> float:
        return round(self.outbound.price + self.inbound.price, 2)

    @property
    def addon_fees(self) -> float:
        """Carry-on and seat selection: $0 for an Avelo PLUS member."""
        return BENEFITS.total

    @property
    def total_price(self) -> float:
        """Total Getaway Cost: both member fares plus (zero) member add-on fees."""
        return round(self.fare_total + self.addon_fees, 2)

    @property
    def deal_pct(self) -> float | None:
        """Positive = cheaper than typical for these routes; None if unknown."""
        if not self.typical_total:
            return None
        return round((self.typical_total - self.total_price) / self.typical_total, 3)

    @property
    def trend_delta(self) -> float | None:
        """Change since the previous scrape (negative = price fell)."""
        if self.previous_total is None:
            return None
        return round(self.total_price - self.previous_total, 2)

    @property
    def trend(self) -> str:
        d = self.trend_delta
        if d is None:
            return "new"
        if d < -0.5:
            return f"▼ falling ${-d:,.0f}"
        if d > 0.5:
            return f"▲ rising ${d:,.0f}"
        return "— steady"

    @property
    def last_scraped(self) -> datetime:
        return min(self.outbound.timestamp_scraped, self.inbound.timestamp_scraped)

    @property
    def savings(self) -> float:
        return round(self.outbound.savings + self.inbound.savings, 2)

    @property
    def member_fare(self) -> bool:
        return self.outbound.is_member_fare and self.inbound.is_member_fare

    @property
    def nights(self) -> int:
        return (self.inbound.departure_date - self.outbound.departure_date).days

    @property
    def time_verified(self) -> bool:
        return self.outbound.time_known and self.inbound.time_known

    @property
    def destination(self) -> str:
        """The city you're going to (where the outbound lands)."""
        return self.outbound.destination

    @property
    def destination_city(self) -> str:
        return airport_name(self.destination)

    @property
    def home_out(self) -> str:
        """Home airport the trip leaves from."""
        return self.outbound.origin

    @property
    def home_back(self) -> str:
        """Home airport the trip returns to."""
        return self.inbound.destination

    @property
    def is_open_jaw(self) -> bool:
        return self.home_out != self.home_back

    @property
    def home_label(self) -> str:
        return f"{self.home_out}→{self.home_back}" if self.is_open_jaw else self.home_out

    @property
    def is_mixed(self) -> bool:
        return self.outbound.destination != self.inbound.origin

    @property
    def route_mix(self) -> str:
        if self.is_mixed:
            return f"{self.outbound.destination} in · {self.inbound.origin} out"
        return f"{self.outbound.destination} in & out"

    @property
    def pair_miles(self) -> float:
        return round(distance_miles(self.outbound.destination, self.inbound.origin), 1)

    @property
    def price_key(self) -> tuple[float, float]:
        return (self.total_price, -self.fit)

    @property
    def schedule_key(self) -> tuple[float, float]:
        return (-self.fit, self.total_price)

    @property
    def sun_key(self) -> tuple[int, float, float]:
        scored = self.sun_score is not None
        return (0 if scored else 1, -(self.sun_score or 0.0), self.total_price)

    @property
    def door_key(self) -> tuple[int, float, float]:
        known = self.door_to_door_cost is not None
        return (0 if known else 1, self.door_to_door_cost or 0.0, self.total_price)


# --------------------------------------------------------------------------- #
# Core rules (pure, no DB)
# --------------------------------------------------------------------------- #


def can_pair(arrive_at: str, depart_from: str, query: GetawayQuery) -> bool:
    """May a getaway land at `arrive_at` and fly home from `depart_from`?"""
    if arrive_at == depart_from:
        return True
    if not query.allow_mix:
        return False
    a, b = DESTINATIONS.get(arrive_at), DESTINATIONS.get(depart_from)
    if not (a and b) or a.region != b.region:
        return False
    return distance_miles(arrive_at, depart_from) <= query.max_pair_miles


def outbound_fits(f: Flight, w: Window) -> bool:
    if f.time_known:
        return w.start <= f.departure_datetime <= w.end
    return w.start.date() <= f.departure_date <= w.end.date()


def inbound_fits(back: Flight, out: Flight, w: Window) -> bool:
    """Return leaves after the outbound lands and is home before the window ends."""
    if back.time_known and out.time_known:
        return out.effective_arrival < back.departure_datetime and back.effective_arrival <= w.end
    if back.time_known:
        return back.departure_date > out.departure_date and back.effective_arrival <= w.end
    # Return time unknown: be conservative on both ends.
    return back.departure_date > out.departure_date and back.departure_date <= w.end.date()


def fit_score(out: Flight, back: Flight, w: Window) -> float:
    """Share of the free window the trip occupies (1.0 = wall to wall)."""
    if out.time_known and back.time_known:
        used = (back.effective_arrival - out.departure_datetime).total_seconds()
        available = (w.end - w.start).total_seconds()
    else:
        used = ((back.departure_date - out.departure_date).days + 1) * 86400
        available = ((w.end.date() - w.start.date()).days + 1) * 86400
    return max(0.0, min(1.0, used / available)) if available > 0 else 0.0


def build_itineraries(outbounds: list[Flight], inbounds: list[Flight], query: GetawayQuery) -> list[Itinerary]:
    """Every valid (outbound, inbound, window) triple, unsorted."""
    homes = set(query.origins)
    outbounds = [f for f in outbounds if f.origin in homes and f.destination in query.destinations]
    inbound_by_airport: dict[str, list[Flight]] = defaultdict(list)
    for f in inbounds:
        if f.destination in homes and f.origin in query.destinations:
            inbound_by_airport[f.origin].append(f)

    # Which airports each arrival airport may be paired with (computed once).
    pairable: dict[str, list[str]] = {
        code: [other for other in inbound_by_airport if can_pair(code, other, query)] for code in query.destinations
    }

    results: list[Itinerary] = []
    for out in outbounds:
        for w in query.windows:
            if not outbound_fits(out, w):
                continue
            for away_airport in pairable.get(out.destination, ()):
                for back in inbound_by_airport[away_airport]:
                    if not query.allow_open_jaw and back.destination != out.origin:
                        continue
                    if not inbound_fits(back, out, w):
                        continue
                    if (back.departure_date - out.departure_date).days < query.min_nights:
                        continue
                    if query.max_budget is not None and out.price + back.price > query.max_budget:
                        continue
                    results.append(Itinerary(out, back, w, fit_score(out, back, w)))
    return results


def rank_by_price(itineraries: list[Itinerary]) -> list[Itinerary]:
    return sorted(itineraries, key=lambda i: i.price_key)


def rank_by_schedule(itineraries: list[Itinerary]) -> list[Itinerary]:
    return sorted(itineraries, key=lambda i: i.schedule_key)


def rank_by_sun(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Sunniest first; packages beyond the forecast horizon sink to the bottom."""
    return sorted(itineraries, key=lambda i: i.sun_key)


def rank_by_door_to_door(itineraries: list[Itinerary]) -> list[Itinerary]:
    return sorted(itineraries, key=lambda i: i.door_key)


def enrich_weather(
    itineraries: list[Itinerary],
    forecasts: dict[str, list[dict] | None],
    hub_forecast: list[dict] | None,
) -> list[Itinerary]:
    """Attach sun score and temperature delta from per-airport 16-day outlooks."""
    from weather import mean_high, score, slice_days

    for it in itineraries:
        start, end = it.outbound.departure_date, it.inbound.departure_date
        rows = slice_days(forecasts.get(it.destination), start, end)
        it.sun_score = score(rows)
        there, home = mean_high(rows), mean_high(slice_days(hub_forecast, start, end))
        it.temp_delta_f = round(there - home, 1) if there is not None and home is not None else None
    return itineraries


def enrich_drives(
    itineraries: list[Itinerary],
    minutes_by_airport: dict[str, float],
    value_per_hour: float = DEFAULT_DRIVE_VALUE_PER_HOUR,
) -> list[Itinerary]:
    """Attach drive times from the place to each leg's airport."""
    for it in itineraries:
        it.drive_out_min = minutes_by_airport.get(it.outbound.destination)
        it.drive_back_min = minutes_by_airport.get(it.inbound.origin)
        it.drive_value_per_hour = value_per_hour
    return itineraries


def clear_drives(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Undo `enrich_drives()`: drop drive minutes and restore the default $/hour.

    Callers that reuse Itinerary objects across reruns (the dashboard's search cache)
    must call this when no place is set, or an earlier place's drives and rate linger.
    """
    for it in itineraries:
        it.drive_out_min = None
        it.drive_back_min = None
        it.drive_value_per_hour = DEFAULT_DRIVE_VALUE_PER_HOUR
    return itineraries


def inter_home_minutes(a: str, b: str) -> float:
    """Rough drive between two home airports: road ≈ 1.25 × straight line at 50 mph."""
    return round(distance_miles(a, b) * 1.25 / 50 * 60, 1)


def enrich_home(itineraries: list[Itinerary], costs: dict[str, HomeCost]) -> list[Itinerary]:
    """Attach parking and home-side drive time from the owner's per-airport costs."""
    for it in itineraries:
        out_c, back_c = costs.get(it.home_out), costs.get(it.home_back)
        days_parked = max(it.nights, 1)
        it.parking_cost = round((out_c.parking_per_day if out_c else 0.0) * days_parked, 2)
        legs = [out_c.drive_min if out_c else None, back_c.drive_min if back_c else None]
        if any(m is None for m in legs):
            it.home_drive_min = None
        else:
            extra = inter_home_minutes(it.home_back, it.home_out) if it.is_open_jaw else 0.0
            it.home_drive_min = round(legs[0] + legs[1] + extra, 1)
    return itineraries


# --------------------------------------------------------------------------- #
# DB-backed entry point and presentation
# --------------------------------------------------------------------------- #


def enrich(
    itineraries: list[Itinerary],
    medians: dict[tuple[str, str], float],
    previous: dict[tuple[str, str], float],
) -> list[Itinerary]:
    """Attach deal score and trend inputs. Pure: takes the lookups as dicts."""
    for it in itineraries:
        m_out = medians.get((it.outbound.origin, it.outbound.destination))
        m_in = medians.get((it.inbound.origin, it.inbound.destination))
        it.typical_total = round(m_out + m_in + it.addon_fees, 2) if m_out is not None and m_in is not None else None
        p_out = previous.get((it.outbound.flight_id, it.outbound.departure_datetime.isoformat(timespec="minutes")))
        p_in = previous.get((it.inbound.flight_id, it.inbound.departure_datetime.isoformat(timespec="minutes")))
        # Trend needs both legs' history; fall back to whichever leg has one.
        if p_out is not None and p_in is not None:
            it.previous_total = round(p_out + p_in + it.addon_fees, 2)
        elif p_out is not None:
            it.previous_total = round(p_out + it.inbound.price + it.addon_fees, 2)
        elif p_in is not None:
            it.previous_total = round(it.outbound.price + p_in + it.addon_fees, 2)
    return itineraries


def find_itineraries(query: GetawayQuery, conn: sqlite3.Connection | None = None) -> list[Itinerary]:
    start, end = query.span
    # Phase 3 makes the airline set part of GetawayQuery; until then packages are Avelo-only.
    outbounds = query_flights(query.origins, query.destinations, start, end, conn=conn, airlines=["XP"])
    inbounds = query_flights(query.destinations, query.origins, start, end, conn=conn, airlines=["XP"])
    itineraries = build_itineraries(outbounds, inbounds, query)
    if not itineraries:
        return itineraries
    medians = route_medians(query.origins, query.destinations, start, end, conn=conn, airlines=["XP"])
    keys = {(f.flight_id, f.departure_datetime) for it in itineraries for f in (it.outbound, it.inbound)}
    return enrich(itineraries, medians, previous_prices(keys, conn=conn))


def wildcard(itineraries: list[Itinerary], rng: random.Random | None = None) -> Itinerary | None:
    """Surprise pick: a random package, weighted towards cheaper ones, with
    each destination getting a fair shot rather than the busiest route."""
    if not itineraries:
        return None
    rng = rng or random.Random()
    by_dest: dict[str, list[Itinerary]] = defaultdict(list)
    for it in itineraries:
        by_dest[it.destination].append(it)
    dest = rng.choice(sorted(by_dest))
    pool = by_dest[dest]
    weights = [1.0 / max(it.total_price, 1.0) for it in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def _clock(f: Flight) -> str:
    return f.departure_datetime.strftime("%H:%M") if f.time_known else "—"


def to_dataframe(itineraries: list[Itinerary]) -> pd.DataFrame:
    rows = []
    for rank, it in enumerate(itineraries, start=1):
        rows.append(
            {
                "Rank": rank,
                "Window": it.window.label,
                "Destination": it.destination_city,
                "Home": it.home_label,
                "Route Mix": it.route_mix,
                "Out Route": it.outbound.route,
                "Out Date": it.outbound.departure_datetime.strftime("%a %b %d"),
                "Out Time": _clock(it.outbound),
                "Out Price": it.outbound.price,
                "Back Route": it.inbound.route,
                "Back Date": it.inbound.departure_datetime.strftime("%a %b %d"),
                "Back Time": _clock(it.inbound),
                "Back Price": it.inbound.price,
                "Total Getaway Cost": it.total_price,
                "Add-on Fees": it.addon_fees,
                "Deal vs Typical": it.deal_pct,
                "Trend": it.trend,
                "Nights": it.nights,
                "Fit": it.fit,
                "Member Fare": it.member_fare,
                "Savings vs Public": it.savings,
                "Times Verified": it.time_verified,
                "Airport Gap (mi)": it.pair_miles,
                "Sun Score": it.sun_score,
                "Δ°F vs home": it.temp_delta_f,
                "Drive (min)": it.drive_total_min,
                "Door-to-door": it.door_to_door_cost,
                "Home-side $": it.home_side_cost,
                "Last Scraped": it.last_scraped.strftime("%b %d %H:%M"),
            }
        )
    return pd.DataFrame(rows)
