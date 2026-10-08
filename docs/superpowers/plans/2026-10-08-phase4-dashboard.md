# Phase 4 — Dashboard for Several Airlines and Home Airports — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the multi-airline, multi-home-airport engine on screen: choose home airports and airlines, toggle open-jaw and carry-on, edit memberships and home-airport costs, compare airlines side by side, and scrape exactly what is selected.

**Architecture:** A new pure module `views.py` (no Streamlit) computes everything the dashboard shows that is not already in `optimizer`/`database`: which destinations each selected airline serves, labels, the airline-vs-airline table, scrape route counts, map arcs and the Discount Den meter text. `app.py` stays a thin layer of widgets that calls it. Widget state lives in `st.session_state` keys so callbacks and `AppTest` can drive it.

**Tech Stack:** Python 3.13, Streamlit ≥1.63 (`AppTest`), pandas, pydeck, plotly, pytest (offline).

**Spec:** `docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md` §6 (this is §8 Phase 4), plus items earlier phases deferred here: default airlines flip to all, ntfy topic hidden, dashboard controls for memberships/parking.

## Global Constraints

- Project root `~/avelo-finder` (never `~/Desktop/avelo-finder`). Branch `phase4-dashboard` from `main`. Baseline before Task 1: the suite count on `main` after the Phase 3 merge (record it in the first report).
- EVERY python/pytest command an agent runs starts with `AVELO_DB_PATH=<project>/.superpowers/probe/sdd-scratch/agent-sandbox.db AVELO_OFFLINE=1`. `data/flights.db` is off limits; real-shaped data only via `sqlite3 "file:data/flights.db?mode=ro" ".backup '<copy>'"`. Never weaken the root `conftest.py` guard. Never launch a real scrape, browser or URL from a test (`subprocess.Popen` and `webbrowser.open` are monkeypatched).
- No network in tests. Never print or display `AVELO_NTFY_TOPIC` — after this phase the dashboard only says whether a topic is set.
- Streamlit ≥1.63: `width="stretch"`; a unique `key` for every `st.plotly_chart`; widgets driven from callbacks use `session_state` keys, never `value=`; `AppTest.from_file` needs an absolute path; AppTest tests are slow (30–90 s each) — add few, make each count.
- Logic the dashboard needs goes in `views.py` as pure functions with unit tests; `app.py` only wires widgets to them.
- Session-state keys (exact): `home_select` (list of home codes), `airline_select` (list of airline codes), `open_jaw` (bool), `count_carry_on` (bool), `dest_select` (list of destination codes, existing).
- Airline names and colours come from `airlines.get(code)`; the owner pays `price` (membership already applied at save) — never recompute it in the dashboard.
- A destination is offered when at least one selected airline serves it from at least one selected home airport (route catalog; documented fallbacks when an airline's catalog is empty).
- Defaults must reproduce today's screen for an owner with only Avelo data: all active home airports, every known airline, open-jaw off, carry-on off.
- Commits end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; stage only touched files.

## Review Focus

1. **An owner with only Avelo fares** (today's real database) must see the same packages as before the phase, with no exception and no empty screen caused by the new filters. → AppTest in Task 2.
2. **Deselecting every airline or every home airport** must give a clear message, not a crash or a silent empty table. → tests in Tasks 1 and 2.
3. **Destinations already selected that the new airline/home selection no longer serves** must drop out of the selection instead of raising Streamlit's "default value not in options" error. → AppTest in Task 2.
4. **The scrape button must launch exactly what the screen says** (airlines, home airports, destinations, route count). → tests in Task 4.
5. **Changing a membership in the settings panel** must re-price stored fares and must not raise a price-drop alert. → test in Task 3.
6. **The private ntfy topic must not appear anywhere in the rendered page.** → AppTest in Task 6.

---

## File Structure

| File | Change |
|---|---|
| `config.py` | `ALL_DESTINATIONS`, regions and colours for Frontier airports, `FRONTIER_SHARED_DESTINATIONS`, `AVELO_AIRLINES` default `XP,F9` |
| `views.py` | **new** — pure view logic |
| `app.py` | sidebar controls, settings panel, comparison tab, badges, map, calendar, scrape panel |
| `database.py` | `route_calendar(by_airline=True)` |
| `tests/conftest.py` | pin `AVELO_AIRLINES=XP` for existing tests |
| `tests/test_views.py` | **new** |
| `tests/test_app.py`, `tests/test_config.py`, `tests/test_database.py` | extended |

---

### Task 1: Destination universe and pure view logic (`views.py`)

**Files:**
- Modify: `config.py`, `tests/conftest.py`, `tests/test_config.py`
- Create: `views.py`, `tests/test_views.py`

**Interfaces:**
- Produces in `config`: `ALL_DESTINATIONS: dict[str, Airport]` (= `DESTINATIONS` ∪ `FRONTIER_AIRPORTS`); `REGIONS` lists every region of `ALL_DESTINATIONS` (Avelo's four first, in today's order); `REGION_COLORS` has a colour for each; `region_codes(region)` reads `ALL_DESTINATIONS`; `FRONTIER_SHARED_DESTINATIONS: tuple[str, ...]` (Avelo destinations Frontier also sells from PHL/BWI, used only as a fallback when the catalog is empty); `ACTIVE_AIRLINES` default becomes `["XP", "F9"]`.
- Produces in `views`: `served_destinations(airline_codes: Iterable[str], homes: Iterable[str], catalog: dict[str, list[tuple[str, str]]]) -> dict[str, list[str]]` (destination → sorted airline codes; `catalog` maps airline → directional pairs, possibly empty); `destination_label(code: str, served_by: list[str]) -> str`; `scrape_plan(airline_codes, homes, destinations, catalog) -> dict[str, int]` (airline → number of directional routes a scrape would request); `airline_comparison(itineraries: list[Itinerary]) -> pd.DataFrame`; `map_arcs(homes, served: dict[str, list[str]], selected: Iterable[str], catalog) -> list[dict]`; `program_meter(program: dict, itineraries: list[Itinerary]) -> str | None`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_views.py`:

```python
from datetime import datetime

import pandas as pd

import config
import views
from models import Flight
from optimizer import GetawayQuery, Window, build_itineraries

WINDOW = Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))
CATALOG = {
    "XP": [("ILG", "TPA"), ("TPA", "ILG"), ("ILG", "MCO"), ("MCO", "ILG")],
    "F9": [("PHL", "MCO"), ("MCO", "PHL"), ("PHL", "DEN"), ("DEN", "PHL"), ("BWI", "LAS"), ("LAS", "BWI")],
}


