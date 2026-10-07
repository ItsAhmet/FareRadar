# Phase 1 — Multiple Home Airports, Trip Identity, Scrape Reliability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single ILG origin with a configurable list of home airports (ILG, PHL, BWI), identify trips by what they are (airline + route + date + time-when-known) instead of by database row, and fix the audit's stale-fare and scrape-reliability bugs — still Avelo-only.

**Architecture:** `config.HOME_CODES` replaces `config.ORIGIN`; `GetawayQuery.origins` + `allow_open_jaw` drive multi-origin pairing in `optimizer.py`; a new pure module `trips.py` defines trip keys, and `database.find_trip()` resolves them, so watches/rules/bookings survive calendar→detail upgrades. A new `persist.py` writes each search result consistently (removing vanished fares); `runlock.py` guarantees one scrape at a time; `scrape_runs.status` keeps partial/failed runs out of signal baselines.

**Tech Stack:** Python 3.13, SQLite (stdlib `sqlite3`), Playwright (async), Streamlit ≥1.63, pytest (offline).

**Spec:** `docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md` (this plan implements §8 Phase 1: parts of §3, §4 "Reliability", §5).

**Roadmap (separate plans, written after this phase lands):**
- Phase 2 — `airline` column + `member_price`, `airlines.py` registry, `route_catalog`, `browser.py` extraction, `frontier.py` scraper + parsers (fixtures in `docs/superpowers/specs/frontier-spike/`).
- Phase 3 — optimizer/alerts/signals/hand-off across airlines (Discount Den savings, per-airline signals, `price_drop` vs last alert, save-after-delivery, partner window in rules, `--from-rules` nightly targets).
- Phase 4 — dashboard (home-airport & airline filters, settings expander, Airline-vs-airline tab, Discount Den meter, multi-hub map, hide ntfy topic).

## Global Constraints

- Project root: `~/avelo-finder` (NOT under iCloud). Run tests with: `cd ~/avelo-finder && AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`. Baseline before Task 1: **90 passed**.
- Tests make no network calls; database tests use `db.get_connection(":memory:")`; every `database.py` function takes an optional `conn`.
- Schema changes: new tables go in `SCHEMA`; new columns go in `MIGRATIONS` (existing data must survive — the real `data/flights.db` held 817 fares and 15 scrape runs on Oct 6 2026, but **no watches, rules or bookings**: legacy watch/rule/booking payloads exist only as the synthetic fixtures in Task 6 and the rows Task 12 seeds into a copy).
- Never modify `data/flights.db` directly during development; Task 12 checks the migration on a **copy**.
- Never print or log the value of `AVELO_NTFY_TOPIC`.
- Streamlit ≥1.63: `width="stretch"`, unique `key` per `st.plotly_chart`, callback-driven widgets use `session_state` keys.
- Phase 1 is Avelo-only: airline code is `"XP"` everywhere; no Frontier code, no `airline` DB column yet.
- Home airports: env `AVELO_HOME_AIRPORTS`, default `"ILG,PHL,BWI"`; unknown codes are ignored; empty result falls back to `["ILG"]`. `config.PRIMARY_HOME` = first active code (used for weather comparison and the map hub until Phase 4).
- Trip key format (exact): `AIRLINE|ORIGIN|DESTINATION|YYYY-MM-DD|HH:MM`, with `*` instead of `HH:MM` when the time is unknown. Day key: `AIRLINE|ORIGIN|DESTINATION|YYYY-MM-DD`.
- Return-airport rule: same home airport unless `allow_open_jaw=True` (default False).
- Home-side costs (refines spec §5): per home airport, an owner-entered `drive_min` (minutes from home; `None` = unknown, not counted) and `parking_per_day` (defaults ILG $0, PHL $17, BWI $14), stored in a new `settings` table. Geocoding the owner's address is a Phase 4 UI nicety.
- Commits: Task 0 initialises git. If the owner declines git, replace every "Commit" step with "Run the full suite; it must pass".

## Review Focus

1. **Watches, rules and bookings saved before this upgrade** (payloads with only `out_id`/`out_dep`, rule state with old-format `matched` keys) must keep resolving and must not push a burst of "new match" alerts on the first run after upgrade. The real database has none of these rows (0 watches, 0 bookings, checked Oct 6 2026), so the evidence is the synthetic tests in Task 6 plus the legacy rows Task 12 Step 1 seeds into a copy. → tests in Task 6, check in Task 12.
2. **The real database's existing `scrape_runs`** has no `status` column and two runs that never finished (#3 from Sep 8 and #12 from Oct 5; 15 runs in all on Oct 6 2026); migration must add the column, a finished legacy run must still be a valid baseline, the unfinished one must not. → tests in Task 9.
3. **`AVELO_HOME_AIRPORTS` with lowercase, spaces, duplicates or unknown codes** (`" phl, xyz,PHL "`) should give `["PHL"]`, and garbage should fall back to `["ILG"]`. → test in Task 1.
4. **A calendar month that is not offered or fails to render** must leave that month's stored fares untouched and must not produce `sold_out` events. → test in Task 7.
5. **A stale `scrape.lock` from a crashed run** (dead pid or garbage contents) must not block the next scrape forever. → tests in Task 10.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `config.py` | modify | `HomeCost`, `HOME_AIRPORT_INFO`, `HOME_CODES`, `HOME_AIRPORTS`, `PRIMARY_HOME`, `DEFAULT_HOME_COSTS`, `is_home()`, `default_routes(destinations, origins)`; remove `ORIGIN`/`ORIGIN_AIRPORT` |
| `models.py` | modify | `Flight.airline: str = "XP"` |
| `trips.py` | **create** | trip/day keys, parsing, matching, legacy-payload conversion, upgrade |
| `persist.py` | **create** | `save_calendar`, `save_day`: write one search result consistently |
| `runlock.py` | **create** | pid lock file `data/scrape.lock` |
| `optimizer.py` | modify | `GetawayQuery.origins`, `allow_open_jaw`; multi-origin pairing; home-side cost fields; `enrich_home`; `inter_home_minutes`; new dataframe columns |
| `database.py` | modify | per-table `MIGRATIONS`; `settings` table + helpers; `home_costs`; list-of-origins queries; `find_trip`; `update_watch_payload`; `update_booking_payload`; `scrape_runs.status`; baseline filter |
| `signals.py` | modify | `_label` via `is_home`; `allow_sold_out` switch |
| `alerts.py` | modify | trip keys in `package_key`; `evaluate_packages`/`evaluate_rules` on trip identity; `watch_package`; rule `origins`/`allow_open_jaw`; `watched_routes` |
| `bookings.py` | modify | resolve legs via trip keys; upgrade payloads |
| `scraper.py` | modify | `DateUnavailable` not retried; calendar returns covered days; run loop uses `persist` |
| `run_scraper.py` | modify | `--origins`; lock; try/finally; run status; `build_command(origins=)` |
| `mock_data.py` | modify | `PRIMARY_HOME` instead of `ORIGIN`; only primary-home routes |
| `app.py` | modify | origins everywhere, `enrich_home`, `watch_package`, cache key, new columns |
| `tests/test_trips.py`, `tests/test_persist.py`, `tests/test_runlock.py` | **create** | new unit tests |
| existing `tests/test_*.py` | modify | as listed per task |

---

### Task 0: Put the project under git

**Files:** none (repository metadata only). `.gitignore` already excludes `.venv/`, `.env`, `data/*.db`, logs and browser state.

- [ ] **Step 1: Initialise and make a baseline commit**

```bash
cd ~/avelo-finder
git init -b main
git add -A
git status --short | head -40   # confirm: no .env, no data/*.db, no .venv
git commit -m "chore: baseline before phase 1 (multi-home, trip identity)"
```

Expected: commit succeeds; `git status` lists no `.env`, `data/flights.db` or `.venv/` files.

- [ ] **Step 2: Confirm the suite is green**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: `90 passed`

---

### Task 1: Home airports in config (and remove `ORIGIN`)

**Files:**
- Modify: `config.py:1-7` (docstring), `config.py:41-42`, `config.py:84`, `config.py:108-115`
- Modify: `optimizer.py:37`, `optimizer.py:157`
- Modify: `alerts.py:176-177`
- Modify: `signals.py:44-45`
- Modify: `mock_data.py:14`, `mock_data.py:44`, `mock_data.py:58`, `mock_data.py:77-80`
- Modify: `app.py` (every `config.ORIGIN` → `config.PRIMARY_HOME`, `config.ORIGIN_AIRPORT` → `config.HOME_AIRPORTS[config.PRIMARY_HOME]`)
- Test: `tests/test_config.py`, `tests/test_run_scraper.py:18-19`

**Interfaces:**
- Produces: `config.HomeCost(drive_min: float | None = None, parking_per_day: float = 0.0)` (frozen dataclass); `config.HOME_AIRPORT_INFO: dict[str, Airport]`; `config.HOME_CODES: list[str]`; `config.HOME_AIRPORTS: dict[str, Airport]`; `config.PRIMARY_HOME: str`; `config.DEFAULT_HOME_COSTS: dict[str, HomeCost]`; `config.parse_home_codes(raw: str) -> list[str]`; `config.is_home(code: str) -> bool`; `config.default_routes(destinations: list[str] | None = None, origins: list[str] | None = None) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests** — replace `tests/test_config.py:4-5` and `:20-22`:

```python
def test_all_sixteen_ilg_destinations_present_and_grouped():
    assert set(config.DESTINATIONS) == {"LAL", "SRQ", "TPA", "MCO", "RSW", "DAB", "FLL", "PBI", "MYR", "ILM", "SAV", "CHS", "GSP", "RDU", "BNA", "SJU"}
    assert config.REGIONS == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert config.region_codes("Tennessee") == ["BNA"]
    assert set(config.REGION_COLORS) == set(config.REGIONS)


def test_home_airports_default_and_parsing():
    assert not hasattr(config, "ORIGIN")
    assert set(config.HOME_AIRPORT_INFO) == {"ILG", "PHL", "BWI"}
    assert config.parse_home_codes("ILG,PHL,BWI") == ["ILG", "PHL", "BWI"]
    assert config.parse_home_codes(" phl, xyz,PHL ") == ["PHL"]
    assert config.parse_home_codes("xyz") == ["ILG"]
    assert config.parse_home_codes("") == ["ILG"]
    assert config.PRIMARY_HOME == config.HOME_CODES[0]
    assert config.is_home("phl") and not config.is_home("TPA")
    assert config.DEFAULT_HOME_COSTS["PHL"].parking_per_day == 17.0
    assert config.DEFAULT_HOME_COSTS["ILG"] == config.HomeCost(None, 0.0)


def test_default_routes_are_both_directions_from_each_home():
    assert config.default_routes(["chs"], origins=["ILG"]) == [("ILG", "CHS"), ("CHS", "ILG")]
    assert config.default_routes(["chs"], origins=["ILG", "PHL"]) == [
        ("ILG", "CHS"), ("CHS", "ILG"), ("PHL", "CHS"), ("CHS", "PHL")]
    assert len(config.default_routes(origins=["ILG"])) == 32
```

In `tests/test_config.py::test_distances_are_sane` add one line:

```python
    assert 20 < config.distance_miles("ILG", "PHL") < 30
