"""What the dashboard shows, computed without Streamlit so it can be unit-tested.

`app.py` owns widgets and layout; everything it needs to know about airlines,
home airports and packages beyond `optimizer`/`database` is decided here.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

import airlines as airline_registry
import config
from optimizer import Itinerary

Catalog = dict[str, list[tuple[str, str]]]  # airline code → directional (origin, destination) pairs


def _fallback_pairs(airline: str, homes: list[str]) -> list[tuple[str, str]]:
    """Routes to offer before an airline's catalog has been filled by a scrape."""
    if airline == "F9":
        homes = [h for h in homes if h in config.FRONTIER_ROUTE_PAGES]
        away = list(config.FRONTIER_AIRPORTS) + list(config.FRONTIER_SHARED_DESTINATIONS)
    else:
        away = list(config.DESTINATIONS)
    return [(h, d) for h in homes for d in away]


def _pairs(airline: str, homes: list[str], catalog: Catalog) -> list[tuple[str, str]]:
    """(home, destination) pairs this airline serves from the selected homes."""
    listed = catalog.get(airline) or []
    if not listed:
        return _fallback_pairs(airline, homes)
    home_set = set(homes)
    return sorted({(o, d) if o in home_set else (d, o) for o, d in listed if (o in home_set) != (d in home_set)})


def served_destinations(airline_codes: Iterable[str], homes: Iterable[str], catalog: Catalog) -> dict[str, list[str]]:
    """Destination → the selected airlines that serve it from at least one selected home airport."""
    homes = [h.upper() for h in homes]
    served: dict[str, set[str]] = {}
    for airline in (a.upper() for a in airline_codes):
        for _home, dest in _pairs(airline, homes, catalog):
            if dest in config.ALL_DESTINATIONS:
                served.setdefault(dest, set()).add(airline)
    return {dest: sorted(codes) for dest, codes in sorted(served.items())}


def destination_label(code: str, served_by: list[str]) -> str:
    names = ", ".join(sorted(airline_registry.get(a).name for a in served_by))
    return f"{code} · {config.airport_name(code)} · {names}" if names else f"{code} · {config.airport_name(code)}"


@dataclass(frozen=True)
class PlanEntry:
    """Routes one airline's scrape requests, and how far that count can be trusted.

    bound: "exact" (a scrape visits exactly `routes`), "at_least" (the route list will be re-read first and
    can only add routes) or "up_to" (no usable route list yet, so every wanted destination is counted)."""
    code: str
    routes: int
    bound: str = "exact"


def stale_frontier_homes(homes: Iterable[str], checked: Callable[[str], datetime | None],
                         now: datetime | None = None) -> list[str]:
    """Selected homes whose Frontier route page a scrape would re-read first (never listed, or older than the
    refresh age). `checked(home)` is `database.catalog_checked("F9", home)`."""
    now = now or datetime.now()
    out = []
    for home in (h.upper() for h in homes):
        if home not in config.FRONTIER_ROUTE_PAGES:
            continue
        seen = checked(home)
        if seen is None or now - seen >= timedelta(days=config.CATALOG_REFRESH_DAYS):
            out.append(home)
    return out


def scrape_estimate(airline_codes: Iterable[str], homes: Iterable[str], destinations: Iterable[str], catalog: Catalog,
                    refreshing: Iterable[str] = ()) -> dict[str, PlanEntry]:
    """Per airline, the directional routes a scrape of this selection requests (mirrors run_scraper.routes_for).

    Avelo's count is exact. Frontier's comes from its route catalog, which a scrape re-reads first for every
    home in `refreshing`: those counts are "at_least", and with no catalog rows for the
    selected homes the count is the "up_to" ceiling of every wanted destination from each home with a route page."""
    homes, wanted = [h.upper() for h in homes], {d.upper() for d in destinations}
    refreshing = {h.upper() for h in refreshing}
    plan: dict[str, PlanEntry] = {}
    for airline in (a.upper() for a in airline_codes):
        if airline != "F9":
            plan[airline] = PlanEntry(airline, 2 * len(homes) * len(wanted & set(config.DESTINATIONS)))
            continue
        home_set = set(homes)
        page_homes = [h for h in homes if h in config.FRONTIER_ROUTE_PAGES]
        listed = {(o, d) for o, d in (catalog.get(airline) or [])
                  if (o in home_set or d in home_set) and (o in wanted or d in wanted)}  # database.catalog_routes' filter
        if not page_homes:
            plan[airline] = PlanEntry(airline, len(listed))
        elif not catalog.get(airline):
            ceiling = 2 * len(page_homes) * len(wanted - home_set)
            plan[airline] = PlanEntry(airline, ceiling, "up_to" if ceiling else "exact")
        else:
            plan[airline] = PlanEntry(airline, len(listed), "at_least" if refreshing & set(page_homes) else "exact")
    return plan