def leg(airline, origin, dest, day, hour, price, den=None):
    return Flight(airline=airline, origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour),
                  price=price, base_price=price, member_price=den, duration_minutes=150,
                  flight_id=f"{airline}-{origin}-{dest}-{hour}")


def test_destinations_follow_the_selected_airlines_and_homes():
    both = views.served_destinations(["XP", "F9"], ["ILG", "PHL", "BWI"], CATALOG)
    assert both == {"DEN": ["F9"], "LAS": ["F9"], "MCO": ["F9", "XP"], "TPA": ["XP"]}
    assert views.served_destinations(["F9"], ["PHL"], CATALOG) == {"DEN": ["F9"], "MCO": ["F9"]}
    assert views.served_destinations(["XP"], ["PHL"], CATALOG) == {}
    assert views.served_destinations([], ["ILG"], CATALOG) == {} and views.served_destinations(["XP"], [], CATALOG) == {}
    assert views.destination_label("MCO", ["F9", "XP"]) == "MCO · Orlando · Avelo, Frontier"
    assert views.destination_label("DEN", ["F9"]) == "DEN · Denver · Frontier"


def test_empty_catalog_falls_back_to_known_airports():
    served = views.served_destinations(["XP", "F9"], ["ILG", "PHL"], {"XP": [], "F9": []})
    assert set(served) == set(config.DESTINATIONS) | set(config.FRONTIER_AIRPORTS)
    assert served["TPA"] == ["F9", "XP"] if "TPA" in config.FRONTIER_SHARED_DESTINATIONS else served["TPA"] == ["XP"]
    assert served["DEN"] == ["F9"]
    assert views.served_destinations(["F9"], ["ILG"], {"F9": []}) == {}      # Frontier has no page for ILG
    assert set(views.served_destinations(["XP"], ["PHL"], {"XP": []})) == set(config.DESTINATIONS)


def test_scrape_plan_counts_what_run_scraper_would_request():
    plan = views.scrape_plan(["XP", "F9"], ["ILG", "PHL"], ["TPA", "MCO", "DEN"], CATALOG)
    assert plan == {"XP": 8, "F9": 4}       # Avelo: 2 Avelo destinations × 2 homes × 2 directions; Frontier: catalog pairs
    assert views.scrape_plan(["F9"], ["BWI"], ["TPA"], CATALOG) == {"F9": 0}
    assert views.scrape_plan([], ["ILG"], ["TPA"], CATALOG) == {}


def test_airline_comparison_puts_each_airlines_best_side_by_side():
    outs = [leg("XP", "ILG", "MCO", 8, 8, 120), leg("F9", "PHL", "MCO", 8, 9, 60, den=59), leg("F9", "PHL", "MCO", 9, 9, 40)]
    backs = [leg("XP", "MCO", "ILG", 11, 15, 100), leg("F9", "MCO", "PHL", 11, 16, 50)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["ILG", "PHL"]))
    df = views.airline_comparison(its)
    assert list(df.columns) == ["Window", "Destination", "Avelo", "Avelo from", "Frontier", "Frontier from", "Cheaper", "Saves"]
    (row,) = df.to_dict("records")
    assert (row["Avelo"], row["Avelo from"], row["Frontier"], row["Frontier from"]) == (220.0, "ILG", 90.0, "PHL")
    assert (row["Cheaper"], row["Saves"]) == ("Frontier", 130.0)
    only_avelo = views.airline_comparison([i for i in its if i.airlines == ("XP", "XP")])
    assert pd.isna(only_avelo.iloc[0]["Frontier"]) and only_avelo.iloc[0]["Cheaper"] == "Avelo"
    assert views.airline_comparison([]).empty


def test_map_arcs_are_per_home_and_airline():
    served = {"MCO": ["F9", "XP"], "DEN": ["F9"], "TPA": ["XP"]}
    arcs = views.map_arcs(["ILG", "PHL"], served, ["MCO", "DEN"], CATALOG)
    assert {(a["home"], a["code"], a["airline"]) for a in arcs} == {("ILG", "MCO", "XP"), ("PHL", "MCO", "F9"), ("PHL", "DEN", "F9")}
    assert all(len(a["color"]) == 3 and len(a["source"]) == 2 and a["airline_name"] in ("Avelo", "Frontier") for a in arcs)