```

In `tests/test_run_scraper.py:18` change the routes line to pin the origin:

```python
    routes = config.default_routes(parse_destinations(args.destinations), origins=["ILG"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'HOME_AIRPORT_INFO'` (and `parse_home_codes`).

- [ ] **Step 3: Implement in `config.py`**

Replace the module docstring's second paragraph (`config.py:3-6`) with:

```python
"""Central configuration: home airports, every nonstop destination, scraper tunables.

Home airports (ILG, PHL, BWI) are where trips start and end; which ones are
active comes from AVELO_HOME_AIRPORTS. Destinations are grouped by region for
the sidebar and the map, and carry coordinates so the optimizer can decide
whether two airports are close enough to mix-and-match.
"""
```

Replace `config.py:41-42` (`ORIGIN = ...`, `ORIGIN_AIRPORT = ...`) with:

```python
@dataclass(frozen=True)
class HomeCost:
    """Owner-set cost of starting a trip at a home airport."""

    drive_min: float | None = None  # minutes from your home; None = not set, not counted
    parking_per_day: float = 0.0


HOME_AIRPORT_INFO: dict[str, Airport] = {
    a.code: a
    for a in (
        Airport("ILG", "Wilmington, DE", "Home", 39.6787, -75.6065),
        Airport("PHL", "Philadelphia", "Home", 39.8744, -75.2424),
        Airport("BWI", "Baltimore", "Home", 39.1754, -76.6683),
    )
}

# Parking defaults are estimates the owner can change in the dashboard (settings table).
DEFAULT_HOME_COSTS: dict[str, HomeCost] = {
    "ILG": HomeCost(None, 0.0),
    "PHL": HomeCost(None, 17.0),
    "BWI": HomeCost(None, 14.0),
}


def parse_home_codes(raw: str) -> list[str]:
    """Known home codes from a comma list, in order, de-duplicated; ILG if none."""
    codes = [c.strip().upper() for c in (raw or "").split(",") if c.strip()]
    known = [c for c in dict.fromkeys(codes) if c in HOME_AIRPORT_INFO]
    return known or ["ILG"]


HOME_CODES: list[str] = parse_home_codes(os.getenv("AVELO_HOME_AIRPORTS", "ILG,PHL,BWI"))
HOME_AIRPORTS: dict[str, Airport] = {c: HOME_AIRPORT_INFO[c] for c in HOME_CODES}
PRIMARY_HOME: str = HOME_CODES[0]


def is_home(code: str) -> bool:
    return code.strip().upper() in HOME_AIRPORT_INFO
```

Replace `config.py:84` with:

```python
ALL_AIRPORTS: dict[str, Airport] = {**HOME_AIRPORT_INFO, **DESTINATIONS}
```

Replace `default_routes` (`config.py:108-115`) with:

```python
def default_routes(destinations: list[str] | None = None, origins: list[str] | None = None) -> list[tuple[str, str]]:
    """home→dest and dest→home for each destination and each home airport
    (all destinations and all active home airports by default)."""
    routes: list[tuple[str, str]] = []
    for code in destinations or list(DESTINATIONS):
        code = code.upper()
        for home in origins or HOME_CODES:
            home = home.upper()
            routes.append((home, code))
            routes.append((code, home))
    return routes
```

- [ ] **Step 4: Replace remaining `ORIGIN` uses (keep behaviour; later tasks widen them)**

`optimizer.py:37`:

```python
from config import DESTINATIONS, airport_name, distance_miles
```

`optimizer.py:157` (temporary; Task 2 replaces the field):

```python
    origin: str = config.PRIMARY_HOME
```

`alerts.py:176-177`:

```python
                routes.add((config.PRIMARY_HOME, d))
                routes.add((d, config.PRIMARY_HOME))
```

`signals.py:44-45`:

```python
def _label(origin: str, destination: str) -> str:
    return f"{origin}→{destination} ({airport_name(destination) if config.is_home(origin) else airport_name(origin)})"
```

`mock_data.py:14`:

```python
from config import DESTINATIONS, PRIMARY_HOME, default_routes
```

`mock_data.py:44`: `return int(35 + distance_miles(PRIMARY_HOME, dest) / 7.6)  # taxi + ~455 mph`

`mock_data.py:58`: `far = dest if origin == PRIMARY_HOME else origin`

`mock_data.py:77-80` — the synthetic schedule is ILG-only, so skip routes that don't touch the primary home:

```python
    for origin, dest in routes:
        if PRIMARY_HOME not in (origin, dest):
            continue
        outbound = origin == PRIMARY_HOME
        far = dest if outbound else origin
        if far not in DESTINATIONS:
            continue
```

`app.py`: run `grep -n "config.ORIGIN" app.py` and replace each `config.ORIGIN_AIRPORT` with `config.HOME_AIRPORTS[config.PRIMARY_HOME]` and each remaining `config.ORIGIN` with `config.PRIMARY_HOME`. Afterwards `grep -n "config.ORIGIN" app.py` prints nothing.

- [ ] **Step 5: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (91 = 90 + 1 new config test; `test_default_routes_are_both_directions_from_ilg` was renamed to `..._from_each_home`, not added). Also run `grep -rn "ORIGIN\b" --include=*.py . | grep -v ".venv"` → only `HOME`-style names; no `config.ORIGIN`/`from config import ... ORIGIN`.

- [ ] **Step 6: Commit**

```bash
git add config.py optimizer.py alerts.py signals.py mock_data.py app.py tests/test_config.py tests/test_run_scraper.py
git commit -m "feat: configurable home airports (ILG, PHL, BWI) replace the fixed ILG origin"
```

---

### Task 2: Multi-origin packages and the open-jaw toggle

**Files:**
- Modify: `optimizer.py:153-170` (`GetawayQuery`), `optimizer.py:176-305` (`Itinerary`), `optimizer.py:351-378` (`build_itineraries`), `optimizer.py:455-464` (`find_itineraries`), `optimizer.py:486-520` (`to_dataframe`)
- Modify: `database.py:435-509` (`selection_stats`, `route_freshness`, `route_medians` accept a list of origins)
- Test: `tests/test_optimizer.py`, `tests/test_database.py`

**Interfaces:**
- Consumes: `config.HOME_CODES` (Task 1).
- Produces: `GetawayQuery(destinations, windows, origins: list[str] = <HOME_CODES copy>, min_nights=1, max_budget=None, allow_mix=True, max_pair_miles=100.0, allow_open_jaw: bool = False)`; `Itinerary.home_out -> str`, `Itinerary.home_back -> str`, `Itinerary.is_open_jaw -> bool`, `Itinerary.home_label -> str` (`"ILG"` or `"ILG→PHL"`); dataframe column `"Home"`; `database.selection_stats(origins: str | Iterable[str], ...)`, `database.route_freshness(origins: str | Iterable[str], ...)`, `database.route_medians(origins: str | Iterable[str], ...)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_optimizer.py`:

```python
def test_packages_from_each_home_airport_return_to_the_same_home():
    flights_out = [f("ILG", "TPA", 8, hour=8), f("PHL", "TPA", 8, hour=9, price=60)]
    flights_back = [f("TPA", "ILG", 11, hour=15), f("TPA", "PHL", 11, hour=16, price=60)]
    its = build_itineraries(flights_out, flights_back, q(origins=["ILG", "PHL"]))
    assert {(i.home_out, i.home_back) for i in its} == {("ILG", "ILG"), ("PHL", "PHL")}
    assert {i.home_label for i in its} == {"ILG", "PHL"}
    assert not any(i.is_open_jaw for i in its)


def test_open_jaw_toggle_allows_returning_to_another_home():
    flights_out = [f("ILG", "TPA", 8, hour=8), f("PHL", "TPA", 8, hour=9, price=60)]
    flights_back = [f("TPA", "ILG", 11, hour=15), f("TPA", "PHL", 11, hour=16, price=60)]
    its = build_itineraries(flights_out, flights_back, q(origins=["ILG", "PHL"], allow_open_jaw=True))
    assert len(its) == 4
    jaw = next(i for i in its if i.home_out == "ILG" and i.home_back == "PHL")
    assert jaw.is_open_jaw and jaw.home_label == "ILG→PHL"


def test_unselected_home_airports_are_ignored():
    its = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["ILG"]))
    assert its == []


def test_query_requires_an_origin():
    with pytest.raises(ValueError):
        q(origins=[])


def test_dataframe_has_home_column():
    its = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))
    assert list(to_dataframe(its)["Home"]) == ["PHL"]
```

Append to `tests/test_database.py`:

```python
def test_route_queries_accept_several_origins(conn):
    db.upsert_flights([flight(day=10, price=80), flight(origin="PHL", day=10, price=60, number="XP7"),
                       flight(origin="TPA", dest="PHL", day=12, price=50, number="XP8")], conn)
    med = db.route_medians(["ILG", "PHL"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert med == {("ILG", "TPA"): 80, ("PHL", "TPA"): 60, ("TPA", "PHL"): 50}
    s = db.selection_stats(["ILG", "PHL"], ["TPA"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert s["flights"] == 3 and s["routes"] == 3
    fresh = db.route_freshness(["ILG", "PHL"], ["TPA"], conn=conn)
    assert {(r["origin"], r["destination"]) for r in fresh} == {("ILG", "TPA"), ("PHL", "TPA"), ("TPA", "PHL")}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_optimizer.py tests/test_database.py -q`
Expected: FAIL — `TypeError: GetawayQuery.__init__() got an unexpected keyword argument 'origins'`, and the database test fails on the list argument.

- [ ] **Step 3: Implement `GetawayQuery`** — replace `optimizer.py:153-169`:

```python
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
```

- [ ] **Step 4: Add `Itinerary` home properties** — insert after `Itinerary.destination_city` (`optimizer.py:272`):

```python
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
```

- [ ] **Step 5: Implement multi-origin pairing** — replace `build_itineraries` (`optimizer.py:351-378`):

```python
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
```

Replace `find_itineraries` body lines `optimizer.py:457-458` and `:462`:

```python
    outbounds = query_flights(query.origins, query.destinations, start, end, conn=conn)
    inbounds = query_flights(query.destinations, query.origins, start, end, conn=conn)
```

```python
    medians = route_medians(query.origins, query.destinations, start, end, conn=conn)
```

In `to_dataframe` add `"Home": it.home_label,` directly after the `"Destination"` entry.

- [ ] **Step 6: Accept several origins in `database.py`** — add a helper above `selection_stats` and rewrite the three functions' origin handling:

```python
def _codes(values: str | Iterable[str]) -> list[str]:
    """A code or an iterable of codes, upper-cased, order kept."""
    return [values.upper()] if isinstance(values, str) else [v.upper() for v in values]
```

`selection_stats` (`database.py:435-460`) — signature `origins: str | Iterable[str]`; body:

```python
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return {"flights": 0, "member_fares": 0, "last_scraped": None, "routes": 0}
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    sql = f"""
        SELECT COUNT(*) AS flights,
               SUM(is_member_fare) AS member_fares,
               MAX(timestamp_scraped) AS last_scraped,
               COUNT(DISTINCT origin || '-' || destination) AS routes
        FROM avelo_flights
        WHERE ((origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_})))
          AND departure_datetime >= ? AND departure_datetime < ?
    """
    params = [*origins, *destinations, *origins, *destinations,
              start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    row = dict(_fetch(sql, params, conn)[0])
    row["member_fares"] = row["member_fares"] or 0
    return row
```

`route_freshness` (`database.py:463-478`) — signature `origins: str | Iterable[str]`; body:

```python
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return []
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    sql = f"""
        SELECT origin, destination, MAX(timestamp_scraped) AS last_scraped, COUNT(*) AS fares,
               MIN(substr(departure_datetime,1,10)) AS first_day, MAX(substr(departure_datetime,1,10)) AS last_day
        FROM avelo_flights
        WHERE (origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_}))
        GROUP BY origin, destination
        ORDER BY origin NOT IN ({po}), destination, origin
    """
    return [dict(r) for r in _fetch(sql, [*origins, *destinations, *origins, *destinations, *origins], conn)]
```

`route_medians` (`database.py:481-509`) — signature `origins: str | Iterable[str]`; replace the query part:

```python
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return {}
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    sql = f"""
        SELECT origin, destination, price FROM avelo_flights
        WHERE ((origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_})))
          AND departure_datetime >= ? AND departure_datetime < ?
        ORDER BY origin, destination, price
    """
    params = [*origins, *destinations, *origins, *destinations,
              start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
```

(the median computation below stays unchanged).

- [ ] **Step 7: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (existing callers that pass a single `"ILG"` string still work).

- [ ] **Step 8: Commit**

```bash
git add optimizer.py database.py tests/test_optimizer.py tests/test_database.py
git commit -m "feat: build packages from every home airport, optional open-jaw returns"
```

---

### Task 3: Home-side costs (parking + drive) in door-to-door

**Files:**
- Modify: `database.py` `SCHEMA` (new `settings` table) and new functions after the Bookings section
- Modify: `optimizer.py` (`Itinerary` fields/properties, new `inter_home_minutes`, new `enrich_home`, `to_dataframe`)
- Test: `tests/test_database.py`, `tests/test_optimizer.py`

**Interfaces:**
- Consumes: `config.HomeCost`, `config.DEFAULT_HOME_COSTS`, `config.HOME_AIRPORT_INFO` (Task 1); `Itinerary.home_out/home_back/is_open_jaw` (Task 2).
- Produces: `database.get_setting(key: str, default=None, conn=None)`, `database.set_setting(key: str, value, conn=None) -> None` (JSON-encoded), `database.home_costs(conn=None) -> dict[str, HomeCost]`, `database.set_home_cost(code: str, drive_min: float | None, parking_per_day: float, conn=None) -> None`; `optimizer.inter_home_minutes(a: str, b: str) -> float`; `optimizer.enrich_home(itineraries, costs: dict[str, HomeCost]) -> list[Itinerary]`; `Itinerary.home_drive_min: float | None`, `Itinerary.parking_cost: float`, `Itinerary.home_side_cost -> float`; dataframe column `"Home-side $"`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_settings_and_home_costs(conn):
    assert db.get_setting("nope", 7, conn) == 7
    db.set_setting("x", {"a": 1}, conn)
    assert db.get_setting("x", None, conn) == {"a": 1}
    costs = db.home_costs(conn)
    assert costs["PHL"].parking_per_day == 17.0 and costs["PHL"].drive_min is None
    db.set_home_cost("phl", 40, 20.0, conn)
    assert db.home_costs(conn)["PHL"] == config.HomeCost(40, 20.0)
    assert db.home_costs(conn)["ILG"] == config.DEFAULT_HOME_COSTS["ILG"]
```

(add `import config` at the top of `tests/test_database.py` if it is not already imported).

Append to `tests/test_optimizer.py`:

```python
from config import HomeCost
from optimizer import enrich_home, inter_home_minutes


def test_home_side_costs_add_parking_and_drives():
    costs = {"ILG": HomeCost(20, 0.0), "PHL": HomeCost(40, 17.0), "BWI": HomeCost(None, 14.0)}
    ilg = build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "ILG", 11, hour=15)], q(origins=["ILG"]))[0]
    phl = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))[0]
    bwi = build_itineraries([f("BWI", "TPA", 8, hour=9)], [f("TPA", "BWI", 11, hour=16)], q(origins=["BWI"]))[0]
    enrich_home([ilg, phl, bwi], costs)
    assert (ilg.parking_cost, ilg.home_drive_min) == (0.0, 40)
    assert (phl.parking_cost, phl.home_drive_min) == (51.0, 80)   # 3 nights × $17
    assert bwi.home_drive_min is None and bwi.parking_cost == 42.0
    assert phl.home_side_cost == round(51 + 80 / 60 * 15, 2)


