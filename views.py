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
    """Routes to offer while an airline's catalog holds nothing for the selected homes.

    The catalog passed in is already cut down to those homes, so "never filled by a scrape" and "filled, but this
    airline lists nothing from these homes" look the same here. The fallback is therefore limited to the homes
    each airline is known to fly from (Frontier: the ones with a route page; Avelo: `config.AVELO_FALLBACK_HOMES`),
    which makes the second case come out empty instead of as routes nobody flies."""
    if airline == "F9":
        homes = [h for h in homes if h in config.FRONTIER_ROUTE_PAGES]
        away = list(config.FRONTIER_AIRPORTS) + list(config.FRONTIER_SHARED_DESTINATIONS)
    else:
        homes = [h for h in homes if h in config.AVELO_FALLBACK_HOMES]
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


def map_view(homes: Iterable[str], plotted: Iterable[str], selected: Iterable[str]) -> dict[str, float]:
    """Where the map looks. The East Coast frame, unless a selected, offered destination lies west of -95 degrees:
    then a wider zoom centred on the midpoint of the longitudes drawn, so the West Coast is not cut off."""
    plotted, chosen = list(plotted), set(selected)
    if not any(config.ALL_AIRPORTS[c].lon < -95 for c in plotted if c in chosen):
        return {"latitude": 30.5, "longitude": -77.0, "zoom": 3.9}
    lons = [config.ALL_AIRPORTS[c.upper()].lon for c in [*homes, *plotted]]
    return {"latitude": 30.5, "longitude": round((min(lons) + max(lons)) / 2, 1), "zoom": 3.2}


def _cost(it: Itinerary) -> float:
    return it.door_to_door_cost if it.door_to_door_cost is not None else round(it.total_price + it.home_side_cost, 2)


def price_note(it: Itinerary) -> str:
    """Short warning for any place a package's price is shown: '' when every leg is a bookable fare."""
    names = unverified_airlines(it)
    return f"unverified {' & '.join(names)} calendar price" if names else ""


def unverified_airlines(it: Itinerary) -> list[str]:
    """Names of the airlines whose fare in this package is a calendar price that is not a bookable fare
    (Frontier: the day's lowest Discount Den total of any routing). Empty once a day search has confirmed both legs."""
    return airline_registry.unverified_calendar_names((it.outbound, it.inbound))


def airline_comparison(itineraries: list[Itinerary]) -> pd.DataFrame:
    """Per free window and destination: each airline's best single-airline package, side by side.
    Cost is door-to-door when drive times are known, otherwise fares plus home-side parking/drive.

    `Unverified` names the airlines whose figure on that row rests on a calendar price nobody can book as shown;
    when the cheaper side is one of them, `Cheaper` says so, because a day search can reverse the verdict."""
    names = [airline_registry.get(code).name for code in sorted(config.KNOWN_AIRLINES, reverse=True)]  # Avelo, Frontier
    columns = ["Window", "Destination"] + [c for n in names for c in (n, f"{n} from")] + ["Cheaper", "Saves", "Unverified"]
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
        unverified = [n for n in names if n in slot and unverified_airlines(slot[n])]
        cheaper = priced[0][1]
        row["Cheaper"] = f"{cheaper} (unverified)" if cheaper in unverified else cheaper
        row["Saves"] = round(priced[1][0] - priced[0][0], 2) if len(priced) > 1 else None
        row["Unverified"] = ", ".join(unverified)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def comparison_caption(count_carry_on: bool, unverified: Iterable[str] = ()) -> str:
    """What the Airline vs airline figures are made of, and a warning when some of them are calendar prices."""
    fees = ", ".join(f"{airline_registry.get(c).name} ${config.CARRY_ON_FEE.get(c, 0.0):.0f}"
                     for c in sorted(config.KNOWN_AIRLINES, reverse=True))
    text = ("Cost per package: both fares, "
            + (f"a carry-on bag on each flight ({fees}), " if count_carry_on else "")
            + "plus parking and the drive to the home airport when set in Settings "
              "(door-to-door when a destination place is entered). Mixed-airline packages are in the main table.")
    names = sorted(set(unverified))
    if names:
        text += (f" **Unverified** rows: the {' and '.join(names)} figure is a calendar price (the day's lowest "
                 f"{_programs(names)} total of any routing, connections included), not a nonstop fare you can book at "
                 "that price. Run the scraper with depth *full* for those dates before trusting the cheaper airline.")
    return text


def _programs(names: Iterable[str]) -> str:
    """'Discount Den' for ['Frontier']: the membership programs of the airlines with these names."""
    wanted = set(names)
    return " / ".join(sorted(a.member_program for a in airline_registry.AIRLINES.values() if a.name in wanted))


def calendar_note(airline_codes: Iterable[str]) -> str | None:
    """Fare calendar footnote for airlines whose calendar price is not a bookable fare (None when there is none)."""
    names = sorted(airline_registry.get(c).name for c in set(airline_codes) if not airline_registry.get(c).calendar_bookable)
    if not names:
        return None
    return (f"{' and '.join(names)} rows: on days no day search has confirmed, the price is the calendar's lowest "
            f"{_programs(names)} total of any routing (connections included), not a nonstop fare you can book at that "
            "price. A scrape with depth *full* replaces it with the real fare.")


def airline_tag(it: Itinerary) -> str:
    """The packages table's Airlines cell: each airline's name behind a dot in its colour (the map's colours),
    e.g. '🟣 Avelo', '🟢 Frontier', '🟣 Avelo out · 🟢 Frontier back'."""
    out, back = (airline_registry.get(code) for code in it.airlines)
    if out.code == back.code:
        return f"{out.dot} {out.name}"
    return f"{out.dot} {out.name} out · {back.dot} {back.name} back"


def hidden_package_columns(df: pd.DataFrame) -> list[str]:
    """Packages-table columns with nothing to say for these rows: 'Program would save' when it is $0 on every row
    (an owner who is a member of every program whose fares are listed)."""
    column = "Program would save"
    return [column] if column in df.columns and not (df[column].fillna(0) != 0).any() else []


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
    # a booked-legs figure that rounds to $0 (fares unknown, or already paid at the program price) is not a saving
    booked = bool(program.get("legs")) and round(program.get("would_have_saved") or 0.0) > 0
    if not booked and best <= 0:
        return None
    parts = []
    if booked:
        parts.append(f"${program['would_have_saved']:,.0f} on {program['legs']} booked leg{'s' if program['legs'] != 1 else ''}")
    if best > 0:
        parts.append(f"up to ${best:,.0f} on a package listed now")
    return f"{program['program']} would save: " + " · ".join(parts)