def test_program_meter_text():
    den = {"airline": "F9", "program": "Discount Den", "legs": 2, "would_have_saved": 12.0}
    outs, backs = [leg("F9", "PHL", "MCO", 8, 9, 60, den=55)], [leg("F9", "MCO", "PHL", 11, 16, 50, den=46)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["PHL"]))
    text = views.program_meter(den, its)
    assert "Discount Den" in text and "$12" in text and "$9" in text          # booked legs, and the best listed package
    assert views.program_meter({**den, "legs": 0, "would_have_saved": 0.0}, []) is None
```

In `tests/conftest.py`, next to the `AVELO_HOME_AIRPORTS` pin, add `os.environ["AVELO_AIRLINES"] = "XP"` with the comment "existing tests were written for an Avelo-only default; tests that need Frontier set `config.ACTIVE_AIRLINES` themselves". In `tests/test_config.py` update the region assertions to the new lists and add:

```python
def test_destination_universe_covers_both_airlines():
    assert set(config.ALL_DESTINATIONS) == set(config.DESTINATIONS) | set(config.FRONTIER_AIRPORTS)
    assert config.REGIONS[:4] == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert set(config.REGION_COLORS) == set(config.REGIONS) == {a.region for a in config.ALL_DESTINATIONS.values()}
    assert "DEN" in config.region_codes("West") and "MIA" in config.region_codes("Florida")
    assert set(config.FRONTIER_SHARED_DESTINATIONS) <= set(config.DESTINATIONS)
    assert config.parse_airline_codes("XP,F9") == ["XP", "F9"]
```

- [ ] **Step 2: Run tests to verify they fail** — expected `ModuleNotFoundError: No module named 'views'`.

- [ ] **Step 3: `config.py`** — after `FRONTIER_AIRPORTS`:

```python
ALL_DESTINATIONS: dict[str, Airport] = {**DESTINATIONS, **FRONTIER_AIRPORTS}
# Avelo destinations Frontier also sells from PHL/BWI. Only a fallback for the dashboard's destination list
# when the route catalog has not been filled yet; the catalog is the truth once it has.
FRONTIER_SHARED_DESTINATIONS: tuple[str, ...] = ("MCO", "TPA", "RSW", "SJU", "BNA", "RDU", "PBI")
```

`REGIONS = list(dict.fromkeys(a.region for a in ALL_DESTINATIONS.values()))`; `region_codes` iterates `ALL_DESTINATIONS`; add to `REGION_COLORS`: `"Texas & Gulf": (213, 94, 0)`, `"West": (86, 180, 233)`, `"Midwest": (240, 228, 66)`, `"Mexico & Central America": (120, 120, 120)`. `ALL_AIRPORTS` becomes `{**HOME_AIRPORT_INFO, **ALL_DESTINATIONS}`. Change the `AVELO_AIRLINES` default to `"XP,F9"` and its comment to "which airlines a scrape covers when --airlines is not given, and the cap for the nightly job". Update `.env.example`.

- [ ] **Step 4: Create `views.py`**

```python
"""What the dashboard shows, computed without Streamlit so it can be unit-tested.

`app.py` owns widgets and layout; everything it needs to know about airlines,
home airports and packages beyond `optimizer`/`database` is decided here.
"""
from __future__ import annotations

from collections.abc import Iterable

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


def scrape_plan(airline_codes: Iterable[str], homes: Iterable[str], destinations: Iterable[str], catalog: Catalog) -> dict[str, int]:
    """Directional routes a scrape of this selection requests, per airline (mirrors run_scraper.routes_for)."""
    homes, wanted = [h.upper() for h in homes], {d.upper() for d in destinations}
    plan: dict[str, int] = {}
    for airline in (a.upper() for a in airline_codes):
        if airline == "F9":
            plan[airline] = 2 * len({(h, d) for h, d in _pairs(airline, homes, catalog) if d in wanted}) if catalog.get(airline) else 0
        else:
            plan[airline] = 2 * len(homes) * len(wanted & set(config.DESTINATIONS))
    return plan


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


def program_meter(program: dict, itineraries: list[Itinerary]) -> str | None:
    """Badge text: what the program would have saved on booked legs, and would save on the best listed package."""
    best = max((it.program_savings for it in itineraries), default=0.0)
    if not program.get("legs") and best <= 0:
        return None
    parts = []
    if program.get("legs"):
        parts.append(f"${program['would_have_saved']:,.0f} on {program['legs']} booked leg{'s' if program['legs'] != 1 else ''}")
    if best > 0:
        parts.append(f"up to ${best:,.0f} on a package listed now")
    return f"{program['program']} would save: " + " · ".join(parts)