def test_open_jaw_adds_the_drive_between_home_airports():
    costs = {"ILG": HomeCost(20, 0.0), "PHL": HomeCost(40, 17.0)}
    its = build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "PHL", 11, hour=16)],
                            q(origins=["ILG", "PHL"], allow_open_jaw=True))
    (jaw,) = enrich_home(its, costs)
    assert 30 < inter_home_minutes("ILG", "PHL") < 50
    assert jaw.home_drive_min == round(20 + 40 + inter_home_minutes("PHL", "ILG"), 1)
    assert jaw.parking_cost == 0.0   # car is parked at ILG


def test_door_to_door_includes_home_side_when_destination_drives_known():
    costs = {"PHL": HomeCost(40, 17.0)}
    (it,) = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))
    enrich_home([it], costs)
    assert it.door_to_door_cost is None            # destination drives unknown → unchanged behaviour
    enrich_drives([it], {"TPA": 30}, 15.0)
    assert it.door_to_door_cost == round(it.total_price + 60 / 60 * 15 + it.home_side_cost, 2)
    assert "Home-side $" in to_dataframe([it]).columns
```

(add `enrich_drives` to the `from optimizer import (...)` block at the top of the test file).

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_database.py tests/test_optimizer.py -q`
Expected: FAIL — `AttributeError: module 'database' has no attribute 'get_setting'`; `ImportError: cannot import name 'enrich_home'`.

- [ ] **Step 3: Implement settings in `database.py`** — append to `SCHEMA` before the closing `"""`:

```sql
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL      -- JSON
);
```

Add `import config` near the top imports of `database.py` if absent (it already imports from `config` for `DB_PATH`; use `import config` alongside). Append a new section at the end of `database.py`:

```python
# --------------------------------------------------------------------------- #
# Settings (owner preferences: home-airport costs, later memberships)
# --------------------------------------------------------------------------- #


def get_setting(key: str, default=None, conn: sqlite3.Connection | None = None):
    rows = _fetch("SELECT value FROM settings WHERE key = ?", (key,), conn)
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key: str, value, conn: sqlite3.Connection | None = None) -> None:
    _run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
         (key, json.dumps(value)), conn)


def home_costs(conn: sqlite3.Connection | None = None) -> dict[str, config.HomeCost]:
    """Per home airport: owner-set drive minutes and parking $/day over the defaults."""
    stored = get_setting("home_costs", {}, conn) or {}
    out: dict[str, config.HomeCost] = {}
    for code in config.HOME_AIRPORT_INFO:
        default = config.DEFAULT_HOME_COSTS.get(code, config.HomeCost())
        mine = stored.get(code, {})
        drive = mine.get("drive_min", default.drive_min)
        out[code] = config.HomeCost(
            float(drive) if drive is not None else None,
            float(mine.get("parking_per_day", default.parking_per_day)),
        )
    return out


def set_home_cost(code: str, drive_min: float | None, parking_per_day: float,
                  conn: sqlite3.Connection | None = None) -> None:
    stored = get_setting("home_costs", {}, conn) or {}
    stored[code.upper()] = {"drive_min": drive_min, "parking_per_day": float(parking_per_day)}
    set_setting("home_costs", stored, conn)
```

- [ ] **Step 4: Implement home-side costs in `optimizer.py`**

Add `from config import HomeCost` to the imports (next to the existing `from config import ...` line). Add two fields to `Itinerary` after `drive_value_per_hour: float = 15.0`:

```python
    # Filled by `enrich_home()`: getting to and from the home airports.
    home_drive_min: float | None = None  # home→departure airport + return airport→home (+ inter-home if open-jaw)
    parking_cost: float = 0.0            # parking at the departure home airport for the trip
```

Add the property and replace `door_to_door_cost` (`optimizer.py:199-204`):

```python
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
```

Add after `enrich_drives` (`optimizer.py:425`):

```python
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
```

In `to_dataframe` add after `"Door-to-door": it.door_to_door_cost,`:

```python
                "Home-side $": it.home_side_cost,
```

- [ ] **Step 5: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (existing door-to-door test unchanged: `home_side_cost` is 0 when `enrich_home` was never called).

- [ ] **Step 6: Commit**

```bash
git add database.py optimizer.py tests/test_database.py tests/test_optimizer.py
git commit -m "feat: parking and home-airport drive time in door-to-door cost"
```

---

### Task 4: Trip keys (`trips.py`) and `Flight.airline`

**Files:**
- Create: `trips.py`
- Modify: `models.py:29-33` (add `airline` field and normalisation)
- Test: create `tests/test_trips.py`

**Interfaces:**
- Produces: `Flight.airline: str = "XP"` (upper-cased); `trips.ANY_TIME = "*"`; `trips.TripKey(airline, origin, destination, day: date, time: str)` frozen dataclass with `__str__` and `.day_key`; `trips.trip_key(f: Flight) -> str`; `trips.day_key(f: Flight) -> str`; `trips.parse(key: str) -> TripKey` (raises `ValueError`); `trips.matches(a: str, b: str) -> bool`; `trips.legacy_key(flight_id: str, departure_iso: str, airline: str = "XP") -> str | None`; `trips.payload_trip(payload: dict, leg: str) -> str | None` (`leg` is `"out"` or `"in"`); `trips.upgrade(stored: str, found: Flight) -> str`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_trips.py`:

```python
from datetime import date, datetime

import pytest

import trips
from models import Flight


def cal(day=8):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day), price=79,
                  flight_id="LOWFARE-ILG-TPA")


def timed(day=8, hour=8, minute=5):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour, minute), price=79,
                  duration_minutes=150, flight_id="AVELO-ILG-TPA")


def test_flight_airline_defaults_to_avelo_and_normalises():
    assert cal().airline == "XP"
    assert Flight(origin="phl", destination="mco", departure_datetime=datetime(2026, 11, 12), price=1, airline="f9").airline == "F9"


def test_trip_and_day_keys():
    assert trips.trip_key(cal()) == "XP|ILG|TPA|2026-10-08|*"
    assert trips.trip_key(timed()) == "XP|ILG|TPA|2026-10-08|08:05"
    assert trips.day_key(timed()) == trips.day_key(cal()) == "XP|ILG|TPA|2026-10-08"


def test_parse_round_trips_and_rejects_garbage():
    k = trips.parse("XP|ILG|TPA|2026-10-08|08:05")
    assert (k.airline, k.origin, k.destination, k.day, k.time) == ("XP", "ILG", "TPA", date(2026, 10, 8), "08:05")
    assert str(k) == "XP|ILG|TPA|2026-10-08|08:05" and k.day_key == "XP|ILG|TPA|2026-10-08"
    with pytest.raises(ValueError):
        trips.parse("nonsense")


def test_matches_treats_unknown_time_as_wildcard():
    assert trips.matches("XP|ILG|TPA|2026-10-08|*", "XP|ILG|TPA|2026-10-08|08:05")
    assert trips.matches("XP|ILG|TPA|2026-10-08|08:05", "XP|ILG|TPA|2026-10-08|08:05")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|08:05", "XP|ILG|TPA|2026-10-08|09:00")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|*", "F9|ILG|TPA|2026-10-08|*")
    assert not trips.matches("XP|ILG|TPA|2026-10-08|*", "XP|ILG|TPA|2026-10-09|*")