def scrape_plan(airline_codes: Iterable[str], homes: Iterable[str], destinations: Iterable[str], catalog: Catalog) -> dict[str, int]:
    """`scrape_estimate`'s route counts alone."""
    return {code: e.routes for code, e in scrape_estimate(airline_codes, homes, destinations, catalog).items()}


def describe_plan(plan: dict[str, PlanEntry]) -> str:
    """'Avelo 8 · Frontier up to 4 (route list not fetched yet)': only an estimate is worded as one."""
    parts = []
    for code, entry in sorted(plan.items(), reverse=True):
        name = airline_registry.get(code).name
        if entry.bound == "up_to":
            parts.append(f"{name} up to {entry.routes} (route list not fetched yet)")
        elif entry.bound == "at_least":
            parts.append(f"{name} at least {entry.routes} (route list refreshes first)")
        else:
            parts.append(f"{name} {entry.routes}")
    return " · ".join(parts)


def mock_selection(homes: Iterable[str], airline_codes: Iterable[str]) -> tuple[list[str], list[str]]:
    """What a --mock run really covers: run_scraper forces Avelo, and the mock schedule only flies PRIMARY_HOME."""
    airline_codes = list(airline_codes)
    return [h for h in homes if h == config.PRIMARY_HOME], (["XP"] if airline_codes else [])


def map_arcs(homes: Iterable[str], served: dict[str, list[str]], selected: Iterable[str], catalog: Catalog) -> list[dict]:
    """One arc per (home, destination, airline) that is actually served, for the selected destinations."""
    homes, chosen = [h.upper() for h in homes], set(selected)
    arcs = []
    for dest, codes in served.items():
        if dest not in chosen:
            continue
        for airline in codes:
            for home, d in _pairs(airline, homes, catalog):
                if d != dest:
                    continue
                h, a = config.ALL_AIRPORTS[home], config.ALL_AIRPORTS[dest]
                arcs.append({"home": home, "code": dest, "city": a.city, "airline": airline,
                             "airline_name": airline_registry.get(airline).name,
                             "source": [h.lon, h.lat], "target": [a.lon, a.lat],
                             "color": list(airline_registry.get(airline).color)})
    return arcs


def _cost(it: Itinerary) -> float:
    return it.door_to_door_cost if it.door_to_door_cost is not None else round(it.total_price + it.home_side_cost, 2)


def airline_comparison(itineraries: list[Itinerary]) -> pd.DataFrame:
    """Per free window and destination: each airline's best single-airline package, side by side.
    Cost is door-to-door when drive times are known, otherwise fares plus home-side parking/drive."""
    names = [airline_registry.get(code).name for code in sorted(config.KNOWN_AIRLINES, reverse=True)]  # Avelo, Frontier
    columns = ["Window", "Destination"] + [c for n in names for c in (n, f"{n} from")] + ["Cheaper", "Saves"]
    best: dict[tuple, dict[str, Itinerary]] = {}
    for it in itineraries:
        out, back = it.airlines
        if out != back:
            continue
        slot = best.setdefault((it.window.start, it.window.label, it.destination_city), {})
        name = airline_registry.get(out).name
        if name not in slot or _cost(it) < _cost(slot[name]):
            slot[name] = it
    rows = []
    for (_start, window, city), slot in sorted(best.items()):
        row = {"Window": window, "Destination": city}
        for n in names:
            row[n] = _cost(slot[n]) if n in slot else None
            row[f"{n} from"] = slot[n].home_label if n in slot else None
        priced = sorted((row[n], n) for n in names if row[n] is not None)
        row["Cheaper"] = priced[0][1]
        row["Saves"] = round(priced[1][0] - priced[0][0], 2) if len(priced) > 1 else None
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _own_program_savings(it: Itinerary, airline: str | None) -> float:
    """Program savings on the legs this airline flies; other airlines' legs in a mixed package are not its saving."""
    if airline is None:
        return it.program_savings
    saved = 0.0
    for f in (it.outbound, it.inbound):
        if f.airline == airline and not f.is_member_fare and f.member_price is not None:
            saved += max(f.price - f.member_price, 0.0)
    return round(saved, 2)


def program_meter(program: dict, itineraries: list[Itinerary]) -> str | None:
    """Badge text: what the program would have saved on booked legs, and would save on the best listed package."""
    best = max((_own_program_savings(it, program.get("airline")) for it in itineraries), default=0.0)
    if not program.get("legs") and best <= 0:
        return None
    parts = []
    if program.get("legs"):
        parts.append(f"${program['would_have_saved']:,.0f} on {program['legs']} booked leg{'s' if program['legs'] != 1 else ''}")
    if best > 0:
        parts.append(f"up to ${best:,.0f} on a package listed now")
    return f"{program['program']} would save: " + " · ".join(parts)