```

Note for the implementer: `airline_comparison` orders the airline columns Avelo then Frontier; derive the order from the registry by name if `sorted(..., reverse=True)` on codes ever stops giving that (add an assertion in the test, which already pins the column list).

- [ ] **Step 5: Run the whole suite** → all pass (existing tests see `AVELO_AIRLINES=XP` through the conftest pin).

- [ ] **Step 6: Commit**

```bash
git add config.py views.py .env.example tests/conftest.py tests/test_views.py tests/test_config.py
git commit -m "feat: destination universe for both airlines and pure dashboard view logic"
```

---

### Task 2: Sidebar — home airports, airlines, open-jaw, carry-on, destinations

**Files:**
- Modify: `app.py` (`ALL_CODES` uses, `_init_selection_state`, `_on_select_all`, `_on_region`, `_on_airports`, `destination_picker`, `sidebar`, `rule_panel`, `run_search` already keyed)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `views.served_destinations/destination_label` (Task 1); `database.catalog_routes(airline, origins=homes)`; `GetawayQuery(origins=, airlines=, allow_open_jaw=, count_carry_on=)`; `alerts.rule_payload(..., origins=, airlines=, allow_open_jaw=, partner=, count_carry_on=)`.
- Produces: session-state keys `home_select`, `airline_select`, `open_jaw`, `count_carry_on`; `app.load_catalog(homes: tuple[str, ...], _version: float) -> dict` (cached: airline → pairs for every known airline); `app.trip_source_picker() -> tuple[list[str], list[str]]` (homes, airlines); `destination_picker(served: dict[str, list[str]]) -> list[str]`; `st.session_state.filters` gains real `origins`, `airlines`, `allow_open_jaw`, `count_carry_on`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_app.py`:

```python
def test_default_screen_matches_avelo_only_data(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    assert next(m for m in at.multiselect if m.label == "Home airports").value == list(config.HOME_CODES)
    assert next(m for m in at.multiselect if m.label == "Airlines").value == sorted(config.KNOWN_AIRLINES)
    assert at.metric[2].value != "0"                                    # packages still found
    dest = next(m for m in at.multiselect if m.label == "Airports")
    assert set(config.DESTINATIONS) <= set(dest.options) and "DEN" in dest.options     # Frontier airports offered too
    assert any(c.label == "Return to a different home airport" and c.value is False for c in at.checkbox)
    assert any(c.label == "Count a carry-on bag" and c.value is False for c in at.checkbox)


def test_narrowing_airlines_and_homes_prunes_destinations_without_errors(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP"]).run()
    assert not at.exception, at.exception
    dest = next(m for m in at.multiselect if m.label == "Airports")
    assert "DEN" not in dest.options and "DEN" not in dest.value and "TPA" in dest.value
    next(m for m in at.multiselect if m.label == "Airlines").set_value([]).run()
    assert not at.exception, at.exception
    assert any("Pick at least one airline" in w.value for w in at.warning)
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP", "F9"]).run()
    next(m for m in at.multiselect if m.label == "Home airports").set_value([]).run()
    assert not at.exception and any("Pick at least one home airport" in w.value for w in at.warning)


def test_saved_rule_carries_the_sidebar_selection(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Home airports").set_value(["ILG"]).run()
    next(m for m in at.multiselect if m.label == "Airlines").set_value(["XP"]).run()
    next(c for c in at.checkbox if c.label == "Count a carry-on bag").set_value(True).run()
    next(t for t in at.text_input if t.label == "Rule name").set_value("Avelo from ILG").run()
    next(b for b in at.button if b.label == "Save rule").click().run()
    assert not at.exception, at.exception
    payload = database.list_watches("rule")[0]["payload"]
    assert (payload["origins"], payload["airlines"], payload["count_carry_on"], payload["allow_open_jaw"]) == (["ILG"], ["XP"], True, False)
```

- [ ] **Step 2: Run tests to verify they fail** — expected `StopIteration` (no "Home airports" multiselect).

- [ ] **Step 3: Implement in `app.py`**

Add `import views` and replace the module-level `ALL_CODES = list(config.DESTINATIONS)` with `ALL_CODES = list(config.ALL_DESTINATIONS)` (the full universe; the offered subset is computed per run). Add the cached catalog loader next to the other loaders:

```python
@st.cache_data(show_spinner=False)
def load_catalog(homes: tuple[str, ...], _version: float) -> dict[str, list[tuple[str, str]]]:
    """Airline → directional routes it lists from these home airports (empty until a scrape fills the catalog)."""
    return {code: database.catalog_routes(code, origins=list(homes)) for code in config.KNOWN_AIRLINES}
```

New picker, called first in `sidebar()`:

```python
def trip_source_picker() -> tuple[list[str], list[str]]:
    """Which home airports to leave from and whose fares to use."""
    st.sidebar.subheader("From & with")
    if "home_select" not in st.session_state:
        st.session_state.home_select = list(config.HOME_CODES)
    if "airline_select" not in st.session_state:
        st.session_state.airline_select = sorted(config.KNOWN_AIRLINES)
    homes = st.sidebar.multiselect("Home airports", options=list(config.HOME_CODES), key="home_select",
                                   format_func=lambda c: config.HOME_AIRPORTS[c].label)
    airline_codes = st.sidebar.multiselect("Airlines", options=sorted(config.KNOWN_AIRLINES), key="airline_select",
                                           format_func=lambda c: airlines.get(c).name)
    st.sidebar.checkbox("Return to a different home airport", key="open_jaw",
                        help="Off: every trip returns to the airport it left from (where your car is). "
                             "On: also show trips that come back into another home airport.",
                        disabled=len(homes) < 2)
    st.sidebar.checkbox("Count a carry-on bag", key="count_carry_on",
                        help=f"Adds each airline's carry-on fee per flight "
                             f"({', '.join(f'{airlines.get(c).name} ${config.CARRY_ON_FEE[c]:.0f}' for c in sorted(config.KNOWN_AIRLINES))}; estimates you can change in .env).")
    if not homes:
        st.sidebar.warning("Pick at least one home airport.")
    if not airline_codes:
        st.sidebar.warning("Pick at least one airline.")
    return list(homes), list(airline_codes)
```