def test_legacy_payloads_convert_to_trip_keys():
    assert trips.legacy_key("LOWFARE-ILG-TPA", "2026-10-08T00:00") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.legacy_key("AVELO-TPA-ILG", "2026-10-11T15:30") == "XP|TPA|ILG|2026-10-11|15:30"
    assert trips.legacy_key("T-ILG-TPA-8", "2026-10-08T08:00") == "XP|ILG|TPA|2026-10-08|08:00"
    assert trips.legacy_key("AVELO-ILG-TPA", "2026-10-08T00:00") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.legacy_key("garbage", "2026-10-08T08:00") is None
    old = {"out_id": "LOWFARE-ILG-TPA", "out_dep": "2026-10-08T00:00", "in_id": "AVELO-TPA-ILG", "in_dep": "2026-10-11T15:30"}
    assert trips.payload_trip(old, "out") == "XP|ILG|TPA|2026-10-08|*"
    assert trips.payload_trip({**old, "in_trip": "XP|TPA|ILG|2026-10-11|16:00"}, "in") == "XP|TPA|ILG|2026-10-11|16:00"


def test_upgrade_pins_a_wildcard_key_to_the_found_flight():
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|*", timed()) == "XP|ILG|TPA|2026-10-08|08:05"
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|*", cal()) == "XP|ILG|TPA|2026-10-08|*"
    assert trips.upgrade("XP|ILG|TPA|2026-10-08|07:00", timed()) == "XP|ILG|TPA|2026-10-08|07:00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_trips.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'trips'`.

- [ ] **Step 3: Add `airline` to `Flight`** — in `models.py` add after `flight_id: str = ""` (line 29):

```python
    airline: str = "XP"  # IATA code: XP = Avelo, F9 = Frontier
```

and in `__post_init__` after the destination normalisation (line 33):

```python
        object.__setattr__(self, "airline", (self.airline or "XP").strip().upper())
```

Update the class docstring's first line to `"""One nonstop flight on one date at one observed price."""`.

- [ ] **Step 4: Create `trips.py`**

```python
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
```

- [ ] **Step 5: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trips.py models.py tests/test_trips.py
git commit -m "feat: trip keys identify flights by airline, route, day and time"
```

---

### Task 5: Resolve trip keys in the database

**Files:**
- Modify: `database.py` (new `find_trip` after `get_flight` at line 627; new `update_watch_payload` after `update_watch_state`; new `update_booking_payload` after `update_booking_state`)
- Test: `tests/test_database.py`

**Interfaces:**
- Consumes: `trips.parse`, `trips.ANY_TIME` (Task 4); `query_flights` (existing, cheapest first).
- Produces: `database.find_trip(key: str, conn=None) -> Flight | None`; `database.update_watch_payload(watch_id: int, payload: dict, conn=None) -> None`; `database.update_booking_payload(booking_id: int, payload: dict, conn=None) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_find_trip_resolves_day_and_timed_keys(conn):
    placeholder = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 10), price=90,
                         flight_id="LOWFARE-ILG-TPA")
    db.upsert_flights([placeholder], conn)
    assert db.find_trip("XP|ILG|TPA|2026-10-10|*", conn).price == 90
    assert db.find_trip("XP|ILG|TPA|2026-10-10|08:00", conn) is None
    # a detail run replaces the placeholder with timed flights
    db.delete_day("ILG", "TPA", date(2026, 10, 10), conn=conn)
    db.upsert_flights([flight(day=10, hour=8, price=95, number="XP1"), flight(day=10, hour=18, price=85, number="XP2")], conn)
    assert db.find_trip("XP|ILG|TPA|2026-10-10|*", conn).departure_datetime.hour == 18   # cheapest that day
    assert db.find_trip("XP|ILG|TPA|2026-10-10|08:00", conn).price == 95
    assert db.find_trip("F9|ILG|TPA|2026-10-10|*", conn) is None
    assert db.find_trip("XP|ILG|TPA|2026-10-11|*", conn) is None


def test_payload_updates(conn):
    wid = db.add_watch("package", "w", {"out_trip": "a"}, conn)
    db.update_watch_payload(wid, {"out_trip": "b"}, conn)
    assert db.list_watches("package", conn)[0]["payload"] == {"out_trip": "b"}
    bid = db.add_booking("b", {"out_trip": "a"}, 100, conn=conn)
    db.update_booking_payload(bid, {"out_trip": "c"}, conn)
    assert db.list_bookings(conn)[0]["payload"] == {"out_trip": "c"}
```

Check `tests/test_database.py`'s `flight()` helper: it builds `departure_datetime=datetime(2026, 10, day, hour)` with a duration (so `time_known` is true). If it does not pass `duration_minutes`, add `duration_minutes=150` to that helper so detail-style rows have a known time.

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_database.py -q`
Expected: FAIL — `AttributeError: module 'database' has no attribute 'find_trip'`.

- [ ] **Step 3: Implement** — add `import trips` to `database.py` imports, then after `get_flight`:

```python
def find_trip(key: str, conn: sqlite3.Connection | None = None) -> Flight | None:
    """The flight a trip key refers to right now.

    Timed key → the flight departing at exactly that time. Day-only key → the
    cheapest timed flight that day if a detail run has seen the day, else the
    calendar row.
    """
    k = trips.parse(key)
    same_day = [f for f in query_flights([k.origin], [k.destination], k.day, k.day, conn=conn) if f.airline == k.airline]
    if k.time != trips.ANY_TIME:
        return next((f for f in same_day if f.time_known and f.departure_datetime.strftime("%H:%M") == k.time), None)
    timed = [f for f in same_day if f.time_known]
    pool = timed or same_day
    return pool[0] if pool else None
```

After `update_watch_state`:

```python
def update_watch_payload(watch_id: int, payload: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE watchlist SET payload = ? WHERE watch_id = ?", (json.dumps(payload, default=str), watch_id), conn)
```

After `update_booking_state`:

```python
def update_booking_payload(booking_id: int, payload: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE bookings SET payload = ? WHERE booking_id = ?", (json.dumps(payload, default=str), booking_id), conn)
```

- [ ] **Step 4: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_database.py
git commit -m "feat: resolve trip keys to current flights"
```

---

### Task 6: Watches, rules and bookings on trip identity

**Files:**
- Modify: `alerts.py:41-51` (`package_key`), `alerts.py:59-101` (`rule_payload`, `rule_query`), `alerts.py:104-105` (`_key_str`), `alerts.py:113-141` (`evaluate_packages`), `alerts.py:144-167` (`evaluate_rules`), `alerts.py:170-184` (`watched_routes`); new `watch_package`
- Modify: `bookings.py:49-82` (`current_total`, `evaluate_bookings`)
- Test: `tests/test_alerts.py`, `tests/test_bookings.py`

**Interfaces:**
- Consumes: `trips.trip_key`, `trips.day_key`, `trips.payload_trip`, `trips.parse`, `trips.matches`, `trips.upgrade` (Task 4); `database.find_trip`, `database.update_watch_payload`, `database.update_booking_payload` (Task 5); `GetawayQuery.origins/allow_open_jaw` (Task 2).
- Produces: `package_key(it)` additionally returns `out_trip`, `in_trip`, `out_day`, `in_day`; `rule_payload(..., holiday_extend=False, origins: list[str] | None = None, allow_open_jaw: bool = False)` adds `"origins"` and `"allow_open_jaw"`; `alerts.watch_package(it, conn=None) -> tuple[int, bool]` (watch id, created?); rule state gains `"key_version": 2`; `bookings.legs(payload, conn=None) -> tuple[Flight | None, Flight | None]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_alerts.py`:

```python
WINDOW = Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))


def cal(origin, dest, day, price):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day), price=price,
                  flight_id=f"LOWFARE-{origin}-{dest}")


def test_watch_survives_calendar_to_detail_upgrade(conn):
    out, back = cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    wid, created = alerts.watch_package(it, conn)
    assert created
    db.update_watch_state(wid, {"last_total": 180.0}, conn)
    # detail run: placeholders replaced by timed rows at the same price
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.delete_day("TPA", "ILG", date(2026, 10, 11), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 8, 19, 100), f("TPA", "ILG", 11, 15, 80)], conn)
    assert alerts.evaluate_packages(conn) == []
    payload = db.list_watches("package", conn)[0]["payload"]
    assert payload["out_trip"] == "XP|ILG|TPA|2026-10-08|19:00" and payload["in_trip"] == "XP|TPA|ILG|2026-10-11|15:00"


def test_watch_package_refuses_duplicates(conn):
    out, back = f("ILG", "TPA", 8, 19, 100), f("TPA", "ILG", 11, 15, 80)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    first, created1 = alerts.watch_package(it, conn)
    second, created2 = alerts.watch_package(it, conn)
    assert created1 and not created2 and first == second
    assert len(db.list_watches("package", conn)) == 1


def test_legacy_package_watch_still_resolves(conn):
    db.upsert_flights([cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)], conn)
    legacy = {"out_id": "LOWFARE-ILG-TPA", "out_dep": "2026-10-08T00:00", "in_id": "LOWFARE-TPA-ILG",
              "in_dep": "2026-10-11T00:00", "window_start": "2026-10-07T18:00", "destination": "TPA"}
    wid = db.add_watch("package", "old", legacy, conn)
    db.update_watch_state(wid, {"last_total": 200.0}, conn)
    events = alerts.evaluate_packages(conn)
    assert [e.kind for e in events] == ["price_drop"]


def test_rule_does_not_rematch_after_upgrade_or_on_legacy_state(conn):
    db.upsert_flights([cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                  span_days=14, origins=["ILG"])
    assert payload["origins"] == ["ILG"] and payload["allow_open_jaw"] is False
    db.add_watch("rule", "Tampa", payload, conn)
    today = date(2026, 10, 1)
    assert [e.kind for e in alerts.evaluate_rules(conn, today=today)] == ["rule_match"]
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 8, 19, 100)], conn)
    assert alerts.evaluate_rules(conn, today=today) == []          # same trip, now with a time

    legacy_id = db.add_watch("rule", "Legacy", payload, conn)
    db.update_watch_state(legacy_id, {"matched": ["LOWFARE-ILG-TPA|2026-10-08T00:00|x|y|z"]}, conn)
    assert alerts.evaluate_rules(conn, today=today) == []          # legacy state: baseline silently
    assert db.list_watches("rule", conn)[1]["state"]["key_version"] == 2


def test_rule_origins_reach_the_query_and_watched_routes(conn):
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                  span_days=14, origins=["ILG", "PHL"], allow_open_jaw=True)
    q = alerts.rule_query(payload, today=date(2026, 10, 1))
    assert q.origins == ["ILG", "PHL"] and q.allow_open_jaw
    old = {k: v for k, v in payload.items() if k not in ("origins", "allow_open_jaw")}
    assert alerts.rule_query(old, today=date(2026, 10, 1)).origins == [config.PRIMARY_HOME]
    db.add_watch("rule", "r", payload, conn)
    assert alerts.watched_routes(conn) >= {("ILG", "TPA"), ("TPA", "ILG"), ("PHL", "TPA"), ("TPA", "PHL")}