(`import airlines` in `app.py`.) The destination picker works on the offered subset. Replace the four state helpers and `destination_picker` with versions that take `offered: list[str]` (the codes of `served`, in `ALL_CODES` order) instead of the global list:

```python
def _offered() -> list[str]:
    return st.session_state.get("dest_offered", list(ALL_CODES))


def _init_selection_state() -> None:
    if "dest_select" not in st.session_state:
        st.session_state.dest_select = [c for c in _offered() if c in config.DESTINATIONS] or list(_offered())
    offered = set(_offered())
    st.session_state.dest_select = [c for c in st.session_state.dest_select if c in offered]   # never keep an unoffered code
    for region in config.REGIONS:
        codes = set(config.region_codes(region)) & offered
        st.session_state[f"region_{region}"] = bool(codes) and codes <= set(st.session_state.dest_select)
    st.session_state.select_all = bool(offered) and set(st.session_state.dest_select) == offered


def _on_select_all() -> None:
    st.session_state.dest_select = list(_offered()) if st.session_state.select_all else []


def _on_region(region: str) -> None:
    current = set(st.session_state.dest_select)
    codes = set(config.region_codes(region)) & set(_offered())
    current = current | codes if st.session_state[f"region_{region}"] else current - codes
    st.session_state.dest_select = [c for c in _offered() if c in current]


def _on_airports() -> None:
    pass  # region and select-all boxes are recomputed from dest_select on the next run


def destination_picker(served: dict[str, list[str]]) -> list[str]:
    st.session_state.dest_offered = [c for c in ALL_CODES if c in served]
    _init_selection_state()
    st.sidebar.subheader("Destinations")
    if not served:
        st.sidebar.caption("No destinations for this choice of home airports and airlines.")
        return []
    st.sidebar.checkbox("Select all routes", key="select_all", on_change=_on_select_all)
    cols = st.sidebar.columns(2)
    regions = [r for r in config.REGIONS if set(config.region_codes(r)) & set(served)]
    for i, region in enumerate(regions):
        n = len(set(config.region_codes(region)) & set(served))
        cols[i % 2].checkbox(f"{region} ({n})", key=f"region_{region}", on_change=_on_region, args=(region,))
    st.sidebar.multiselect("Airports", options=st.session_state.dest_offered, key="dest_select", on_change=_on_airports,
                           format_func=lambda c: views.destination_label(c, served.get(c, [])))
    return list(st.session_state.dest_select)
```