```

Append to `tests/test_bookings.py` (the file already has a `conn` fixture and a flight helper at line ~20; read it first and reuse its helper name — shown here as `f`):

```python
def test_booking_follows_its_trip_through_a_detail_upgrade(conn):
    out = Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 8), price=100, flight_id="LOWFARE-ILG-TPA")
    back = Flight(origin="TPA", destination="ILG", departure_datetime=datetime(2026, 10, 11), price=80, flight_id="LOWFARE-TPA-ILG")
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))], origins=["ILG"]))
    db.add_booking("trip", bookings.booking_payload(it), 180.0, conn=conn)
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.upsert_flights([Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 8, 19), price=60,
                              duration_minutes=150, flight_id="AVELO-ILG-TPA")], conn)
    events = bookings.evaluate_bookings(conn)
    b = db.list_bookings(conn)[0]
    assert b["state"]["current_total"] == 140.0
    assert b["payload"]["out_trip"] == "XP|ILG|TPA|2026-10-08|19:00"
    assert [e.kind for e in events] == ["booked_drop"]
```

(ensure `tests/test_bookings.py` imports `GetawayQuery, Window, build_itineraries` from `optimizer`, plus `date`, `datetime`, `Flight`, `bookings`, `database as db` — add any that are missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_alerts.py tests/test_bookings.py -q`
Expected: FAIL — `AttributeError: module 'alerts' has no attribute 'watch_package'`, `TypeError: rule_payload() got an unexpected keyword argument 'origins'`.

- [ ] **Step 3: Implement keys and rule payloads in `alerts.py`** — add `import trips` to the imports. Replace `package_key`:

```python
def package_key(it: Itinerary) -> dict:
    return {
        "out_trip": trips.trip_key(it.outbound),
        "in_trip": trips.trip_key(it.inbound),
        "out_day": trips.day_key(it.outbound),
        "in_day": trips.day_key(it.inbound),
        # legacy fields, still read by the dashboard and by old payload readers
        "out_id": it.outbound.flight_id,
        "out_dep": it.outbound.departure_datetime.isoformat(timespec="minutes"),
        "in_id": it.inbound.flight_id,
        "in_dep": it.inbound.departure_datetime.isoformat(timespec="minutes"),
        "window_start": it.window.start.isoformat(timespec="minutes"),
        "window_end": it.window.end.isoformat(timespec="minutes"),
        "destination": it.destination,
        "route_mix": it.route_mix,
    }
```

Replace `rule_payload` signature and add two keys:

```python
def rule_payload(
    destinations: list[str],
    availability: Availability,
    min_nights: int,
    max_budget: float,
    allow_mix: bool,
    max_pair_miles: float,
    span_days: int = 60,
    holiday_extend: bool = False,
    origins: list[str] | None = None,
    allow_open_jaw: bool = False,
) -> dict:
    return {
        "holiday_extend": holiday_extend,
        "destinations": sorted(d.upper() for d in destinations),
        "origins": list(dict.fromkeys(o.upper() for o in (origins or config.HOME_CODES))),
        "allow_open_jaw": bool(allow_open_jaw),
        "start_weekday": availability.start_weekday,
        "start_time": availability.start_time.strftime("%H:%M"),
        "end_weekday": availability.end_weekday,
        "end_time": availability.end_time.strftime("%H:%M"),
        "min_nights": min_nights,
        "max_budget": max_budget,
        "allow_mix": allow_mix,
        "max_pair_miles": max_pair_miles,
        "span_days": span_days,
    }
```

In `rule_query`'s `GetawayQuery(...)` call add two arguments (rules saved before this change get the primary home, which is what they meant):

```python
        origins=list(payload.get("origins") or [config.PRIMARY_HOME]),
        allow_open_jaw=bool(payload.get("allow_open_jaw", False)),
```

Replace `_key_str`:

```python
def _key_str(key: dict) -> str:
    """Rule-match identity: day-level so a calendar→detail upgrade is not a new match."""
    return "|".join(str(key[k]) for k in ("out_day", "in_day", "window_start"))
```

Add after `_key_str`:

```python
def watch_package(it: Itinerary, conn: sqlite3.Connection | None = None) -> tuple[int, bool]:
    """Watch a package unless the same trip is already watched. Returns (watch_id, created)."""
    key = package_key(it)
    for w in database.list_watches("package", conn):
        p = w["payload"]
        out_k, in_k = trips.payload_trip(p, "out"), trips.payload_trip(p, "in")
        if (out_k and in_k and p.get("window_start") == key["window_start"]
                and trips.matches(out_k, key["out_trip"]) and trips.matches(in_k, key["in_trip"])):
            return w["watch_id"], False
    watch_id = database.add_watch("package", package_label(it), key, conn)
    database.update_watch_state(watch_id, {"last_total": it.total_price}, conn)
    return watch_id, True
```

- [ ] **Step 4: Evaluate packages and rules by trip key** — replace the first lines of the loop in `evaluate_packages` (`alerts.py:115-118`) and add the upgrade before the state update:

```python
    for watch in database.list_watches("package", conn):
        key, state = watch["payload"], dict(watch["state"])
        out_k, in_k = trips.payload_trip(key, "out"), trips.payload_trip(key, "in")
        out = database.find_trip(out_k, conn) if out_k else None
        back = database.find_trip(in_k, conn) if in_k else None
```

(the `if out is None or back is None:` block stays as is). Directly before `state.update({"last_total": ...})` insert:

```python
        upgraded = {**key, "out_trip": trips.upgrade(out_k, out), "in_trip": trips.upgrade(in_k, back)}
        if upgraded != key:
            database.update_watch_payload(watch["watch_id"], upgraded, conn)
```

In `evaluate_rules`, replace `previous = set(state.get("matched", []))` and `new_keys = ...` with:

```python
        legacy_state = "matched" in state and state.get("key_version") != 2
        previous = set(state.get("matched", []))
        # Rules saved before trip keys stored a different key format: adopt the
        # current matches silently instead of announcing them all as new.
        new_keys = [] if legacy_state else [k for k in keys if k not in previous]
```

and add `"key_version": 2,` to the `state.update({...})` dict at the end of the loop.

Replace `watched_routes`:

```python
def watched_routes(conn: sqlite3.Connection | None = None) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for watch in database.list_watches(conn=conn):
        p = watch["payload"]
        if watch["kind"] == "rule":
            homes = p.get("origins") or [config.PRIMARY_HOME]
            for d in p.get("destinations", []):
                for home in homes:
                    routes.add((home, d))
                    routes.add((d, home))
        else:
            for leg in ("out", "in"):
                k = trips.payload_trip(p, leg)
                if k:
                    tk = trips.parse(k)
                    routes.add((tk.origin, tk.destination))
    return routes
```

- [ ] **Step 5: Bookings by trip key** — in `bookings.py` add `import trips` and replace `current_total` and the top of `evaluate_bookings`'s loop:

```python
def legs(payload: dict, conn: sqlite3.Connection | None = None):
    """(outbound, inbound) flights the booking refers to now; None for a missing leg."""
    found = []
    for leg in ("out", "in"):
        k = trips.payload_trip(payload, leg)
        found.append(database.find_trip(k, conn) if k else None)
    return found[0], found[1]


def current_total(payload: dict, conn: sqlite3.Connection | None = None) -> float | None:
    out, back = legs(payload, conn)
    if out is None or back is None:
        return None
    return round(out.price + back.price, 2)
```

In `evaluate_bookings`, replace `now_total = current_total(b["payload"], conn)` with:

```python
        payload = b["payload"]
        out, back = legs(payload, conn)
        now_total = round(out.price + back.price, 2) if out is not None and back is not None else None
        if out is not None and back is not None:
            upgraded = {**payload, "out_trip": trips.upgrade(trips.payload_trip(payload, "out"), out),
                        "in_trip": trips.upgrade(trips.payload_trip(payload, "in"), back)}
            if upgraded != payload:
                database.update_booking_payload(b["booking_id"], upgraded, conn)
```

- [ ] **Step 6: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass, including the pre-existing `test_watched_package_alerts_on_drop_and_on_disappearance` and `test_rule_alerts_only_on_new_matches`.

- [ ] **Step 7: Commit**

```bash
git add alerts.py bookings.py tests/test_alerts.py tests/test_bookings.py
git commit -m "fix: watches, rules and bookings follow trips across calendar→detail upgrades"
```

---

### Task 7: Write search results consistently (`persist.py`)

**Files:**
- Create: `persist.py`
- Test: create `tests/test_persist.py`

**Interfaces:**
- Consumes: `database.upsert_flights`, `database.delete_day`, `database.record_seen`, `database.drop_shadowed_placeholders`, `database.seen_in_run`, `database.start_run` (existing).
- Produces: `persist.save_calendar(run_id: int | None, origin: str, dest: str, covered_days: Iterable[date], flights: list[Flight], conn=None) -> int`; `persist.save_day(run_id: int | None, origin: str, dest: str, day: date, flights: list[Flight], conn=None) -> int`. Both return the number of flights written.

- [ ] **Step 1: Write the failing tests** — create `tests/test_persist.py`:

```python
from datetime import date, datetime

import pytest

import database as db
import persist
from models import Flight


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def cal(day, price, month=10):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, month, day), price=price,
                  flight_id="LOWFARE-ILG-TPA")


def detail(day, hour, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour), price=price,
                  duration_minutes=150, flight_id="AVELO-ILG-TPA")


def days_stored(conn):
    return sorted(f.departure_datetime for f in db.query_flights(["ILG"], ["TPA"], date(2026, 10, 1), date(2026, 11, 30), conn=conn))


def test_calendar_removes_days_that_lost_their_fare(conn):
    run1 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    persist.save_calendar(run1, "ILG", "TPA", [date(2026, 10, 9), date(2026, 10, 12)], [cal(9, 79), cal(12, 89)], conn)
    run2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    persist.save_calendar(run2, "ILG", "TPA", [date(2026, 10, 9), date(2026, 10, 12)], [cal(9, 79)], conn)
    assert days_stored(conn) == [datetime(2026, 10, 9)]
    assert db.seen_in_run(run2, "ILG", "TPA", conn) == {date(2026, 10, 9): 79, date(2026, 10, 12): None}


def test_calendar_leaves_uncovered_months_alone(conn):
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 9), date(2026, 11, 3)], [cal(9, 79), cal(3, 99, month=11)], conn)
    run = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    # November failed to render: only October days are covered this time
    persist.save_calendar(run, "ILG", "TPA", [date(2026, 10, 9)], [cal(9, 75)], conn)
    assert days_stored(conn) == [datetime(2026, 10, 9), datetime(2026, 11, 3)]
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 9): 75}


def test_day_search_replaces_every_row_for_that_day(conn):
    db.upsert_flights([cal(10, 70), detail(10, 10, 80)], conn)
    run = db.start_run("detail", [("ILG", "TPA")], conn=conn)
    written = persist.save_day(run, "ILG", "TPA", date(2026, 10, 10), [detail(10, 11, 85)], conn)
    assert written == 1
    assert days_stored(conn) == [datetime(2026, 10, 10, 11)]
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 10): 85}


def test_sold_out_day_search_clears_the_day_and_records_none(conn):
    db.upsert_flights([detail(10, 10, 80)], conn)
    run = db.start_run("detail", [("ILG", "TPA")], conn=conn)
    assert persist.save_day(run, "ILG", "TPA", date(2026, 10, 10), [], conn) == 0
    assert days_stored(conn) == []
    assert db.seen_in_run(run, "ILG", "TPA", conn) == {date(2026, 10, 10): None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_persist.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'persist'`.

- [ ] **Step 3: Create `persist.py`**