The default selection is the Avelo destinations (today's behaviour) when any are offered. In `sidebar()`: change the title to `"✈️ Getaway Finder"` and remove the "Nonstop on Avelo from …" caption; before the destinations call

```python
    homes, airline_codes = trip_source_picker()
    served = views.served_destinations(airline_codes, homes, load_catalog(tuple(homes), _db_version()))
    destinations = destination_picker(served)
```

build the query only when `homes and airline_codes and destinations and windows`, with `origins=homes, airlines=set(airline_codes), allow_open_jaw=bool(st.session_state.open_jaw) and len(homes) > 1, count_carry_on=bool(st.session_state.count_carry_on)`; store the same four values in `st.session_state.filters`. The "⚡ This weekend" button selects `list(_offered())` instead of `ALL_CODES`. `rule_panel`: pass `airlines=f.get("airlines")` and `count_carry_on=f.get("count_carry_on", False)` (origins/open-jaw already flow from `filters`). Replace remaining `config.HOME_CODES` uses that mean "the homes being searched" in `load_selection_stats`, `load_freshness`, `calendar_heatmap` and the page title with the selected homes (pass them as arguments; cached loaders take them as tuple parameters). In `main()`, when `query is None` and the cause is an empty home or airline selection, show `st.info("Pick at least one home airport and one airline in the sidebar.")`.

- [ ] **Step 4: Run the whole suite** → all pass (existing AppTests that read `at.multiselect[0]` for destinations must look the widget up by label "Airports" instead).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: choose home airports, airlines, open-jaw and carry-on in the sidebar"
```

---

### Task 3: Settings panel — memberships and home-airport costs

**Files:**
- Modify: `app.py` (new `settings_panel()` called from `sidebar()` after `rule_panel()`; `run_search` unaffected)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `database.memberships()`, `database.set_membership(airline, is_member)` (re-prices and resets watch baselines — Phase 2/3), `database.home_costs()`, `database.set_home_cost(code, drive_min, parking_per_day)`.
- Produces: sidebar expander "⚙️ Settings" with one checkbox per airline (label `"<program> member"`, key `member_<code>`) and, per active home airport, number inputs "Parking $/day" (key `parking_<code>`) and "Drive from home (min)" (key `drive_<code>`, 0 = not set) plus a "Save settings" button (key `save_settings`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_app.py`:

```python
def test_settings_panel_changes_membership_and_home_costs(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    den = next(c for c in at.checkbox if c.label == "Discount Den member")
    assert den.value is False and next(c for c in at.checkbox if c.label == "Avelo PLUS member").value is True
    den.set_value(True).run()
    next(n for n in at.number_input if n.key == "parking_PHL").set_value(22.0).run()
    next(n for n in at.number_input if n.key == "drive_PHL").set_value(35).run()
    next(b for b in at.button if b.key == "save_settings").click().run()
    assert not at.exception, at.exception
    assert database.memberships() == {"XP": True, "F9": True}
    assert database.home_costs()["PHL"] == config.HomeCost(35.0, 22.0)
    assert database.home_costs()["ILG"].drive_min is None                 # 0 minutes means "not set"
    assert database.list_events(notify_only=True) == []                   # no alert from the membership change
```

- [ ] **Step 2: Run test to verify it fails** — expected `StopIteration` (no "Discount Den member" checkbox).

- [ ] **Step 3: Implement**

```python
def settings_panel() -> None:
    """Owner settings that change what a fare costs you: memberships and home-airport costs."""
    with st.sidebar.expander("⚙️ Settings"):
        member = database.memberships()
        for code in sorted(config.KNOWN_AIRLINES, reverse=True):
            st.checkbox(f"{airlines.get(code).member_program} member", value=member[code], key=f"member_{code}",
                        help="Stored fares are re-priced to what you would pay; price alerts restart from the new prices.")
        costs = database.home_costs()
        for code in config.HOME_CODES:
            st.caption(config.HOME_AIRPORTS[code].label)
            c1, c2 = st.columns(2)
            c1.number_input("Parking $/day", min_value=0.0, max_value=200.0, step=1.0,
                            value=float(costs[code].parking_per_day), key=f"parking_{code}")
            c2.number_input("Drive from home (min)", min_value=0, max_value=600, step=5,
                            value=int(costs[code].drive_min or 0), key=f"drive_{code}",
                            help="0 = not set; the drive is then left out of door-to-door cost.")
        if st.button("Save settings", key="save_settings"):
            for code in config.KNOWN_AIRLINES:
                wanted = bool(st.session_state[f"member_{code}"])
                if wanted != member[code]:
                    database.set_membership(code, wanted)
            for code in config.HOME_CODES:
                drive = int(st.session_state[f"drive_{code}"])
                database.set_home_cost(code, float(drive) if drive > 0 else None, float(st.session_state[f"parking_{code}"]))
            _search_cache().clear()
            st.success("Settings saved.")
```

These widgets use `value=` because nothing else writes their keys (they are not callback-driven). Update the comment in `config.py` beside `DEFAULT_HOME_COSTS` ("editable in the dashboard's Settings panel").

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add app.py config.py tests/test_app.py
git commit -m "feat: settings panel for memberships and home-airport parking and drive time"
```

---

### Task 4: Scrape exactly what is selected

**Files:**
- Modify: `app.py` (`scraper_panel`, "This weekend" pending scrape)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `views.scrape_plan` (Task 1); `run_scraper.build_command(..., origins=, airlines=)`.
- Produces: `scraper_panel(destinations, start, end, homes, airline_codes, catalog)`; the caption reads `Target: <N> routes × <D> days · Avelo <n1> · Frontier <n2>`; the launched command carries `--origins` and `--airlines` equal to the sidebar selection; promo help says the code applies to Avelo searches.

- [ ] **Step 1: Write the failing test** — append to `tests/test_app.py`:

```python
def test_scrape_button_launches_the_selection(mock_db, monkeypatch):
    launched = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return 0

    def fake_popen(cmd, **_kw):
        launched["cmd"] = cmd
        return FakeProc()

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(m for m in at.multiselect if m.label == "Home airports").set_value(["ILG", "PHL"]).run()
    next(m for m in at.multiselect if m.label == "Airports").set_value(["TPA", "MCO"]).run()
    assert any("Avelo 8" in c.value and "Frontier 2" in c.value for c in at.sidebar.caption)
    next(b for b in at.button if "Execute Target Scrape" in b.label).click().run()
    cmd = launched["cmd"]
    assert cmd[cmd.index("--origins") + 1] == "ILG,PHL" and cmd[cmd.index("--airlines") + 1] == "F9,XP"
    assert cmd[cmd.index("--destinations") + 1] == "TPA,MCO"
```

- [ ] **Step 2: Run test to verify it fails** — expected: no caption with "Frontier 2"; `--airlines` missing from the command.

- [ ] **Step 3: Implement** — `scraper_panel` gains `homes, airline_codes, catalog` parameters. Caption:

```python
    plan = views.scrape_plan(airline_codes, homes, destinations, catalog)
    days = (end - start).days + 1
    per_airline = " · ".join(f"{airlines.get(code).name} {n}" for code, n in sorted(plan.items(), reverse=True))
    st.sidebar.caption(f"Target: {sum(plan.values())} routes × {days} days · {per_airline or 'nothing selected'}"
                       + (f" · promo {promo}" if promo else ""))
```

Disable the button when `not destinations or not homes or not airline_codes or running`. `build_command(..., origins=list(homes), airlines=sorted(airline_codes))`. The pending "This weekend" scrape stores and reuses the homes and airlines selected when it was clicked. Promo help: "Typed into Avelo's search form on every Avelo search. Fares scraped with a code are tagged with it." `sidebar()` passes the three new arguments.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: the scrape button requests the selected airlines and home airports"
```

---

### Task 5: "Airline vs airline" tab

**Files:**
- Modify: `app.py` (new tab between "Getaway packages" and "Trend & weather")
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `views.airline_comparison(itineraries)` (Task 1).
- Produces: tab "Airline vs airline" rendering the comparison dataframe (money columns formatted `$%d`), a caption explaining the cost basis, and an info message when fewer than two airlines have packages.

- [ ] **Step 1: Write the failing test** — append to `tests/test_app.py` (reuse the Frontier-rows insert from `test_dashboard_shows_frontier_packages_with_their_airline`; extract it into a helper `add_frontier_copy()` in the test module and use it in both tests):

```python
def test_airline_vs_airline_tab_compares_the_same_weekend(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if {"Avelo", "Frontier", "Cheaper"} <= set(df.value.columns)]
    assert tables and len(tables[0]) > 0
    both = tables[0].dropna(subset=["Avelo", "Frontier"])
    assert len(both) > 0 and (both["Saves"] >= 0).all()
    assert set(both["Cheaper"]) <= {"Avelo", "Frontier"}


def test_airline_vs_airline_tab_explains_single_airline_data(mock_db):
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert any("Only one airline has packages" in i.value for i in at.info)
```

- [ ] **Step 2: Run tests to verify they fail** — expected: no dataframe with those columns; no such info message.

- [ ] **Step 3: Implement**

```python
def comparison_tab(itins: list[Itinerary]) -> None:
    """Same free window, same destination: each airline's best package, from whichever home airport is cheapest."""
    df = views.airline_comparison(itins)
    if df.empty:
        st.info("Nothing to compare yet.")
        return
    names = [airlines.get(code).name for code in sorted(config.KNOWN_AIRLINES, reverse=True)]
    if not all(df[n].notna().any() for n in names):
        st.info("Only one airline has packages for this selection. Scrape the other airline for the same dates to compare.")
    st.caption("Cost per package: both fares, plus parking and the drive to the home airport when set in Settings "
               "(door-to-door when a destination place is entered). Mixed-airline packages are in the main table.")
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={**{n: st.column_config.NumberColumn(format="$%d") for n in names},
                                "Saves": st.column_config.NumberColumn(format="$%d", help="How much the cheaper airline saves.")})
```

Add the tab to `st.tabs([...])` after "Getaway packages" and call `comparison_tab(itins)` in it.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: airline vs airline tab"
```

---

### Task 6: Badges and privacy — Discount Den meter, neutral titles, hidden ntfy topic

**Files:**
- Modify: `app.py` (`member_badge`, `rule_panel` caption, `coverage_line`, page title, CSV file name)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `views.program_meter` (Task 1); `bookings.program_savings("F9")`; `database.memberships()`.
- Produces: `member_badge(itins)` shows the Avelo PLUS badge only when the owner is an Avelo member, the Plus ROI badge as today, and a third badge with `views.program_meter(...)` text for each program the owner is **not** in (when it returns text); the rule panel caption says "Push alerts are on (ntfy)" or "Push alerts are off — add AVELO_NTFY_TOPIC to .env" without the topic; `coverage_line` says "member fares" (not "Avelo PLUS member fares").

- [ ] **Step 1: Write the failing tests** — append to `tests/test_app.py`:

```python
def test_ntfy_topic_never_appears_on_screen(mock_db, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "super-secret-topic-123")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception
    rendered = " ".join(str(getattr(el, "value", "")) for group in (at.markdown, at.caption, at.sidebar.caption, at.sidebar.markdown, at.info, at.warning, at.success)
                        for el in group)
    assert "super-secret-topic-123" not in rendered and "Push alerts are on" in rendered


def test_discount_den_meter_appears_when_frontier_packages_are_listed(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert any("Discount Den would save" in m.value for m in at.markdown)
    database.set_membership("F9", True)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not any("Discount Den would save" in m.value for m in at.markdown)      # already a member
```

- [ ] **Step 2: Run tests to verify they fail** — expected: the topic appears in the rule panel caption; no meter text.

- [ ] **Step 3: Implement** — `member_badge(itins: list[Itinerary])`: read `member = database.memberships()`; render the first (Avelo PLUS) pill only if `member["XP"]`; keep the ROI pill; then

```python
    for code in sorted(config.KNOWN_AIRLINES):
        if member[code]:
            continue
        text = views.program_meter(bookings.program_savings(code), [it for it in itins if code in it.airlines])
        if text:
            st.markdown(f'<div style="display:inline-block;padding:6px 12px;border-radius:999px;background:rgba(86,180,233,0.12);'
                        f'border:1px solid rgba(86,180,233,0.5);font-size:0.9rem;margin-bottom:8px">💡 {text}</div>',
                        unsafe_allow_html=True)
```

Call it in `main()` after the packages are built (move the call below `run_search`; when `query is None` call `member_badge([])`). Rule panel caption:

```python
        st.caption(("Push alerts are on (ntfy)" if config.NTFY_TOPIC else "Push alerts are off — add AVELO_NTFY_TOPIC to .env")
                   + "; every alert also appears in the Watchlist & alerts tab.")
```

Page title: `"Flexible Getaways from " + " / ".join(homes)`; CSV download name `getaways.csv`; `coverage_line`: "… are member fares …". `grep -n "NTFY_TOPIC" app.py` must show only boolean uses.

- [ ] **Step 4: Run the whole suite** → all pass (update the existing assertion that looked for the Avelo Plus badge text only if its wording changed).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: Discount Den meter; dashboard no longer shows the ntfy topic"
```

---

### Task 7: Map from every home airport; fare calendar by airline

**Files:**
- Modify: `app.py` (`system_map`, `calendar_heatmap`, `load_calendar`), `database.py` (`route_calendar`)
- Test: `tests/test_database.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `views.map_arcs` (Task 1); `airlines.get(code).color/name`.
- Produces: `database.route_calendar(start_date, end_date, conn=None, airlines=None, by_airline: bool = False)` — with `by_airline=True` rows are grouped by `(airline, origin, destination, day)` and include `airline`; `system_map(selected, homes, served, catalog)` draws one point per offered destination (region colour when selected), one point per selected home airport, and arcs from `views.map_arcs` coloured by airline with a legend naming airlines; `calendar_heatmap` rows are labelled `"<Airline> ORG→DST"` and grouped by airline.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_route_calendar_can_split_by_airline(conn):
    db.upsert_flights([flight(origin="PHL", dest="MCO", day=10, price=100, number="XP1"), f9(price=60.0)], conn)
    merged = db.route_calendar(date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert [(r["origin"], r["destination"], r["min_price"], r["flights"]) for r in merged] == [("PHL", "MCO", 60.0, 2)]
    split = db.route_calendar(date(2026, 10, 1), date(2026, 10, 31), conn=conn, by_airline=True)
    assert {(r["airline"], r["min_price"]) for r in split} == {("XP", 100.0), ("F9", 60.0)}
```

Append to `tests/test_app.py`:

```python
def test_map_and_calendar_render_with_two_airlines(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    add_frontier_copy()
    database.upsert_catalog("F9", [("PHL", "TPA"), ("TPA", "PHL")], "route-page")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    assert any("Frontier" in m.value and "Avelo" in m.value and "home airport" in m.value for m in at.markdown)   # map legend
```

- [ ] **Step 2: Run tests to verify they fail** — expected `TypeError: route_calendar() got an unexpected keyword argument 'by_airline'`; legend text missing.

- [ ] **Step 3: Implement** — `route_calendar`: when `by_airline`, select and group by `airline` as the first column. `load_calendar(start, end, _version)` calls it with `by_airline=True`. `calendar_heatmap(cal, destinations, span, homes)`: filter on `homes`, label rows `airlines.get(r.airline).name + " " + origin + "→" + destination`, order rows by airline name then route. `system_map(selected, homes, served, catalog)`: points = offered destinations (`served` keys; colour by region when selected, `INACTIVE_COLOR` otherwise) + one hub point per selected home (`HUB_COLOR`); `arcs = views.map_arcs(homes, served, selected, catalog)`; the `ArcLayer` uses `get_source_color="color"`, `get_target_color="color"`; tooltip `"{code} · {city}\n{airline_name}"`; legend lists each airline with its colour, then "home airport" and "not selected". Keep the view state; widen the zoom (`zoom=3.2`, `longitude=-88`) when any selected destination is west of −95°.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add app.py database.py tests/test_database.py tests/test_app.py
git commit -m "feat: map draws every home airport by airline; fare calendar split by airline"
```

---

### Task 8: Verify and document

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `.env.example`

- [ ] **Step 1: Dashboard dry run on a copy of the real database** — make a read-only `.backup` copy under `.superpowers/probe/`, then run an `AppTest` of `app.py` against it from a scratch test file (with `AVELO_DB_PATH` set to the copy, `subprocess.Popen` and `webbrowser.open` monkeypatched): the page renders without exception; packages are found; "Airlines" defaults to both; the comparison tab shows the single-airline message (the real database has only Avelo fares). Paste the assertions and result into the report; delete the copy.

- [ ] **Step 2: Live check (controller runs this with the owner — implementers skip it)** — on a copy of the real database: a two-airline scrape from the dashboard's own command (`run_scraper.py --airlines F9,XP --origins ILG,PHL --destinations TPA,MCO ... --mode full --no-alerts`), then start the dashboard against the copy and look at: the sidebar controls, mixed packages, the Airline vs airline tab, the Discount Den meter, the map with both airlines, the calendar split by airline, Settings (toggle Discount Den and watch prices re-price), and "Open on airline site" for a Frontier leg.

- [ ] **Step 3: Documentation** — `CLAUDE.md`: add the `views.py` layout row ("pure dashboard view logic: served destinations, labels, scrape plan, airline comparison, map arcs, program meter"); update the `app.py` row and the Streamlit conventions with the session-state keys from Global Constraints; replace the "no dashboard controls until Phase 4" limitation with what now exists; note `AVELO_AIRLINES` defaults to `XP,F9` and caps the nightly job; update the test count; state that all four phases of the spec are built and list what remains (Frontier route list page 2; carry-on fees are estimates; challenge detection never seen live). `README.md`: rewrite the intro for two airlines and three home airports, document the new sidebar controls, Settings panel and comparison tab. `.env.example`: `AVELO_AIRLINES=XP,F9`.

- [ ] **Step 4: Final full suite and commit**

```bash
git add CLAUDE.md README.md .env.example
git commit -m "docs: phase 4 — dashboard for several airlines and home airports"
```