```python
"""Write one search result to SQLite consistently (shared by every scraper).

A search answers "what fares exist for these route-days right now". Anything
we stored earlier for a covered day that the search no longer shows is gone
(sold out, retimed, cancelled) and is deleted, so packages, rules and watches
never offer a fare that cannot be booked. Days the search did not cover (a
month that failed to render) are left untouched and not recorded.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import date

import database
from models import Flight

log = logging.getLogger(__name__)


def save_calendar(run_id: int | None, origin: str, dest: str, covered_days: Iterable[date], flights: list[Flight],
                  conn: sqlite3.Connection | None = None) -> int:
    """Calendar result: one lowest fare per priced day; covered days without a fare are cleared."""
    covered = sorted(set(covered_days))
    priced = {f.departure_date for f in flights}
    written = database.upsert_flights(flights, conn)
    for day in covered:
        if day not in priced:
            removed = database.delete_day(origin, dest, day, conn=conn)
            if removed:
                log.info("%s→%s %s: no longer offered, removed %d stored fare(s)", origin, dest, day, removed)
    if run_id is not None and covered:
        database.record_seen(run_id, origin, dest, covered, flights, conn)
    database.drop_shadowed_placeholders(origin, dest, conn)
    return written


def save_day(run_id: int | None, origin: str, dest: str, day: date, flights: list[Flight],
             conn: sqlite3.Connection | None = None) -> int:
    """Detail result for one day: it replaces every stored row for that day."""
    database.delete_day(origin, dest, day, conn=conn)
    written = database.upsert_flights(flights, conn)
    if run_id is not None:
        database.record_seen(run_id, origin, dest, [day], flights, conn)
    return written
```

- [ ] **Step 4: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add persist.py tests/test_persist.py
git commit -m "feat: persist search results and remove fares that vanished"
```

---

### Task 8: Scraper uses `persist`; sold-out days are not retried

**Files:**
- Modify: `scraper.py:450-463` (`_retry`), `scraper.py:638-672` (`scrape_lowfare_route`), `scraper.py:337-370` (run loop)
- Test: `tests/test_scraper.py`

**Interfaces:**
- Consumes: `persist.save_calendar`, `persist.save_day` (Task 7); existing `DateUnavailable`, `ScrapeError`.
- Produces: `AveloScraper.scrape_lowfare_route(origin, dest, days) -> tuple[list[Flight], list[date]]` (flights, covered days); `_retry` re-raises `DateUnavailable` immediately.

- [ ] **Step 1: Write the failing test** — append to `tests/test_scraper.py`:

```python
import asyncio

from scraper import AveloScraper, DateUnavailable, ScrapeError


def test_retry_does_not_retry_a_sold_out_day():
    s = AveloScraper(screenshots=False, max_retries=2)
    calls = []

    async def sold_out():
        calls.append(1)
        raise DateUnavailable("Oct 12 not selectable")

    with pytest.raises(DateUnavailable):
        asyncio.run(s._retry(sold_out, "t"))
    assert len(calls) == 1


def test_retry_still_retries_other_scrape_errors(monkeypatch):
    s = AveloScraper(screenshots=False, max_retries=1)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ScrapeError("selector timeout")
        return "ok"

    async def no_sleep(_):
        return None

    async def no_page(*_a, **_k):
        return None

    monkeypatch.setattr("scraper.asyncio.sleep", no_sleep)
    monkeypatch.setattr(s, "_new_page", no_page)
    monkeypatch.setattr(AveloScraper, "page", property(lambda self: type("P", (), {"context": type("C", (), {"browser": None})()})()))
    assert asyncio.run(s._retry(flaky, "t")) == "ok" and len(calls) == 2
```

(`pytest` is already imported in that file; add it if not.)

- [ ] **Step 2: Run tests to verify the first fails**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_scraper.py -q`
Expected: `test_retry_does_not_retry_a_sold_out_day` FAILS (3 calls, because `DateUnavailable` is a `ScrapeError`).

- [ ] **Step 3: Implement `_retry` change** — in `scraper.py:452-455` add a clause before the generic one:

```python
        for attempt in range(self.max_retries + 1):
            try:
                return await fn(*args)
            except DateUnavailable:
                raise  # a sold-out day is an answer, not a failure: don't retry it
            except (PlaywrightTimeout, ScrapeError) as exc:
```

- [ ] **Step 4: Return covered days from the calendar** — in `scrape_lowfare_route` (`scraper.py:638-672`): change the return type to `tuple[list[Flight], list[date]]`, add `covered: list[date] = []` next to `flights: list[Flight] = []`, and after `flights.extend(in_month)` add:

```python
            covered.extend(d for d in days if (d.year, d.month) == (month.year, month.month))
```

Change the final line to `return flights, covered`.

- [ ] **Step 5: Use `persist` in the run loop** — add `import persist` to the imports. Replace the calendar block body (`scraper.py:339-350`):

```python
                        try:
                            flights, covered = await self._retry(self.scrape_lowfare_route, f"{origin}-{dest}-calendar", origin, dest, days)
                            flights = [self._tag(f) for f in flights if f.departure_date in wanted]
                            covered = [d for d in covered if d in wanted]
                            report.flights_written += persist.save_calendar(self.run_id, origin, dest, covered, flights)
                            report.succeeded += 1
                            await self._save_state()
                            log.info("%s→%s calendar: %d fares over %d days", origin, dest, len(flights), len(covered))
```

Replace the detail block body (`scraper.py:358-367`):

```python
                            try:
                                try:
                                    flights = await self._retry(self.scrape_day, f"{origin}-{dest}-{day}", origin, dest, day)
                                except DateUnavailable:
                                    log.info("%s→%s %s: not selectable (sold out)", origin, dest, day)
                                    flights = []
                                flights = [self._tag(f) for f in flights]
                                report.flights_written += persist.save_day(self.run_id, origin, dest, day, flights)
                                report.succeeded += 1
                                await self._save_state()
                                log.info("%s→%s %s: %d flight(s)", origin, dest, day, len(flights))
```

(`LOWFARE_PREFIX` stays defined; it is still used when building calendar flight ids.)

- [ ] **Step 6: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scraper.py tests/test_scraper.py
git commit -m "fix: scraper clears vanished fares and treats sold-out days as answers"
```

---

### Task 9: Run status; partial and failed runs are not baselines

**Files:**
- Modify: `database.py:141-145` (`MIGRATIONS` → per table), `database.py:180-192` (`_migrate`), `database.py:562-565` (`finish_run`), `database.py:608-611` (`previous_run_for_route`)
- Modify: `signals.py:48-103` (`detect_route`), `signals.py:106-122` (`run_detection`)
- Test: `tests/test_database.py` (update `test_runs_sightings_floors_and_watchlist`), `tests/test_signals.py`

**Interfaces:**
- Produces: `MIGRATIONS: dict[tuple[str, str], str]` keyed by (table, column); `scrape_runs.status TEXT` (`'ok' | 'partial' | 'failed'`, NULL for legacy rows); `database.finish_run(run_id, summary=None, conn=None, now=None, status: str = "ok")`; `previous_run_for_route` only returns finished runs whose status is `'ok'` or NULL; `signals.detect_route(..., allow_sold_out: bool = True)`; `signals.run_detection(run_id, conn=None, allow_sold_out: bool = True)`.

- [ ] **Step 1: Write the failing tests** — in `tests/test_database.py::test_runs_sightings_floors_and_watchlist`, insert `db.finish_run(run, {}, conn)` on the line before `run2 = db.start_run(...)` (a baseline must now be a finished run). Then append:

```python
import sqlite3


def test_old_database_gains_run_status_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE scrape_runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, mode TEXT NOT NULL, routes TEXT NOT NULL, promo_code TEXT, summary TEXT);
        INSERT INTO scrape_runs (started_at, finished_at, mode, routes) VALUES ('2026-09-15T16:00', '2026-09-15T16:05', 'calendar', '[]');
        INSERT INTO scrape_runs (started_at, finished_at, mode, routes) VALUES ('2026-09-15T17:00', NULL, 'calendar', '[]');
    """)
    raw.commit()
    raw.close()
    conn = db.get_connection(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scrape_runs)")}
    assert "status" in cols
    assert [r["run_id"] for r in conn.execute("SELECT run_id FROM scrape_runs")] == [1, 2]
    conn.close()


def test_only_finished_ok_runs_are_baselines(conn):
    day = [date(2026, 10, 10)]
    legacy = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(legacy, "ILG", "TPA", day, [], conn)
    conn.execute("UPDATE scrape_runs SET finished_at = '2026-09-15T16:05', status = NULL WHERE run_id = ?", (legacy,))
    unfinished = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(unfinished, "ILG", "TPA", day, [], conn)
    failed = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(failed, "ILG", "TPA", day, [], conn)
    db.finish_run(failed, {}, conn, status="failed")
    partial = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(partial, "ILG", "TPA", day, [], conn)
    db.finish_run(partial, {}, conn, status="partial")
    current = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    assert db.previous_run_for_route(current, "ILG", "TPA", conn) == legacy
    assert db.last_runs(1, conn)[0]["status"] is None   # current run not finished yet
```

Append to `tests/test_signals.py`:

```python
def test_partial_runs_raise_no_sold_out():
    from datetime import date

    from signals import detect_route

    prev = {date(2026, 10, 9): 79.0, date(2026, 10, 12): 89.0}
    cur = {date(2026, 10, 9): 79.0, date(2026, 10, 12): None}
    events, _ = detect_route("ILG", "TPA", prev, cur, None)
    assert [e.kind for e in events if e.kind == "sold_out"] == ["sold_out"]
    events, _ = detect_route("ILG", "TPA", prev, cur, None, allow_sold_out=False)
    assert not [e for e in events if e.kind == "sold_out"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_database.py tests/test_signals.py -q`
Expected: FAIL — no `status` column; `finish_run() got an unexpected keyword argument 'status'`; `detect_route() got an unexpected keyword argument 'allow_sold_out'`.

- [ ] **Step 3: Per-table migrations** — replace `MIGRATIONS` (`database.py:141-145`):

```python
MIGRATIONS: dict[tuple[str, str], str] = {
    ("avelo_flights", "base_price"): "ALTER TABLE avelo_flights ADD COLUMN base_price REAL",
    ("avelo_flights", "is_member_fare"): "ALTER TABLE avelo_flights ADD COLUMN is_member_fare INTEGER NOT NULL DEFAULT 0",
    ("avelo_flights", "promo_code"): "ALTER TABLE avelo_flights ADD COLUMN promo_code TEXT",
    ("scrape_runs", "status"): "ALTER TABLE scrape_runs ADD COLUMN status TEXT",  # ok | partial | failed
}
```

Replace the first three lines of `_migrate` (`database.py:181-184`):

```python
    columns: dict[str, set[str]] = {}
    for (table, column), ddl in MIGRATIONS.items():
        if table not in columns:
            columns[table] = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns[table]:
            conn.execute(ddl)
            columns[table].add(column)
```

(the price-history seeding and `conn.commit()` below stay.)

- [ ] **Step 4: Status in `finish_run` and the baseline query**

```python
def finish_run(run_id: int, summary: dict | None = None, conn: sqlite3.Connection | None = None,
               now: datetime | None = None, status: str = "ok") -> None:
    _run("UPDATE scrape_runs SET finished_at = ?, summary = ?, status = ? WHERE run_id = ?",
         (_now(now), json.dumps(summary or {}), status, run_id), conn)
```

```python
def previous_run_for_route(run_id: int, origin: str, destination: str, conn: sqlite3.Connection | None = None) -> int | None:
    """Latest earlier run that finished cleanly and saw this route (legacy runs have NULL status)."""
    rows = _fetch(
        """SELECT MAX(s.run_id) AS prev FROM route_day_seen s
           JOIN scrape_runs r ON r.run_id = s.run_id
           WHERE s.run_id < ? AND s.origin = ? AND s.destination = ?
             AND r.finished_at IS NOT NULL AND COALESCE(r.status, 'ok') = 'ok'""",
        (run_id, origin.upper(), destination.upper()), conn)
    return rows[0]["prev"] if rows and rows[0]["prev"] is not None else None
```

- [ ] **Step 5: `allow_sold_out` in `signals.py`** — add the parameter to `detect_route` (`allow_sold_out: bool = True` after `floor`) and guard the sold-out loop:

```python
        if allow_sold_out:
            for d in sorted(set(prev_priced) & {d for d, p in cur.items() if p is None}):
                events.append(Event("sold_out", origin, destination, d,
                                    f"{label}: {d:%a %b %d} no longer bookable (was ${prev_priced[d]:.0f})",
                                    {"last_price": prev_priced[d]}))
```

Change `run_detection`'s signature to `def run_detection(run_id: int, conn: sqlite3.Connection | None = None, allow_sold_out: bool = True) -> list[Event]:` and pass it on: `detect_route(origin, dest, prev, cur, floor, allow_sold_out=allow_sold_out)`.

- [ ] **Step 6: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add database.py signals.py tests/test_database.py tests/test_signals.py
git commit -m "fix: record run status; unfinished or partial runs never become signal baselines"
```

---

### Task 10: One scrape at a time, crash-safe runs, `--origins`

**Files:**
- Create: `runlock.py`
- Modify: `run_scraper.py` (imports, `parse_origins`, `build_command`, `build_parser`, `main`)
- Test: create `tests/test_runlock.py`; modify `tests/test_run_scraper.py`

**Interfaces:**
- Consumes: `config.HOME_AIRPORT_INFO`, `config.HOME_CODES`, `config.default_routes(destinations, origins)` (Task 1); `database.finish_run(..., status=)` and `signals.run_detection(..., allow_sold_out=)` (Task 9).
- Produces: `runlock.LOCK_PATH`; `runlock.acquire(path: Path | None = None) -> bool`; `runlock.holder_pid(path: Path | None = None) -> int | None`; `runlock.release(path: Path | None = None) -> None`; `run_scraper.parse_origins(spec: str | None) -> list[str]`; `build_command(..., origins: list[str] | None = None)` appends `--origins A,B` after `--mode`; `main()` returns `3` when another scrape holds the lock.

- [ ] **Step 1: Write the failing tests** — create `tests/test_runlock.py`:

```python
import os
import subprocess

import runlock


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_second_acquire_fails_while_holder_alive(tmp_path):
    lock = tmp_path / "scrape.lock"
    assert runlock.acquire(lock)
    assert runlock.holder_pid(lock) == os.getpid()
    assert not runlock.acquire(lock)
    runlock.release(lock)
    assert not lock.exists()


def test_stale_or_garbage_lock_is_taken_over(tmp_path):
    lock = tmp_path / "scrape.lock"
    lock.write_text(f"{dead_pid()} 2026-10-06T03:15:00")
    assert runlock.acquire(lock)
    runlock.release(lock)
    lock.write_text("garbage")
    assert runlock.acquire(lock)
    runlock.release(lock)


def test_release_leaves_someone_elses_lock(tmp_path):
    lock = tmp_path / "scrape.lock"
    lock.write_text("1 2026-10-06T03:15:00")   # pid 1 (launchd) is always alive
    runlock.release(lock)
    assert lock.exists()
    assert not runlock.acquire(lock)
```

Append to `tests/test_run_scraper.py`:

```python
import database
import runlock
import run_scraper


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "f.db")
    monkeypatch.setattr(runlock, "LOCK_PATH", tmp_path / "scrape.lock")
    return tmp_path


def test_build_command_passes_origins():
    cmd = build_command(["TPA"], date(2026, 10, 1), date(2026, 10, 7), origins=["ilg", "phl"], python="py")
    assert cmd[cmd.index("--origins") + 1] == "ILG,PHL"
    assert "--origins" not in build_command(["TPA"], date(2026, 10, 1), date(2026, 10, 7), python="py")


def test_parse_origins_validates():
    assert run_scraper.parse_origins("ilg,PHL") == ["ILG", "PHL"]
    assert run_scraper.parse_origins(None) == config.HOME_CODES
    with pytest.raises(SystemExit):
        run_scraper.parse_origins("ILG,TPA")


def test_mock_run_finishes_ok_and_releases_lock(isolated):
    assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "7", "--no-alerts", "--origins", "ILG"]) == 0
    assert database.last_runs(1)[0]["status"] == "ok"
    assert not (isolated / "scrape.lock").exists()


def test_crash_is_recorded_as_failed_run(isolated, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("browser exploded")
    monkeypatch.setattr(run_scraper, "generate_mock_flights", boom)
    assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "7", "--no-alerts"]) == 1
    run = database.last_runs(1)[0]
    assert run["status"] == "failed" and run["finished_at"] and "browser exploded" in run["summary"]["error"]
    assert not (isolated / "scrape.lock").exists()


def test_refuses_to_start_while_another_scrape_runs(isolated):
    assert runlock.acquire()
    try:
        assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "3", "--no-alerts"]) == 3
    finally:
        runlock.release()
    assert database.last_runs(1) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_runlock.py tests/test_run_scraper.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'runlock'`.

- [ ] **Step 3: Create `runlock.py`**

```python
"""One scrape at a time: a pid lock file shared by the dashboard, nightly job and CLI.

The file holds "<pid> <started iso>". A lock whose pid is no longer running (a
crashed scrape) or whose contents are unreadable is stale and is taken over.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import config

LOCK_PATH = config.PROJECT_ROOT / "data" / "scrape.lock"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def holder_pid(path: Path | None = None) -> int | None:
    try:
        return int((path or LOCK_PATH).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def acquire(path: Path | None = None) -> bool:
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = holder_pid(path)
            if pid is not None and _alive(pid):
                return False
            path.unlink(missing_ok=True)  # stale: holder died or file is garbage
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}")
        return True
    return False


def release(path: Path | None = None) -> None:
    path = path or LOCK_PATH
    if holder_pid(path) == os.getpid():
        path.unlink(missing_ok=True)
```

- [ ] **Step 4: `run_scraper.py` — origins, lock, crash safety**

Add `import runlock` to the imports. Add after `parse_destinations`:

```python
def parse_origins(spec: str | None) -> list[str]:
    if not spec:
        return list(config.HOME_CODES)
    codes = [c.strip().upper() for c in spec.split(",") if c.strip()]
    unknown = [c for c in codes if c not in config.HOME_AIRPORT_INFO]
    if unknown:
        raise SystemExit(f"Unknown home airport(s): {', '.join(unknown)}")
    return list(dict.fromkeys(codes))
```

In `build_command` add the parameter `origins: list[str] | None = None,` (after `promo`) and, right after the `"--mode", mode,` list entry is built (i.e. after the `cmd = [...]` literal), add:

```python
    if origins:
        cmd += ["--origins", ",".join(o.upper() for o in origins)]
```

In `build_parser` add after `--routes`:

```python
    p.add_argument("--origins", help="Comma list of home airports, e.g. ILG,PHL (default: AVELO_HOME_AIRPORTS)")
```

Replace `main` (`run_scraper.py:117-177`) with:

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    log = logging.getLogger("run_scraper")
    if not runlock.acquire():
        log.error("another scrape is already running (pid %s); not starting", runlock.holder_pid())
        return 3
    try:
        return _run(args, log)
    finally:
        runlock.release()


def _run(args: argparse.Namespace, log: logging.Logger) -> int:
    database.init_db()
    housekeeping = database.maintenance()
    log.info("maintenance: %s", ", ".join(f"{k}={v}" for k, v in housekeeping.items()))

    if args.routes:
        routes = parse_routes(args.routes)
    else:
        routes = config.default_routes(parse_destinations(args.destinations) or None, parse_origins(args.origins))
    dates = build_dates(args.start, args.end, args.days)
    log.info("routes=%d (%s) dates=%s..%s", len(routes), ",".join(f"{o}-{d}" for o, d in routes), dates[0], dates[-1])

    run_id = database.start_run("mock" if args.mock else args.mode, routes, args.promo)
    summary: dict = {}
    status, exit_code = "failed", 1
    try:
        if args.mock:
            flights = generate_mock_flights(dates[0], len(dates), routes, seed=args.mock_seed, discount=args.mock_sale)
            written = database.upsert_flights(flights)
            for origin, dest in routes:
                database.record_seen(run_id, origin, dest, scheduled_days(origin, dest, dates), flights)
            log.info("mock mode: wrote %d flights", written)
            summary = {"flights_written": written, "mock": True}
            status, exit_code = "ok", 0
        else:
            from scraper import AveloScraper  # imported lazily so --mock works without Playwright

            scraper = AveloScraper(headless=not args.headful and config.HEADLESS, mode=args.mode, promo_code=args.promo, run_id=run_id)
            started = datetime.now()
            report = asyncio.run(scraper.run(routes, dates))
            log.info(
                "done in %s: %d flights written, %d/%d searches ok, %d failed, %d routes skipped (not scheduled)",
                datetime.now() - started, report.flights_written, report.succeeded, report.attempted,
                report.failed, len(report.skipped_routes),
            )
            if report.skipped_routes:
                log.info("skipped (Avelo has no flights in range): %s", ", ".join(report.skipped_routes))
            for failure in report.failures:
                log.warning("failed: %s", failure)
            summary = {
                "flights_written": report.flights_written, "attempted": report.attempted, "succeeded": report.succeeded,
                "failed": report.failed, "skipped": report.skipped_routes, "failures": report.failures,
            }
            if report.failed == 0:
                status, exit_code = "ok", 0
            else:
                status, exit_code = ("partial" if report.succeeded else "failed"), 1

        if not args.no_alerts:
            signal_events = signals.run_detection(run_id, allow_sold_out=(status == "ok"))
            summary["signals"] = len(signal_events)
            summary["alerts"] = alerts.evaluate_and_deliver(run_id, signal_events)
    except Exception as exc:  # noqa: BLE001 - always close the run row
        log.exception("scrape crashed: %s", exc)
        summary["error"] = repr(exc)[:500]
        status, exit_code = "failed", 1
    finally:
        database.finish_run(run_id, summary, status=status)
    return exit_code
```

- [ ] **Step 5: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add runlock.py run_scraper.py tests/test_runlock.py tests/test_run_scraper.py
git commit -m "fix: one scrape at a time, crash-safe run records, --origins"
```

---

### Task 11: Dashboard wiring

**Files:**
- Modify: `app.py` — imports (~line 37), `load_selection_stats` (~71), `load_freshness` (~81), `run_search` cache key (~142), sidebar caption (~242), filters dict + `GetawayQuery(...)` (~296-315), `rule_panel` (~330-336), scrape caption + `build_command` call (~395-420), `system_map` hub (~473), package inspector watch button (~617-620), season-plan watch button (~783-787), `calendar_heatmap` (~878-879), weather + drives enrichment (~981-984), packages table `column_order` (~858-865)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `config.HOME_CODES`, `config.HOME_AIRPORTS`, `config.PRIMARY_HOME` (Task 1); `GetawayQuery.origins/allow_open_jaw` (Task 2); `database.home_costs`, `optimizer.enrich_home`, columns `"Home"`, `"Home-side $"` (Tasks 2-3); `alerts.watch_package`, `rule_payload(origins=)` (Task 6); `build_command(origins=)` (Task 10).
- Produces: nothing new for later tasks (Phase 4 adds the UI controls).

- [ ] **Step 1: Write the failing test** — append to `tests/test_app.py`:

```python
def test_packages_from_a_second_home_airport_show_up(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    with database.connection() as conn:
        conn.execute("""
            INSERT INTO avelo_flights (flight_id, origin, destination, departure_datetime, arrival_datetime, price,
                currency, duration_minutes, flight_number, base_price, is_member_fare, promo_code, timestamp_scraped)
            SELECT replace(flight_id, 'ILG', 'PHL'),
                   CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END,
                   CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END,
                   departure_datetime, arrival_datetime, price - 40, currency, duration_minutes, flight_number,
                   base_price, is_member_fare, promo_code, timestamp_scraped
            FROM avelo_flights WHERE origin = 'ILG' OR destination = 'ILG'
        """)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if "Home" in df.value.columns]
    assert tables and (tables[0]["Home"] == "PHL").any()
    phl = tables[0][tables[0]["Home"] == "PHL"]
    assert (phl["Home-side $"] > 0).all(), "PHL parking (default $17/day) must be counted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_app.py -q`
Expected: the new test FAILS on the `Home-side $` assertion — PHL packages already appear (Task 2 made `GetawayQuery` default to every home airport) but the dashboard never calls `enrich_home`, so parking is 0.

- [ ] **Step 3: Wire origins through `app.py`** (exact replacements)

1. Imports: add `enrich_home` to the `from optimizer import (...)` block.
2. `load_selection_stats`: `return database.selection_stats(config.HOME_CODES, list(destinations), start, end)`
3. `load_freshness`: `return pd.DataFrame(database.route_freshness(config.HOME_CODES, list(destinations)))`
4. `run_search` cache key tuple: add `tuple(query.origins),` and `query.allow_open_jaw,` after `query.max_pair_miles,`.
5. Sidebar caption: `st.sidebar.caption(f"Nonstop on Avelo from {' · '.join(config.HOME_CODES)}")`
6. `st.session_state.filters = {...}`: add `"origins": list(config.HOME_CODES), "allow_open_jaw": False,`.
7. `GetawayQuery(` call: add `origins=list(config.HOME_CODES),`.
8. `rule_panel`'s `alerts.rule_payload(...)` call: append `origins=f.get("origins"), allow_open_jaw=f.get("allow_open_jaw", False),`.
9. Scrape caption: `routes = len(destinations) * 2 * len(config.HOME_CODES)` and the caption text `f"Target: {routes} routes × {days} days · {'/'.join(config.HOME_CODES)} ⇄ {', '.join(destinations) or '—'}"`.
10. `build_command(` call: add `origins=list(config.HOME_CODES),`.
11. `system_map`: keep `hub = config.HOME_AIRPORTS[config.PRIMARY_HOME]` (from Task 1; Phase 4 draws every home).
12. Package inspector watch button body:

```python
        watch_id, created = alerts.watch_package(it)
        if created:
            st.success(f"Watching {alerts.package_label(it)} at ${it.total_price:,.0f}")
        else:
            st.info("You're already watching this package.")
```

13. "Watch all planned trips" body:

```python
        created = sum(alerts.watch_package(t)[1] for t in plan.trips)
        st.success(f"Watching {created} new trip(s); {len(plan.trips) - created} already watched.")
```

14. `calendar_heatmap`:

```python
    out = cal[cal.origin.isin(config.HOME_CODES) & cal.destination.isin(destinations)]
    back = cal[cal.destination.isin(config.HOME_CODES) & cal.origin.isin(destinations)]
```

15. After the `if pm.get("place"): enrich_drives(...)` block add:

```python
    enrich_home(itins, database.home_costs())
```

16. Packages table `column_order`: insert `"Home"` after `"Destination"`, and `"Home-side $"` after `"Door-to-door"`; in `column_config` add `"Home-side $": st.column_config.NumberColumn(format="$%d"),`.

Afterwards: `grep -n "config.ORIGIN\|add_watch(\"package\"" app.py` prints nothing.

- [ ] **Step 4: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (existing `test_watch_and_rule_buttons_create_watches` still finds "Watch this package").

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: dashboard searches every home airport and shows home-side costs"
```

---

### Task 12: Verify on real data and document

**Files:**
- Modify: `CLAUDE.md` (layout table, run commands, conventions), `README.md` (paths and commands), `.env.example` (`AVELO_HOME_AIRPORTS`)

- [ ] **Step 1: Migration dry run on a copy of the real database**

The real `data/flights.db` (Oct 6 2026: 817 fares, 15 scrape runs, runs #3 and #12 never finished) has **no watches, rules or bookings**, so migrating the copy alone proves nothing about legacy payloads. Part A migrates the copy and checks the run table. Part B seeds hand-built pre-upgrade watch, rule and booking rows into the **copy** (never the real file) and checks that the first evaluation stays silent. Both parts use the same copy; delete it at the end.

```bash
cd ~/avelo-finder
cp data/flights.db /tmp/flights-copy.db

# Part A: migration
AVELO_DB_PATH=/tmp/flights-copy.db AVELO_OFFLINE=1 .venv/bin/python - <<'EOF'
import database
conn = database.get_connection()
cols = {r["name"] for r in conn.execute("PRAGMA table_info(scrape_runs)")}
print("status column:", "status" in cols)
print("fares:", conn.execute("SELECT COUNT(*) FROM avelo_flights").fetchone()[0])
print("runs:", conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0])
print("unfinished runs:", [r[0] for r in conn.execute("SELECT run_id FROM scrape_runs WHERE finished_at IS NULL")])
print("legacy runs with NULL status:", conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE status IS NULL").fetchone()[0] if "status" in cols else "n/a (no status column yet)")
print("watches:", len(database.list_watches(conn=conn)), "bookings:", len(database.list_bookings(conn)))
EOF

# Part B: seed legacy rows into the copy, then evaluate once
AVELO_DB_PATH=/tmp/flights-copy.db AVELO_OFFLINE=1 .venv/bin/python - <<'EOF'
from datetime import date
import alerts, bookings, database

conn = database.get_connection()
row = conn.execute(
    "SELECT o.flight_id AS oid, o.departure_datetime AS odep, o.destination AS dst, o.price AS oprice, "
    "       o.base_price AS obase, i.flight_id AS iid, i.departure_datetime AS idep, i.price AS iprice, "
    "       i.base_price AS ibase "
    "FROM avelo_flights o JOIN avelo_flights i "
    "  ON i.origin = o.destination AND i.destination = o.origin AND i.departure_datetime > o.departure_datetime "
    "WHERE o.origin = 'ILG' AND o.departure_datetime >= ? "
    "ORDER BY o.departure_datetime, i.departure_datetime LIMIT 1", (date.today().isoformat(),)).fetchone()
if row is None:
    raise SystemExit("no future ILG round trip in the copy; scrape one first, or rely on the Task 6 tests and say so")
total = round(row["oprice"] + row["iprice"], 2)
# Exactly the fields the pre-upgrade code saved (no out_trip / in_trip / origins).
package = {"out_id": row["oid"], "out_dep": row["odep"], "in_id": row["iid"], "in_dep": row["idep"],
           "window_start": row["odep"][:10] + "T00:00", "window_end": row["idep"][:10] + "T23:59",
           "destination": row["dst"], "route_mix": f"{row['dst']} in & out"}
w = database.add_watch("package", "legacy package", package, conn)
database.update_watch_state(w, {"last_total": total}, conn)
database.add_booking("legacy booking", {**package, "out_price": row["oprice"], "out_base": row["obase"],
                     "in_price": row["iprice"], "in_base": row["ibase"], "member_fare": 0,
                     "destination_city": row["dst"], "out_date": row["odep"][:10], "in_date": row["idep"][:10]},
                     total, conn=conn)
rule = {"holiday_extend": False, "destinations": [row["dst"]], "start_weekday": 4, "start_time": "17:00",
        "end_weekday": 6, "end_time": "23:59", "min_nights": 1, "max_budget": 1000, "allow_mix": True,
        "max_pair_miles": 100.0, "span_days": 60}
r = database.add_watch("rule", "legacy rule", rule, conn)
old_key = "|".join(package[k] for k in ("out_id", "out_dep", "in_id", "in_dep", "window_start"))
database.update_watch_state(r, {"matched": [old_key], "match_count": 1}, conn)   # old-format state, no key_version
events = alerts.evaluate_packages(conn) + alerts.evaluate_rules(conn) + bookings.evaluate_bookings(conn)
print("seeded:", row["oid"], row["odep"], "->", row["iid"], row["idep"], "total", total)
print("events on first evaluation:", [(e.kind, e.message[:60]) for e in events])
print("rule key_version:", database.list_watches("rule", conn)[0]["state"].get("key_version"))
EOF

rm /tmp/flights-copy.db
```

Expected, Part A: `status column: True`; `fares:` the current count (817 on Oct 6 2026); `runs: 15` (or more); `unfinished runs: [3, 12]` (they must never become baselines); `legacy runs with NULL status:` equal to `runs` (migration does not backfill); `watches: 0 bookings: 0` (the real file has none, so this is expected, not a failure).

Expected, Part B: `events on first evaluation: []` and `rule key_version: 2`. The legacy rule state is adopted silently, the legacy package watch and booking still resolve against the calendar rows, and no `watch_unavailable`, `price_drop` or `booked_drop` event appears. For reference, the pre-upgrade code (before Task 6) emits a burst for the same seed (three `rule_match` events plus "N more new matches" on the Oct 6 2026 data); that burst is what Task 6 must prevent. Investigate anything else before continuing.

- [ ] **Step 2: Manual live check (owner present)**

```bash
.venv/bin/python run_scraper.py --destinations TPA --origins ILG --start <next Friday> --end <next Monday> --mode full --no-alerts
```

Expected: exit 0; log shows `calendar: N fares over M days` and per-day `flight(s)` lines; no route marked failed for a sold-out day. Then start the dashboard (`~/Desktop/Avelo Finder.command`) and confirm the packages table shows the **Home** column.

- [ ] **Step 3: Documentation**

`.env.example` — add:

```bash
# Home airports to search from (comma list; first is the "primary" one used for weather comparison)
AVELO_HOME_AIRPORTS=ILG,PHL,BWI
```

`CLAUDE.md` — in the layout table add rows for `trips.py` ("trip keys: airline|origin|dest|date|time-or-*; watches/rules/bookings store these"), `persist.py` ("writes one search result; deletes fares that vanished from covered days"), `runlock.py` ("data/scrape.lock, one scrape at a time; main() exits 3 when held"); replace mentions of "ILG hub"/`config.ORIGIN` with `config.HOME_CODES`/`PRIMARY_HOME`; add to Conventions: "Package/watch identity is the trip key, never the flight_id row" and "A run's `status` (ok/partial/failed) decides whether it can be a signal baseline". Update the test count in the Run section to the new total.

`README.md` — replace any `cd avelo-finder` with `cd ~/avelo-finder`, mention `~/Desktop/Avelo Finder.command`, and update the test count.

- [ ] **Step 4: Final full suite and commit**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add CLAUDE.md README.md .env.example
git commit -m "docs: phase 1 — home airports, trip identity, scrape reliability"
```
