# Phase 2 — Airline Data Model, Shared Browser Base, Frontier Scraper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store which airline every fare belongs to, share the browser plumbing between scrapers, and add a Frontier scraper (PHL/BWI) so `run_scraper.py --airlines F9` writes real Frontier nonstop fares — public and Discount Den — into SQLite.

**Architecture:** `avelo_flights`, `price_history` and `scrape_runs` gain an `airline` column (one scrape run per airline, so sightings and baselines stay per airline without rebuilding tables). `airlines.py` is a small registry (codes, membership programs, booking URLs, owner-price rule). `browser.py` holds a `BaseScraper` extracted from `AveloScraper`. `frontier.py` has pure parsers over Frontier's embedded JSON plus a thin `FrontierScraper` driver. A `route_catalog` table records who flies what, with a nonstop flag that gates Frontier calendar placeholders.

**Tech Stack:** Python 3.13, SQLite, Playwright (async), pytest (offline), BeautifulSoup (existing).

**Spec:** `docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md` (§3 Database + Airlines, §4 Scrapers; this is §8 Phase 2). Phase 1 ledger rulings that bind here: R16 (deletes must be airline-scoped before Frontier rows exist).

**Not in this phase (Phase 3/4):** optimizer/alerts/signals/hand-off across airlines, per-airline `route_floor`/`events`, `GetawayQuery.airlines`, dashboard controls. Until Phase 3 the dashboard and alerts keep reading **Avelo rows only** (Task 1 pins that).

## Global Constraints

- Project root `~/avelo-finder` (NOT under iCloud; never touch `~/Desktop/avelo-finder`). Tests: `cd ~/avelo-finder && AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`. Baseline before Task 1: **188 passed**. Work on branch `phase2-airlines-frontier` cut from `main`.
- Tests make no network calls; DB tests use `db.get_connection(":memory:")` or `tmp_path`; every `database.py` function takes an optional `conn`. Never open `data/flights.db` for writing in tests or probes (copy it under the project's git-ignored `.superpowers/probe/` if real data is needed). Never print `AVELO_NTFY_TOPIC`.
- Schema: new tables in `SCHEMA`; new columns in `MIGRATIONS` (keyed `(table, column)`). **An index that names a new column must be created in `_migrate`, never in `SCHEMA`** — `SCHEMA` runs before `_migrate`, so on an existing database the column does not exist yet.
- Airline codes: `XP` = Avelo, `F9` = Frontier. `Flight.airline` already exists (default `"XP"`).
- Fare semantics (spec §3): `price` = what the owner would pay (program fare if the owner is a member of that airline's program, else public); `base_price` = public fare; `member_price` = program fare. Memberships default `{"XP": True, "F9": False}` and live in the `settings` table.
- Frontier flight ids: detail `F9-<flightnumber>-ORG-DST` (e.g. `F9-2415-PHL-MCO`), calendar placeholder `F9CAL-ORG-DST` at 00:00 with no arrival time. Avelo ids unchanged.
- Nonstops only: a Frontier flight counts only when `stopCount == 0` and it has exactly one leg.
- One scrape run **per airline** (`scrape_runs.airline`); `previous_run_for_route` compares only runs of the same airline.
- Every delete of fares is scoped to one airline (ruling R16).
- Default airlines to scrape: env `AVELO_AIRLINES`, default `"XP"` in this phase (deviation from spec §4 "default all", so the dashboard button and nightly job do not start scraping data nothing displays yet; Phase 4 flips the default). `--airlines XP,F9` opts in.
- Frontier URLs (verified by the 2026-10-06 spike): home `https://www.flyfrontier.com/`; search `https://booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov%2012%2C%202026&ADT=1&mon=true&promo=` (add `&c=true` for the calendar view, which POSTs `/Flight/GetLowFareCalendarAvailability`); public route pages `https://flights.flyfrontier.com/en/flights-from-philadelphia` and `.../flights-from-baltimore`.
- Frontier calls West Palm Beach `DJT`; the app's code is `PBI`. Convert at the scraper boundary only.
- Commits end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; stage only touched files.

## Review Focus

1. **An existing database** (no `airline`/`member_price` columns, 811+ Avelo rows, 17 runs) must migrate in place: rows become `XP`, member fares get `member_price`, the airline index is created after the column, history seeding still works. → tests in Task 1.
2. **An Avelo run must never delete or shadow Frontier fares on the same route-day, and vice versa** (`save_calendar`, `save_day`, `clear_unscheduled`, `drop_shadowed_placeholders`). → tests in Task 2.
3. **A Frontier challenge / blocked page or a page without `FlightDataJson`** must not be read as "no flights": nothing is deleted, no sighting recorded, two in a row stop the Frontier run as `partial`. → tests in Tasks 6 and 7.
4. **A calendar response that is unsuccessful or has no priced day** is not an answer: nothing covered, nothing deleted. → tests in Tasks 6 and 7.
5. **Routes Frontier only serves with connections** (e.g. BWI→SFO) must never produce stored fares or placeholders. → tests in Tasks 6 and 7.
6. **The public route page being unreachable or changed** must leave the stored catalog intact (never wipe it). → test in Task 4.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `models.py` | modify | `Flight.member_price` |
| `database.py` | modify | airline/member_price columns + migration; airline-scoped deletes; `scrape_runs.airline`; `route_catalog`; memberships; `reprice` |
| `persist.py` | modify | `airline` parameter on every function |
| `airlines.py` | **create** | `Airline` registry, `owner_price`, site-code aliases, booking URLs |
| `config.py` | modify | `ACTIVE_AIRLINES`, `FRONTIER_AIRPORTS`, Frontier URLs, state-path helper |
| `browser.py` | **create** | `BaseScraper`, `ScrapeError`, `DateUnavailable`, `BlockedError`, `ScrapeReport`, `local_today`, `answerable_days` |
| `scraper.py` | modify | `AveloScraper(BaseScraper)`; member_price in parsers; fills catalog |
| `frontier.py` | **create** | pure parsers + `FrontierScraper` + catalog refresh |
| `optimizer.py` | modify | reads `XP` rows only until Phase 3 |
| `run_scraper.py` | modify | `--airlines`, per-airline runs, per-airline promo |
| `tests/fixtures/frontier/` | **create** | copies of `docs/superpowers/specs/frontier-spike/*.json|html` |
| `tests/test_airlines.py`, `tests/test_browser.py`, `tests/test_frontier.py` | **create** | new unit tests |

---

### Task 1: Airline and member-price columns

**Files:**
- Modify: `models.py` (new field), `database.py` (`SCHEMA`, `MIGRATIONS`, `UPSERT_SQL`, `HISTORY_SQL`, `_migrate`, `_to_row`, `_to_history_row`, `_from_row`, `query_flights`, `start_run`, `previous_run_for_route`), `optimizer.py` (`find_itineraries`)
- Test: `tests/test_database.py`, `tests/test_optimizer.py`

**Interfaces:**
- Produces: `Flight.member_price: float | None = None`; columns `avelo_flights.airline TEXT NOT NULL DEFAULT 'XP'`, `avelo_flights.member_price REAL`, `price_history.airline TEXT NOT NULL DEFAULT 'XP'`, `scrape_runs.airline TEXT NOT NULL DEFAULT 'XP'`; index `idx_flights_airline_route`; `database.query_flights(origins, destinations, start_date, end_date, max_price=None, conn=None, airlines: Iterable[str] | None = None)`; `database.start_run(mode, routes, promo_code=None, conn=None, now=None, airline: str = "XP")`; `previous_run_for_route` restricted to the same airline as `run_id`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def f9(day=10, hour=9, price=84.0, dd=83.0, number="2415", origin="PHL", dest="MCO"):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour), price=price,
                  base_price=price, member_price=dd, duration_minutes=166, flight_number=number, airline="F9",
                  flight_id=f"F9-{number}-{origin}-{dest}")


def test_airline_and_member_price_round_trip(conn):
    db.upsert_flights([f9(), flight(origin="PHL", dest="MCO", day=10, hour=12, price=70, base=99, number="XP9")], conn)
    rows = db.query_flights(["PHL"], ["MCO"], date(2026, 10, 10), date(2026, 10, 10), conn=conn)
    by_airline = {r.airline: r for r in rows}
    assert set(by_airline) == {"F9", "XP"}
    assert (by_airline["F9"].member_price, by_airline["F9"].base_price, by_airline["F9"].flight_number) == (83.0, 84.0, "2415")
    only_f9 = db.query_flights(["PHL"], ["MCO"], date(2026, 10, 10), date(2026, 10, 10), conn=conn, airlines=["f9"])
    assert [r.airline for r in only_f9] == ["F9"]
    assert db.find_trip("F9|PHL|MCO|2026-10-10|09:00", conn).price == 84.0
    assert db.find_trip("XP|PHL|MCO|2026-10-10|09:00", conn) is None
    hist = conn.execute("SELECT airline FROM price_history WHERE flight_id = 'F9-2415-PHL-MCO'").fetchone()
    assert hist["airline"] == "F9"


def test_pre_phase2_database_migrates_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE avelo_flights (flight_id TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            departure_datetime TEXT NOT NULL, arrival_datetime TEXT, price REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
            duration_minutes INTEGER, flight_number TEXT, base_price REAL, is_member_fare INTEGER NOT NULL DEFAULT 0,
            promo_code TEXT, timestamp_scraped TEXT NOT NULL, PRIMARY KEY (flight_id, departure_datetime));
        CREATE TABLE price_history (flight_id TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            departure_datetime TEXT NOT NULL, price REAL NOT NULL, base_price REAL, promo_code TEXT, scraped_at TEXT NOT NULL,
            PRIMARY KEY (flight_id, departure_datetime, scraped_at));
        CREATE TABLE scrape_runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
            mode TEXT NOT NULL, routes TEXT NOT NULL, promo_code TEXT, summary TEXT, status TEXT);
        INSERT INTO avelo_flights VALUES ('LOWFARE-ILG-TPA','ILG','TPA','2026-10-10T00:00',NULL,77,'USD',NULL,NULL,119,1,NULL,'2026-10-07T00:20:00');
        INSERT INTO avelo_flights VALUES ('AVELO-ILG-TPA','ILG','TPA','2026-10-12T10:30','2026-10-12T13:07',129,'USD',157,'532',129,0,NULL,'2026-10-07T00:55:00');
        INSERT INTO scrape_runs (started_at, finished_at, mode, routes, status) VALUES ('2026-10-07T00:53','2026-10-07T00:56','detail','[]','ok');
    """)
    raw.commit()
    raw.close()
    conn = db.get_connection(path)
    rows = {r["flight_id"]: dict(r) for r in conn.execute("SELECT * FROM avelo_flights")}
    assert rows["LOWFARE-ILG-TPA"]["airline"] == "XP" and rows["LOWFARE-ILG-TPA"]["member_price"] == 77
    assert rows["AVELO-ILG-TPA"]["member_price"] is None            # a public fare is not a member price
    assert conn.execute("SELECT airline FROM scrape_runs").fetchone()["airline"] == "XP"
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE airline = 'XP'").fetchone()[0] == 2   # seeded
    assert "idx_flights_airline_route" in {r["name"] for r in conn.execute("PRAGMA index_list(avelo_flights)")}
    conn.close()
    db.get_connection(path).close()                                  # second open is a no-op, not an error


def test_baselines_are_per_airline(conn):
    day = [date(2026, 10, 10)]
    xp = db.start_run("calendar", [("PHL", "MCO")], conn=conn)
    db.record_seen(xp, "PHL", "MCO", day, [], conn)
    db.finish_run(xp, {}, conn)
    f9_run = db.start_run("calendar", [("PHL", "MCO")], conn=conn, airline="F9")
    db.record_seen(f9_run, "PHL", "MCO", day, [], conn)
    assert db.previous_run_for_route(f9_run, "PHL", "MCO", conn) is None
    db.finish_run(f9_run, {}, conn)
    f9_next = db.start_run("calendar", [("PHL", "MCO")], conn=conn, airline="F9")
    assert db.previous_run_for_route(f9_next, "PHL", "MCO", conn) == f9_run
    assert db.last_runs(1, conn)[0]["airline"] == "F9"
```

Append to `tests/test_optimizer.py`:

```python
def test_packages_use_avelo_rows_only_until_phase_3():
    conn = db.get_connection(":memory:")
    out_f9 = Flight(origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 9), price=60,
                    duration_minutes=150, airline="F9", flight_id="F9-1-PHL-TPA")
    back_f9 = Flight(origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 16), price=60,
                     duration_minutes=150, airline="F9", flight_id="F9-2-TPA-PHL")
    db.upsert_flights([out_f9, back_f9, f("ILG", "TPA", 8, hour=8), f("TPA", "ILG", 11, hour=15)], conn)
    its = find_itineraries(q(origins=["ILG", "PHL"]), conn=conn)
    assert {i.outbound.airline for i in its} == {"XP"} and len(its) == 1
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_database.py tests/test_optimizer.py -q`
Expected: FAIL — `TypeError: Flight.__init__() got an unexpected keyword argument 'member_price'`.

- [ ] **Step 3: `models.py`** — add after the `base_price` field:

```python
    member_price: float | None = None  # the airline's program fare (Avelo PLUS, Discount Den), when shown
```

and extend the class docstring: `` `member_price` keeps the program fare so membership savings can be shown either way. ``

- [ ] **Step 4: `database.py` schema and migration**

In `SCHEMA`, add to `CREATE TABLE IF NOT EXISTS avelo_flights` (before `timestamp_scraped`): `member_price REAL,` and `airline TEXT NOT NULL DEFAULT 'XP',`; add `airline TEXT NOT NULL DEFAULT 'XP',` to `price_history` (before `scraped_at`) and to `scrape_runs` (before `summary`). Do **not** add an index on `airline` to `SCHEMA`.

Add to `MIGRATIONS`:

```python
    ("avelo_flights", "airline"): "ALTER TABLE avelo_flights ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
    ("avelo_flights", "member_price"): "ALTER TABLE avelo_flights ADD COLUMN member_price REAL",
    ("price_history", "airline"): "ALTER TABLE price_history ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
    ("scrape_runs", "airline"): "ALTER TABLE scrape_runs ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
```

In `_migrate`, after the column loop and before the history seeding, add; and rewrite the seeding statement with explicit columns:

```python
    # Indexes on migrated columns live here: SCHEMA runs before the columns exist on an old database.
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_flights_airline_route
                    ON avelo_flights (airline, origin, destination, departure_datetime)""")
    # Rows written before member_price existed: a member fare was stored as `price`.
    conn.execute("UPDATE avelo_flights SET member_price = price WHERE member_price IS NULL AND is_member_fare = 1")
    # Seed history from current fares so trends have a starting point.
    if conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0:
        conn.execute(
            """INSERT OR IGNORE INTO price_history
                   (flight_id, origin, destination, departure_datetime, price, base_price, promo_code, airline, scraped_at)
               SELECT flight_id, origin, destination, departure_datetime, price, base_price, promo_code, airline, timestamp_scraped
               FROM avelo_flights"""
        )
```

- [ ] **Step 5: `database.py` reads and writes**

`UPSERT_SQL`: add `member_price, airline` to the column list (15 `?`), and to the `DO UPDATE SET` clause add:

```sql
    member_price      = COALESCE(excluded.member_price, avelo_flights.member_price),
    airline           = excluded.airline,
```

`HISTORY_SQL`: columns `(flight_id, origin, destination, departure_datetime, price, base_price, promo_code, airline, scraped_at)` with 9 `?`.

`_to_row`: insert before the final `timestamp_scraped` element:

```python
        float(f.member_price) if f.member_price is not None else None,
        f.airline,
```

(keep the tuple order identical to the `UPSERT_SQL` column order). `_to_history_row`: insert `f.airline,` before the final scraped-at element. `_from_row`: add `member_price=row["member_price"],` and `airline=row["airline"],`.

`query_flights`: add the parameter `airlines: Iterable[str] | None = None` (last) and, before the `max_price` clause:

```python
    if airlines is not None:
        codes = [a.upper() for a in airlines]
        if not codes:
            return []
        sql += f" AND airline IN ({_placeholders(codes)})"
        params.extend(codes)
```

`start_run`: add `airline: str = "XP"` (last parameter); SQL `INSERT INTO scrape_runs (started_at, mode, routes, promo_code, airline) VALUES (?, ?, ?, ?, ?)` with `airline.upper()` appended to both parameter tuples.

`previous_run_for_route`: add to the `WHERE`: `AND r.airline = (SELECT airline FROM scrape_runs WHERE run_id = ?)` and pass `run_id` again as the last parameter.

- [ ] **Step 6: `optimizer.py`** — in `find_itineraries`, pass `airlines=["XP"]` to both `query_flights` calls with the comment `# Phase 3 makes the airline set part of GetawayQuery; until then packages are Avelo-only.`

- [ ] **Step 7: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (188 + 4 new).

- [ ] **Step 8: Commit**

```bash
git add models.py database.py optimizer.py tests/test_database.py tests/test_optimizer.py
git commit -m "feat: airline and member-price columns; one scrape run per airline"
```

---

### Task 2: Airline-scoped deletes (ruling R16)

**Files:**
- Modify: `database.py` (`delete_day`, `delete_days`, `drop_shadowed_placeholders`), `persist.py` (all three functions)
- Test: `tests/test_persist.py`, `tests/test_database.py`

**Interfaces:**
- Consumes: `avelo_flights.airline` (Task 1).
- Produces: `database.delete_day(origin, destination, day, flight_id_prefix=None, conn=None, airline: str = "XP")`; `database.delete_days(origin, destination, days, conn=None, airline: str = "XP")`; `database.drop_shadowed_placeholders(origin, destination, conn=None, airline: str = "XP")` (placeholder = row with `arrival_datetime IS NULL`, shadowed by a same-airline row with a known arrival on that day); `persist.save_calendar(run_id, origin, dest, covered_days, flights, conn=None, airline: str = "XP")`, `persist.save_day(run_id, origin, dest, day, flights, conn=None, airline: str = "XP")`, `persist.clear_unscheduled(origin, dest, days, conn=None, airline: str = "XP")`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_persist.py`:

```python
def f9_timed(day, hour, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day, hour), price=price,
                  duration_minutes=150, airline="F9", flight_number="77", flight_id="F9-77-ILG-TPA")


def f9_cal(day, price):
    return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day), price=price,
                  airline="F9", flight_id="F9CAL-ILG-TPA")


def airlines_on(conn, day):
    return sorted((f.airline, f.flight_id) for f in db.query_flights(["ILG"], ["TPA"], date(2026, 10, day), date(2026, 10, day), conn=conn))


def test_an_avelo_search_never_removes_frontier_fares(conn):
    db.upsert_flights([f9_timed(10, 9, 60), f9_cal(12, 55), detail(10, 10, 80), cal(12, 70)], conn)
    persist.save_day(None, "ILG", "TPA", date(2026, 10, 10), [], conn)                       # Avelo: sold out
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 12)], [], conn)                # Avelo: day lost its fare
    persist.clear_unscheduled("ILG", "TPA", [date(2026, 10, 10), date(2026, 10, 12)], conn)  # Avelo schedule dropped them
    assert airlines_on(conn, 10) == [("F9", "F9-77-ILG-TPA")]
    assert airlines_on(conn, 12) == [("F9", "F9CAL-ILG-TPA")]


def test_a_frontier_search_never_removes_avelo_fares(conn):
    db.upsert_flights([f9_timed(10, 9, 60), detail(10, 10, 80), cal(12, 70)], conn)
    persist.save_day(None, "ILG", "TPA", date(2026, 10, 10), [], conn, airline="F9")
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 12)], [], conn, airline="F9")
    assert airlines_on(conn, 10) == [("XP", "AVELO-ILG-TPA")]
    assert airlines_on(conn, 12) == [("XP", "LOWFARE-ILG-TPA")]


def test_placeholders_are_shadowed_only_by_their_own_airline(conn):
    db.upsert_flights([f9_cal(10, 55), detail(10, 10, 80)], conn)        # Avelo detail row, Frontier placeholder
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 10)], [f9_cal(10, 50)], conn, airline="F9")
    assert airlines_on(conn, 10) == [("F9", "F9CAL-ILG-TPA"), ("XP", "AVELO-ILG-TPA")]
    db.upsert_flights([f9_timed(10, 9, 60)], conn)
    persist.save_calendar(None, "ILG", "TPA", [date(2026, 10, 10)], [f9_cal(10, 50)], conn, airline="F9")
    assert airlines_on(conn, 10) == [("F9", "F9-77-ILG-TPA"), ("XP", "AVELO-ILG-TPA")]   # F9 placeholder shadowed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_persist.py -q`
Expected: FAIL — first test: Frontier rows deleted (`airlines_on(conn, 10) == []`); others: `unexpected keyword argument 'airline'`.

- [ ] **Step 3: `database.py`** — replace the three functions:

```python
def delete_day(
    origin: str,
    destination: str,
    day: date,
    flight_id_prefix: str | None = None,
    conn: sqlite3.Connection | None = None,
    airline: str = "XP",
) -> int:
    """Delete one airline's flights on one route-day, optionally only those whose
    flight_id starts with `flight_id_prefix`."""
    sql = """
        DELETE FROM avelo_flights
        WHERE airline = ? AND origin = ? AND destination = ?
          AND departure_datetime >= ? AND departure_datetime < ?
    """
    params: list = [airline.upper(), origin.upper(), destination.upper(), day.isoformat(),
                    (day + timedelta(days=1)).isoformat()]
    if flight_id_prefix:
        sql += " AND flight_id LIKE ?"
        params.append(f"{flight_id_prefix}%")
    return _run(sql, params, conn)


def delete_days(origin: str, destination: str, days: Iterable[date], conn: sqlite3.Connection | None = None,
                airline: str = "XP") -> int:
    """Delete one airline's flights on one route on any of `days` in one statement."""
    keys = sorted({d.isoformat() for d in days})
    if not keys:
        return 0
    sql = f"""
        DELETE FROM avelo_flights
        WHERE airline = ? AND origin = ? AND destination = ?
          AND substr(departure_datetime, 1, 10) IN ({_placeholders(keys)})
    """
    return _run(sql, [airline.upper(), origin.upper(), destination.upper(), *keys], conn)
```

```python
def drop_shadowed_placeholders(origin: str, destination: str, conn: sqlite3.Connection | None = None,
                               airline: str = "XP") -> int:
    """Remove an airline's calendar placeholders (no arrival time) on days where the
    same airline already has timed rows, so a calendar run after a detail run never
    duplicates a flight."""
    sql = """
        DELETE FROM avelo_flights
        WHERE airline = ? AND origin = ? AND destination = ? AND arrival_datetime IS NULL
          AND substr(departure_datetime, 1, 10) IN (
              SELECT substr(departure_datetime, 1, 10) FROM avelo_flights
              WHERE airline = ? AND origin = ? AND destination = ? AND arrival_datetime IS NOT NULL
          )
    """
    a, o, d = airline.upper(), origin.upper(), destination.upper()
    return _run(sql, (a, o, d, a, o, d), conn)
```

- [ ] **Step 4: `persist.py`** — add `airline: str = "XP"` as the last parameter of `save_calendar`, `save_day` and `clear_unscheduled`, and pass `airline=airline` to every `database.delete_day`, `database.delete_days` and `database.drop_shadowed_placeholders` call inside them. In `clear_unscheduled`'s docstring and log text replace "Avelo's schedule" with "the airline's schedule".

- [ ] **Step 5: Run the whole suite**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q`
Expected: all pass (the existing `test_delete_day_with_prefix_only_removes_placeholders` and shadow tests still pass: their rows default to `XP`, and their detail rows have a duration, hence an arrival time).

- [ ] **Step 6: Commit**

```bash
git add database.py persist.py tests/test_persist.py
git commit -m "fix: every fare delete is scoped to one airline"
```

---

### Task 3: Airline registry, memberships, owner price

**Files:**
- Create: `airlines.py`; Test: create `tests/test_airlines.py`
- Modify: `config.py` (airline settings, Frontier airports and URLs), `database.py` (memberships, `reprice`), `scraper.py` (`_price_fields` sets `member_price`), `run_scraper.py` (`parse_destinations` accepts Frontier airports)
- Test: `tests/test_config.py`, `tests/test_database.py`, `tests/test_scraper.py`

**Interfaces:**
- Produces:
  - `config.parse_airline_codes(raw: str) -> list[str]`, `config.ACTIVE_AIRLINES: list[str]` (env `AVELO_AIRLINES`, default `["XP"]`), `config.DEFAULT_MEMBERSHIPS = {"XP": True, "F9": False}`, `config.FRONTIER_AIRPORTS: dict[str, Airport]`, `config.FRONTIER_HOME_URL`, `config.FRONTIER_SELECT_URL`, `config.FRONTIER_ROUTES_URL` (format string with `{slug}`), `config.FRONTIER_ROUTE_PAGES = {"PHL": "philadelphia", "BWI": "baltimore"}`, `config.FRONTIER_SITE_CODES = {"PBI": "DJT"}`, `config.storage_state_path(airline: str) -> Path`; `config.ALL_AIRPORTS` includes `FRONTIER_AIRPORTS`.
  - `airlines.Airline(code, name, member_program, color)` frozen dataclass with `site_code(app_code) -> str`, `app_code(site_code) -> str`, `booking_url(origin, destination, day) -> str | None`; `airlines.AIRLINES: dict[str, Airline]`; `airlines.get(code) -> Airline` (raises `KeyError`); `airlines.owner_price(member: bool, member_price: float | None, base_price: float | None) -> tuple[float | None, bool]`; `airlines.frontier_search_url(origin, destination, day, calendar=False, promo="") -> str`.
  - `database.memberships(conn=None) -> dict[str, bool]`, `database.set_membership(airline, is_member, conn=None) -> None` (also reprices), `database.reprice(airline, is_member, conn=None) -> int`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_airlines.py`:

```python
from datetime import date

import pytest

import airlines


def test_registry_knows_avelo_and_frontier():
    assert set(airlines.AIRLINES) == {"XP", "F9"}
    assert airlines.get("f9").member_program == "Discount Den" and airlines.get("XP").name == "Avelo"
    with pytest.raises(KeyError):
        airlines.get("ZZ")


def test_owner_price_follows_membership():
    assert airlines.owner_price(True, 83.0, 84.0) == (83.0, True)
    assert airlines.owner_price(False, 83.0, 84.0) == (84.0, False)
    assert airlines.owner_price(False, 83.0, None) == (83.0, False)   # only the program fare is known: best estimate
    assert airlines.owner_price(True, None, 84.0) == (84.0, False)
    assert airlines.owner_price(True, None, None) == (None, False)


def test_frontier_urls_and_site_codes():
    url = airlines.frontier_search_url("PHL", "MCO", date(2026, 11, 12))
    assert url == "https://booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov%2012%2C%202026&ADT=1&mon=true&promo="
    cal = airlines.frontier_search_url("PHL", "PBI", date(2026, 11, 2), calendar=True, promo="save20")
    assert "d1=DJT" in cal and "&c=true" in cal and cal.endswith("promo=SAVE20") and "dd1=Nov%202%2C%202026" in cal
    f9 = airlines.get("F9")
    assert f9.site_code("PBI") == "DJT" and f9.app_code("DJT") == "PBI" and f9.app_code("MCO") == "MCO"
    assert f9.booking_url("PHL", "MCO", date(2026, 11, 12)) == url
    assert airlines.get("XP").booking_url("ILG", "TPA", date(2026, 11, 12)) is None   # Avelo ignores URL parameters
```

Append to `tests/test_config.py`:

```python
def test_airline_settings_and_frontier_airports():
    assert config.parse_airline_codes("xp, f9,XP,zz") == ["XP", "F9"]
    assert config.parse_airline_codes("") == ["XP"] and config.parse_airline_codes("zz") == ["XP"]
    assert config.DEFAULT_MEMBERSHIPS == {"XP": True, "F9": False}
    assert {"DEN", "LAS", "ATL", "MIA", "CUN", "ORD"} <= set(config.FRONTIER_AIRPORTS)
    assert not set(config.FRONTIER_AIRPORTS) & set(config.DESTINATIONS)       # Avelo's list is untouched
    assert config.airport_name("DEN") == "Denver" and config.distance_miles("PHL", "DEN") > 1400
    assert config.storage_state_path("f9").name == "browser_state_F9.json"
    assert set(config.FRONTIER_ROUTE_PAGES) == {"PHL", "BWI"}
```

Append to `tests/test_database.py`:

```python
def test_memberships_and_reprice(conn):
    assert db.memberships(conn) == {"XP": True, "F9": False}
    db.upsert_flights([f9(price=84.0, dd=83.0)], conn)
    db.set_membership("f9", True, conn)
    assert db.memberships(conn)["F9"] is True
    row = conn.execute("SELECT price, is_member_fare FROM avelo_flights WHERE airline = 'F9'").fetchone()
    assert (row["price"], row["is_member_fare"]) == (83.0, 1)
    db.set_membership("F9", False, conn)
    row = conn.execute("SELECT price, is_member_fare FROM avelo_flights WHERE airline = 'F9'").fetchone()
    assert (row["price"], row["is_member_fare"]) == (84.0, 0)
```

Append to `tests/test_scraper.py`:

```python
def test_avelo_parsers_keep_the_member_price():
    (f,) = parse_select_page((FIXTURES / "select_page_bundles.html").read_text(), "TPA", "ILG", date(2026, 10, 12))
    assert (f.price, f.member_price, f.base_price, f.airline) == (110.0, 110.0, 147.0, "XP")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_airlines.py tests/test_config.py tests/test_database.py tests/test_scraper.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'airlines'`, `AttributeError: ... 'parse_airline_codes'`.

- [ ] **Step 3: `config.py`** — after the `ALL_AIRPORTS` line add (and change `ALL_AIRPORTS` as shown):

```python
# Airports only Frontier flies from the home airports. Kept apart from DESTINATIONS (Avelo's list, which the
# dashboard shows) until the dashboard gains airline filters in Phase 4.
FRONTIER_AIRPORTS: dict[str, Airport] = {
    a.code: a
    for a in (
        Airport("MIA", "Miami", "Florida", 25.7959, -80.2870),
        Airport("JAX", "Jacksonville", "Florida", 30.4941, -81.6879),
        Airport("ATL", "Atlanta", "Carolinas & Georgia", 33.6407, -84.4277),
        Airport("CLT", "Charlotte", "Carolinas & Georgia", 35.2144, -80.9473),
        Airport("DFW", "Dallas/Fort Worth", "Texas & Gulf", 32.8998, -97.0403),
        Airport("IAH", "Houston", "Texas & Gulf", 29.9902, -95.3368),
        Airport("MSY", "New Orleans", "Texas & Gulf", 29.9934, -90.2580),
        Airport("DEN", "Denver", "West", 39.8561, -104.6737),
        Airport("LAS", "Las Vegas", "West", 36.0840, -115.1537),
        Airport("PHX", "Phoenix", "West", 33.4352, -112.0101),
        Airport("LAX", "Los Angeles", "West", 33.9416, -118.4085),
        Airport("SFO", "San Francisco", "West", 37.6213, -122.3790),
        Airport("DTW", "Detroit", "Midwest", 42.2162, -83.3554),
        Airport("IND", "Indianapolis", "Midwest", 39.7169, -86.2956),
        Airport("MSP", "Minneapolis", "Midwest", 44.8848, -93.2223),
        Airport("ORD", "Chicago", "Midwest", 41.9742, -87.9073),
        Airport("CUN", "Cancún", "Mexico & Central America", 21.0365, -86.8771),
        Airport("SAL", "San Salvador", "Mexico & Central America", 13.4409, -89.0557),
        Airport("PUJ", "Punta Cana", "Caribbean", 18.5674, -68.3634),
        Airport("SDQ", "Santo Domingo", "Caribbean", 18.4297, -69.6689),
        Airport("MBJ", "Montego Bay", "Caribbean", 18.5037, -77.9134),
    )
}

ALL_AIRPORTS: dict[str, Airport] = {**HOME_AIRPORT_INFO, **DESTINATIONS, **FRONTIER_AIRPORTS}

# --------------------------------------------------------------------------- #
# Airlines
# --------------------------------------------------------------------------- #

KNOWN_AIRLINES: tuple[str, ...] = ("XP", "F9")  # Avelo, Frontier
DEFAULT_MEMBERSHIPS: dict[str, bool] = {"XP": True, "F9": False}  # Avelo PLUS member; not in Discount Den


def parse_airline_codes(raw: str) -> list[str]:
    """Known airline codes from a comma list, in order, de-duplicated; Avelo if none."""
    codes = [c.strip().upper() for c in (raw or "").split(",") if c.strip()]
    known = [c for c in dict.fromkeys(codes) if c in KNOWN_AIRLINES]
    return known or ["XP"]


# Which airlines a scrape covers when --airlines is not given. Avelo only until the dashboard can show Frontier.
ACTIVE_AIRLINES: list[str] = parse_airline_codes(os.getenv("AVELO_AIRLINES", "XP"))

FRONTIER_HOME_URL = "https://www.flyfrontier.com/"
FRONTIER_SELECT_URL = "https://booking.flyfrontier.com/Flight/InternalSelect"
FRONTIER_CALENDAR_ENDPOINT = "GetLowFareCalendarAvailability"  # POSTed by the calendar view
FRONTIER_ROUTES_URL = "https://flights.flyfrontier.com/en/flights-from-{slug}"
FRONTIER_ROUTE_PAGES: dict[str, str] = {"PHL": "philadelphia", "BWI": "baltimore"}  # home airport → page slug
FRONTIER_SITE_CODES: dict[str, str] = {"PBI": "DJT"}  # app code → the code Frontier's site uses
FRONTIER_PROBE_DAYS: int = 3  # day searches used to learn whether a route has nonstops
```

Remove the old `ALL_AIRPORTS = {**HOME_AIRPORT_INFO, **DESTINATIONS}` line. Next to `STORAGE_STATE_PATH` add:

```python
def storage_state_path(airline: str) -> Path:
    """Per-airline cookie file, so one site's session never reaches the other."""
    return PROJECT_ROOT / "data" / f"browser_state_{airline.strip().upper()}.json"
```

Add to `.gitignore`: `data/browser_state_*.json`.

- [ ] **Step 4: Create `airlines.py`**

```python
"""The airlines the app knows: names, membership programs, booking links.

Which airline flies which route is data (the `route_catalog` table), not code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import config


def frontier_search_url(origin: str, destination: str, day: date, calendar: bool = False, promo: str = "") -> str:
    """Frontier's search page for one route and day (it reads these URL parameters)."""
    f9 = AIRLINES["F9"]
    when = quote(f"{day:%b} {day.day}, {day.year}")
    url = (f"{config.FRONTIER_SELECT_URL}?o1={f9.site_code(origin)}&d1={f9.site_code(destination)}"
           f"&dd1={when}&ADT=1&mon=true")
    if calendar:
        url += "&c=true"
    return f"{url}&promo={(promo or '').strip().upper()}"


@dataclass(frozen=True)
class Airline:
    code: str
    name: str
    member_program: str
    color: tuple[int, int, int]

    def site_code(self, app_code: str) -> str:
        """The airport code this airline's site uses for one of the app's codes."""
        code = app_code.strip().upper()
        return config.FRONTIER_SITE_CODES.get(code, code) if self.code == "F9" else code

    def app_code(self, site_code: str) -> str:
        code = site_code.strip().upper()
        if self.code == "F9":
            return {site: app for app, site in config.FRONTIER_SITE_CODES.items()}.get(code, code)
        return code

    def booking_url(self, origin: str, destination: str, day: date) -> str | None:
        """A link that opens this leg's search, or None when the site needs the assisted flow."""
        return frontier_search_url(origin, destination, day) if self.code == "F9" else None


AIRLINES: dict[str, Airline] = {
    "XP": Airline("XP", "Avelo", "Avelo PLUS", (88, 28, 135)),
    "F9": Airline("F9", "Frontier", "Discount Den", (0, 102, 67)),
}


def get(code: str) -> Airline:
    return AIRLINES[code.strip().upper()]


def owner_price(member: bool, member_price: float | None, base_price: float | None) -> tuple[float | None, bool]:
    """(the fare the owner would pay, whether it is the program fare)."""
    if member and member_price is not None:
        return member_price, True
    if base_price is not None:
        return base_price, False
    return member_price, False  # only a program fare is known (Frontier's calendar): the best estimate
```

- [ ] **Step 5: `database.py` memberships** — append to the Settings section:

```python
def memberships(conn: sqlite3.Connection | None = None) -> dict[str, bool]:
    """Which airline programs the owner belongs to (settings over config defaults)."""
    stored = get_setting("memberships", {}, conn) or {}
    return {code: bool(stored.get(code, default)) for code, default in config.DEFAULT_MEMBERSHIPS.items()}


def reprice(airline: str, is_member: bool, conn: sqlite3.Connection | None = None) -> int:
    """Recompute `price` (what the owner pays) for one airline from the two stored fares."""
    if is_member:
        sql = """UPDATE avelo_flights SET price = member_price, is_member_fare = 1
                 WHERE airline = ? AND member_price IS NOT NULL"""
    else:
        sql = """UPDATE avelo_flights SET price = base_price, is_member_fare = 0
                 WHERE airline = ? AND base_price IS NOT NULL"""
    return _run(sql, (airline.upper(),), conn)


def set_membership(airline: str, is_member: bool, conn: sqlite3.Connection | None = None) -> None:
    stored = get_setting("memberships", {}, conn) or {}
    stored[airline.upper()] = bool(is_member)
    set_setting("memberships", stored, conn)
    reprice(airline, is_member, conn)
```

- [ ] **Step 6: `scraper.py` and `run_scraper.py`**

`scraper._price_fields`: return `member_price` too:

```python
    if member is not None:
        return {"price": member, "base_price": public, "member_price": member, "is_member_fare": True}
    if public is not None:
        return {"price": public, "base_price": public, "member_price": None, "is_member_fare": False}
    return None
```

`run_scraper.parse_destinations`: accept Frontier airports:

```python
    unknown = [c for c in codes if c not in config.DESTINATIONS and c not in config.FRONTIER_AIRPORTS]
```

- [ ] **Step 7: Run the whole suite** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q` → all pass.

- [ ] **Step 8: Commit**

```bash
git add airlines.py config.py database.py scraper.py run_scraper.py .gitignore tests/test_airlines.py tests/test_config.py tests/test_database.py tests/test_scraper.py
git commit -m "feat: airline registry, memberships and owner-price rule"
```

---

### Task 4: Route catalog

**Files:**
- Modify: `database.py` (`SCHEMA` + new functions after the Settings section), `scraper.py` (`AveloScraper.run` records the schedule's routes)
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: table `route_catalog(airline, origin, destination, first_seen, last_seen, source, nonstop INTEGER, nonstop_checked TEXT, PRIMARY KEY (airline, origin, destination))`; `database.upsert_catalog(airline: str, pairs: Iterable[tuple[str, str]], source: str, conn=None, now=None) -> int`; `database.catalog_routes(airline: str, origins: Iterable[str] | None = None, destinations: Iterable[str] | None = None, conn=None, max_age_days: int = 45, now=None) -> list[tuple[str, str]]` (directional pairs `(origin, destination)` where either end filters apply to the home/away ends — see code); `database.catalog_checked(airline: str, origin: str, conn=None) -> datetime | None` (latest `last_seen` among routes from `origin`); `database.route_nonstop(airline, origin, destination, conn=None, recheck_days: int = 30, now=None) -> bool | None`; `database.set_route_nonstop(airline, origin, destination, nonstop: bool, conn=None, now=None) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_route_catalog_round_trip_and_filters(conn):
    t0 = datetime(2026, 10, 7, 9)
    assert db.upsert_catalog("f9", [("PHL", "MCO"), ("mco", "phl"), ("BWI", "DEN"), ("DEN", "BWI")], "route-page", conn, now=t0) == 4
    db.upsert_catalog("XP", [("ILG", "TPA"), ("TPA", "ILG")], "routes/all", conn, now=t0)
    assert db.catalog_routes("F9", conn=conn, now=t0) == [("BWI", "DEN"), ("DEN", "BWI"), ("MCO", "PHL"), ("PHL", "MCO")]
    assert db.catalog_routes("F9", origins=["PHL"], conn=conn, now=t0) == [("MCO", "PHL"), ("PHL", "MCO")]
    assert db.catalog_routes("F9", origins=["PHL", "BWI"], destinations=["den"], conn=conn, now=t0) == [("BWI", "DEN"), ("DEN", "BWI")]
    assert db.catalog_routes("XP", conn=conn, now=t0) == [("ILG", "TPA"), ("TPA", "ILG")]
    assert db.catalog_checked("F9", "PHL", conn) == t0 and db.catalog_checked("F9", "ILG", conn) is None


def test_catalog_keeps_first_seen_and_ages_out(conn):
    t0, t1 = datetime(2026, 8, 1, 9), datetime(2026, 10, 7, 9)
    db.upsert_catalog("F9", [("PHL", "MCO"), ("PHL", "SJU")], "route-page", conn, now=t0)
    db.upsert_catalog("F9", [("PHL", "MCO")], "route-page", conn, now=t1)          # SJU not listed any more
    assert db.catalog_routes("F9", conn=conn, now=t1) == [("PHL", "MCO")]           # 67-day-old row aged out
    row = conn.execute("SELECT first_seen, last_seen FROM route_catalog WHERE destination = 'MCO'").fetchone()
    assert row["first_seen"].startswith("2026-08-01") and row["last_seen"].startswith("2026-10-07")
    assert db.upsert_catalog("F9", [], "route-page", conn, now=t1) == 0             # an empty fetch changes nothing
    assert conn.execute("SELECT COUNT(*) FROM route_catalog").fetchone()[0] == 2


def test_nonstop_flag_is_tristate_and_rechecked(conn):
    t0 = datetime(2026, 10, 7, 9)
    db.upsert_catalog("F9", [("BWI", "SFO"), ("PHL", "MCO")], "route-page", conn, now=t0)
    assert db.route_nonstop("F9", "PHL", "MCO", conn, now=t0) is None
    db.set_route_nonstop("F9", "PHL", "MCO", True, conn, now=t0)
    db.set_route_nonstop("F9", "BWI", "SFO", False, conn, now=t0)
    assert db.route_nonstop("F9", "PHL", "MCO", conn, now=t0) is True
    assert db.route_nonstop("F9", "BWI", "SFO", conn, now=t0) is False
    assert db.route_nonstop("F9", "BWI", "SFO", conn, now=datetime(2026, 11, 20)) is None   # "no" is re-probed after 30 days
    assert db.route_nonstop("F9", "PHL", "MCO", conn, now=datetime(2027, 3, 1)) is True     # "yes" does not expire
    db.set_route_nonstop("F9", "PHL", "DEN", True, conn, now=t0)                             # unknown route: created
    assert db.route_nonstop("F9", "PHL", "DEN", conn, now=t0) is True
```

- [ ] **Step 2: Run tests to verify they fail** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_database.py -q` → FAIL `no attribute 'upsert_catalog'`.

- [ ] **Step 3: `database.py`** — append to `SCHEMA`:

```sql
-- Who flies what. nonstop: NULL = not yet known, 1 = a nonstop was seen, 0 = probed, none found.
CREATE TABLE IF NOT EXISTS route_catalog (
    airline         TEXT NOT NULL,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    source          TEXT NOT NULL,
    nonstop         INTEGER,
    nonstop_checked TEXT,
    PRIMARY KEY (airline, origin, destination)
);
```

Append a new section at the end of `database.py`:

```python
# --------------------------------------------------------------------------- #
# Route catalog (which airline flies which directional route)
# --------------------------------------------------------------------------- #


def upsert_catalog(airline: str, pairs: Iterable[tuple[str, str]], source: str,
                   conn: sqlite3.Connection | None = None, now: datetime | None = None) -> int:
    """Record that `airline` lists these directional routes now. Never removes rows:
    a route that stops being listed simply ages out of `catalog_routes`."""
    stamp = _now(now)
    rows = sorted({(airline.upper(), o.strip().upper(), d.strip().upper(), stamp, stamp, source) for o, d in pairs})
    if not rows:
        return 0
    _run("""INSERT INTO route_catalog (airline, origin, destination, first_seen, last_seen, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (airline, origin, destination) DO UPDATE SET last_seen = excluded.last_seen, source = excluded.source""",
         rows, conn, many=True)
    return len(rows)


def catalog_routes(airline: str, origins: Iterable[str] | None = None, destinations: Iterable[str] | None = None,
                   conn: sqlite3.Connection | None = None, max_age_days: int = 45,
                   now: datetime | None = None) -> list[tuple[str, str]]:
    """Directional routes `airline` listed within `max_age_days`. `origins` are home airports and
    `destinations` the far ends: a route is kept when one end is in each (either direction)."""
    cutoff = ((now or datetime.now()) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    rows = _fetch("""SELECT origin, destination FROM route_catalog
                     WHERE airline = ? AND last_seen >= ? ORDER BY origin, destination""", (airline.upper(), cutoff), conn)
    homes = {o.upper() for o in origins} if origins is not None else None
    away = {d.upper() for d in destinations} if destinations is not None else None
    out = []
    for r in rows:
        o, d = r["origin"], r["destination"]
        if homes is not None and not (o in homes or d in homes):
            continue
        if away is not None and not (o in away or d in away):
            continue
        out.append((o, d))
    return out


def catalog_checked(airline: str, origin: str, conn: sqlite3.Connection | None = None) -> datetime | None:
    """When this airline's routes from `origin` were last confirmed (None = never)."""
    rows = _fetch("SELECT MAX(last_seen) AS seen FROM route_catalog WHERE airline = ? AND origin = ?",
                  (airline.upper(), origin.upper()), conn)
    return datetime.fromisoformat(rows[0]["seen"]) if rows and rows[0]["seen"] else None


def route_nonstop(airline: str, origin: str, destination: str, conn: sqlite3.Connection | None = None,
                  recheck_days: int = 30, now: datetime | None = None) -> bool | None:
    """True = a nonstop was seen; False = probed recently, none found; None = unknown (or a stale "no")."""
    rows = _fetch("SELECT nonstop, nonstop_checked FROM route_catalog WHERE airline = ? AND origin = ? AND destination = ?",
                  (airline.upper(), origin.upper(), destination.upper()), conn)
    if not rows or rows[0]["nonstop"] is None:
        return None
    if rows[0]["nonstop"]:
        return True
    checked = datetime.fromisoformat(rows[0]["nonstop_checked"])
    return False if (now or datetime.now()) - checked < timedelta(days=recheck_days) else None


def set_route_nonstop(airline: str, origin: str, destination: str, nonstop: bool,
                      conn: sqlite3.Connection | None = None, now: datetime | None = None) -> None:
    stamp = _now(now)
    _run("""INSERT INTO route_catalog (airline, origin, destination, first_seen, last_seen, source, nonstop, nonstop_checked)
            VALUES (?, ?, ?, ?, ?, 'search', ?, ?)
            ON CONFLICT (airline, origin, destination) DO UPDATE SET nonstop = excluded.nonstop, nonstop_checked = excluded.nonstop_checked""",
         (airline.upper(), origin.upper(), destination.upper(), stamp, stamp, 1 if nonstop else 0, stamp), conn)
```

- [ ] **Step 4: Avelo fills the catalog** — in `scraper.py` add `import database` to the imports, and in `AveloScraper.run`, right after `schedule = await self._retry(self.fetch_schedule, "schedule")`, add:

```python
                database.upsert_catalog("XP", schedule.keys(), "routes/all")
```

Existing tests that drive `AveloScraper.run` with a fake schedule monkeypatch `database.DB_PATH` or pass through `persist`; if one fails because it has no database, give it a `tmp_path` database with `monkeypatch.setattr(database, "DB_PATH", tmp_path / "f.db")`.

- [ ] **Step 5: Run the whole suite** → all pass.

- [ ] **Step 6: Commit**

```bash
git add database.py scraper.py tests/test_database.py tests/test_scraper.py
git commit -m "feat: route catalog with a nonstop flag; Avelo fills it from its schedule"
```

---

### Task 5: Shared browser base (`browser.py`)

**Files:**
- Create: `browser.py`; Test: create `tests/test_browser.py`
- Modify: `scraper.py` (class and module members move out; `AveloScraper(BaseScraper)`), `config.py` (remove nothing; `STORAGE_STATE_PATH` stays as the legacy path)

**Interfaces:**
- Produces in `browser.py`: `ScrapeError`, `DateUnavailable(ScrapeError)`, `BlockedError(ScrapeError)`, `ScrapeReport` (existing fields plus `blocked: bool = False`), `local_today() -> date`, `answerable_days(days, wanted, today) -> list[date]`, `BLOCKED_HOSTS` (the tracker regex), and `class BaseScraper` with class attribute `airline: str = "XP"`, `__init__(headless=None, mode="calendar", delay_range=config.DELAY_RANGE, max_retries=config.MAX_RETRIES, timeout_ms=config.PAGE_TIMEOUT_MS, screenshots=True, promo_code=None, run_id=None)`, `state_path -> Path`, `async _new_page(browser) -> Page`, `async _save_state()`, `_tag(flight)`, `async _human_pause()`, `async _wander()`, `page` property, `async _polite_delay(short=False)`, `async _retry(fn, tag, *args)`, `async _snapshot(tag, attempt)`.
- `scraper.py` keeps importing these names (`from browser import ...`) so `scraper.ScrapeError`, `scraper.DateUnavailable`, `scraper.ScrapeReport`, `scraper.local_today`, `scraper.answerable_days` remain valid for existing callers and tests.

- [ ] **Step 1: Write the failing tests** — first make every test blind to the owner's real cookie files. Add to `tests/conftest.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def _no_real_browser_state(tmp_path, monkeypatch):
    """Scrapers adopt and write cookie files under data/; tests must never touch the owner's."""
    import config

    monkeypatch.setattr(config, "STORAGE_STATE_PATH", tmp_path / "legacy_browser_state.json")
    monkeypatch.setattr(config, "storage_state_path", lambda airline: tmp_path / f"browser_state_{airline.strip().upper()}.json")
```

Then create `tests/test_browser.py` (its tests re-point both names themselves where they check the real path logic):

```python
import asyncio
import json

import pytest

import browser
import config
from browser import BaseScraper, BlockedError, DateUnavailable, ScrapeError


class FakeContext:
    def __init__(self, state):
        self.state = state

    async def storage_state(self):
        return json.loads(json.dumps(self.state))


REAL_STATE_PATH = config.storage_state_path  # captured at import, before the autouse fixture replaces it


def real_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "storage_state_path", REAL_STATE_PATH)
    monkeypatch.setattr(config, "STORAGE_STATE_PATH", tmp_path / "data" / "browser_state.json")


def test_state_file_is_per_airline_and_written_atomically(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)

    class F9(BaseScraper):
        airline = "F9"

    s = F9(screenshots=False)
    assert s.state_path == tmp_path / "data" / "browser_state_F9.json"
    s._context = FakeContext({"cookies": [{"name": "a", "value": "1"}], "origins": [{"origin": "x"}]})
    asyncio.run(s._save_state())
    saved = json.loads(s.state_path.read_text())
    assert saved == {"cookies": [{"name": "a", "value": "1"}], "origins": []}      # cookies only
    assert not list(s.state_path.parent.glob("*.tmp"))                              # temp file renamed away


def test_corrupt_or_missing_state_file_is_ignored(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)
    s = BaseScraper(screenshots=False)
    assert s._usable_state() is None                                                # missing
    s.state_path.parent.mkdir(parents=True)
    s.state_path.write_text('{"cookies": [')                                        # truncated by a crash
    assert s._usable_state() is None
    s.state_path.write_text('{"cookies": [], "origins": []}')
    assert s._usable_state() == str(s.state_path)


def test_legacy_avelo_state_file_is_adopted(tmp_path, monkeypatch):
    real_paths(tmp_path, monkeypatch)
    legacy = config.STORAGE_STATE_PATH
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"cookies": [{"name": "old"}], "origins": []}')
    s = BaseScraper(screenshots=False)                                               # airline XP
    assert s._usable_state() == str(s.state_path) and not legacy.exists()
    assert json.loads(s.state_path.read_text())["cookies"] == [{"name": "old"}]


def test_retry_never_retries_answers_or_blocks():
    s = BaseScraper(screenshots=False, max_retries=2)
    for exc in (DateUnavailable("sold out"), BlockedError("challenge")):
        calls = []

        async def fn(exc=exc, calls=calls):
            calls.append(1)
            raise exc

        with pytest.raises(type(exc)):
            asyncio.run(s._retry(fn, "t"))
        assert len(calls) == 1
    assert issubclass(BlockedError, ScrapeError) and issubclass(DateUnavailable, ScrapeError)


def test_snapshot_saves_html_beside_the_screenshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCREENSHOT_DIR", tmp_path)

    class Page:
        async def screenshot(self, path, full_page):
            open(path, "wb").write(b"png")

        async def content(self):
            return "<html>challenge</html>"

    s = BaseScraper()
    s._page = Page()
    asyncio.run(s._snapshot("F9-PHL-MCO", 0))
    assert [p.suffix for p in sorted(tmp_path.iterdir())] == [".html", ".png"]
    assert "challenge" in next(tmp_path.glob("*.html")).read_text()


def test_report_knows_when_a_run_was_blocked():
    assert browser.ScrapeReport().blocked is False
```

- [ ] **Step 2: Run tests to verify they fail** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_browser.py -q` → FAIL `ModuleNotFoundError: No module named 'browser'`.

- [ ] **Step 3: Create `browser.py`** — move these out of `scraper.py` **unchanged** unless noted: `ScrapeError`, `DateUnavailable`, `ScrapeReport`, `local_today`, `answerable_days`, the tracker regex (rename `_BLOCKED_HOSTS` → `BLOCKED_HOSTS`), and from `AveloScraper`: `__init__`, `_new_page`, `_save_state`, `_tag`, `_human_pause`, `_wander`, `page`, `_polite_delay`, `_retry`, `_snapshot`. The module starts with:

```python
"""Browser plumbing shared by every airline scraper.

One fingerprint per run, cookies kept per airline, stealth init script,
trackers blocked, human pauses, retries with a snapshot per failure. Airline
scrapers subclass `BaseScraper` and add only their site's pages and parsers.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

import config
from models import Flight

log = logging.getLogger(__name__)

Mode = Literal["calendar", "detail", "full"]
```

Changes while moving:

1. Add after `DateUnavailable`:

```python
class BlockedError(ScrapeError):
    """The site answered with a bot challenge instead of the page. Not retried within a run."""
```

2. `ScrapeReport`: add the field `blocked: bool = False  # the site challenged us; the rest of this airline was skipped`.

3. `class BaseScraper:` gets the class attribute `airline: str = "XP"` and these members replace the moved `_new_page` state handling and `_save_state`:

```python
    @property
    def state_path(self) -> Path:
        return config.storage_state_path(self.airline)

    def _usable_state(self) -> str | None:
        """Path of this airline's saved cookies, or None when there are none or the file is damaged."""
        path = self.state_path
        legacy = config.STORAGE_STATE_PATH  # pre-phase-2 Avelo cookies: adopt them once
        if self.airline == "XP" and not path.exists() and legacy.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy, path)
        if not path.exists():
            return None
        try:
            json.loads(path.read_text())
        except (OSError, ValueError):
            log.warning("ignoring unreadable browser state %s", path.name)
            return None
        return str(path)

    async def _save_state(self) -> None:
        """Persist cookies so the next run looks like a returning visitor."""
        if self._context is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            state = await self._context.storage_state()
            # Cookies only. Avelo's app caches its route list in localStorage; replaying
            # that would stop it re-fetching the schedule feed we capture on load.
            state["origins"] = []
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)  # atomic: a crash never leaves half a file
        except Exception as exc:  # noqa: BLE001 - never fail a scrape over this
            log.debug("could not save browser state: %s", exc)
```

In `_new_page`, replace the two lines that compute and use `state` with `state = self._usable_state()` and `storage_state=state,`.

4. `_retry`: the first `except` becomes `except (DateUnavailable, BlockedError): raise  # an answer or a block: retrying cannot help`.

5. `_snapshot`: after the screenshot line add:

```python
            path.with_suffix(".html").write_text(await self._page.content())  # the page itself, for a new fixture
```

- [ ] **Step 4: `scraper.py`** — delete the moved members; add:

```python
from browser import (BLOCKED_HOSTS, BaseScraper, BlockedError, DateUnavailable, Mode, ScrapeError,  # noqa: F401
                     ScrapeReport, answerable_days, local_today)
```

`class AveloScraper(BaseScraper):` with `airline = "XP"` and no `__init__` of its own (the base's is identical). Keep everything Avelo-specific (`run`, `fetch_schedule`, `_submit_search`, `_pick_location`, `_pick_date`, `_apply_promo`, `_close_popovers`, `scrape_lowfare_route`, `scrape_day`, parsers, `plan_targets`, `unscheduled_days`). Replace any remaining `_BLOCKED_HOSTS` use with `BLOCKED_HOSTS`. `handoff.py` and tests import from `scraper` and keep working through the re-exports; run `grep -rn "_BLOCKED_HOSTS\|STORAGE_STATE_PATH" --include='*.py' . | grep -v .venv` and fix anything left besides `config.py` and `browser.py`.

- [ ] **Step 5: Run the whole suite** → all pass (existing scraper tests patch `scraper.local_today` and `scraper.asyncio.sleep`; both still take effect: `run` looks the name up in `scraper`, and `asyncio.sleep` is the same module object everywhere).

- [ ] **Step 6: Commit**

```bash
git add browser.py scraper.py tests/test_browser.py
git commit -m "refactor: shared BaseScraper; per-airline atomic cookie file; HTML in failure snapshots"
```

---

### Task 6: Frontier parsers (pure)

**Files:**
- Create: `frontier.py` (parsers only in this task), `tests/fixtures/frontier/` (copy the six files from `docs/superpowers/specs/frontier-spike/`: `select_PHL_MCO_2026-11-12.json`, `select_MCO_PHL_2026-11-15.json`, `select_BWI_TPA_2026-11-13.json`, `lowfare_calendar_PHL_MCO_2026-11.json`, `routes_from_PHL.html`, `routes_from_BWI.html`)
- Test: create `tests/test_frontier.py`

**Interfaces:**
- Consumes: `airlines.owner_price`, `airlines.get("F9").app_code/site_code` (Task 3); `browser.ScrapeError` (Task 5); `Flight.member_price/airline` (Task 1).
- Produces: `frontier.AIRLINE = "F9"`; `frontier.parse_select(payload: dict, origin: str, destination: str, day: date, member: bool = False, scraped_at: datetime | None = None) -> list[Flight]`; `frontier.select_has_flights(payload: dict) -> bool` (any flight at all, nonstop or not); `frontier.parse_calendar(payload: dict, origin: str, destination: str, member: bool = False, scraped_at: datetime | None = None) -> tuple[list[Flight], list[date]]` (flights, days the response answered for — empty when nothing was priced); `frontier.parse_routes(html: str, origin: str) -> set[str]` (destination app codes); `frontier.is_challenge_page(html: str) -> bool`; `frontier.probe_days(flights: list[Flight], limit: int) -> list[date]`.

- [ ] **Step 1: Copy fixtures and write the failing tests**

```bash
mkdir -p tests/fixtures/frontier && cp docs/superpowers/specs/frontier-spike/*.json docs/superpowers/specs/frontier-spike/routes_from_*.html tests/fixtures/frontier/
```

Create `tests/test_frontier.py`:

```python
import json
from datetime import date, datetime
from pathlib import Path

import pytest

import frontier
from browser import ScrapeError

FX = Path(__file__).parent / "fixtures" / "frontier"


def load(name):
    return json.loads((FX / name).read_text())


def test_select_keeps_only_nonstops_with_both_fares():
    payload = load("select_MCO_PHL_2026-11-15.json")          # 25 flights, 5 nonstop
    flights = frontier.parse_select(payload, "MCO", "PHL", date(2026, 11, 15))
    assert len(flights) == 5 and frontier.select_has_flights(payload)
    first = min(flights, key=lambda f: f.departure_datetime)
    assert first.departure_datetime == datetime(2026, 11, 15, 6, 55) and first.arrival_datetime == datetime(2026, 11, 15, 9, 30)
    assert (first.airline, first.flight_number, first.flight_id) == ("F9", "2506", "F9-2506-MCO-PHL")
    assert first.duration_minutes == 155 and first.time_known
    assert (first.price, first.base_price, first.member_price, first.is_member_fare) == (203.98, 203.98, 202.98, False)
    member = frontier.parse_select(payload, "MCO", "PHL", date(2026, 11, 15), member=True)
    assert (min(member, key=lambda f: f.departure_datetime).price, member[0].is_member_fare) == (202.98, True)
    assert len({f.flight_id for f in flights}) == 5           # one row per flight number


def test_select_handles_next_day_arrival_and_single_nonstop():
    (f,) = frontier.parse_select(load("select_BWI_TPA_2026-11-13.json"), "BWI", "TPA", date(2026, 11, 13))
    assert f.departure_datetime == datetime(2026, 11, 13, 21, 59) and f.arrival_datetime == datetime(2026, 11, 14, 0, 34)
    assert (f.base_price, f.member_price) == (71.98, 70.98)


def test_select_rejects_other_routes_days_and_empty_payloads():
    payload = load("select_PHL_MCO_2026-11-12.json")
    assert frontier.parse_select(payload, "PHL", "TPA", date(2026, 11, 12)) == []        # wrong route
    assert frontier.parse_select(payload, "PHL", "MCO", date(2026, 11, 13)) == []        # wrong day
    assert frontier.parse_select({"journeys": []}, "PHL", "MCO", date(2026, 11, 12)) == []
    assert frontier.parse_select({"journeys": [{"flights": None}]}, "PHL", "MCO", date(2026, 11, 12)) == []
    assert not frontier.select_has_flights({"journeys": [{"flights": []}]})
    connecting_only = {"journeys": [{"flights": [f for f in payload["journeys"][0]["flights"] if f["stopCount"] > 0]}]}
    assert frontier.parse_select(connecting_only, "PHL", "MCO", date(2026, 11, 12)) == []
    assert frontier.select_has_flights(connecting_only)                                   # flights, but none nonstop


def test_calendar_gives_discount_den_placeholders_and_answered_days():
    flights, answered = frontier.parse_calendar(load("lowfare_calendar_PHL_MCO_2026-11.json"), "PHL", "MCO")
    assert len(answered) == 30 and answered[0] == date(2026, 11, 1) and answered[-1] == date(2026, 11, 30)
    nov1 = next(f for f in flights if f.departure_date == date(2026, 11, 1))
    assert (nov1.price, nov1.member_price, nov1.base_price) == (155.96, 155.96, None)   # 79.16 + 76.80 taxes
    assert (nov1.flight_id, nov1.airline, nov1.time_known, nov1.is_member_fare) == ("F9CAL-PHL-MCO", "F9", False, False)
    assert all(f.departure_datetime.hour == 0 for f in flights)


def test_calendar_that_is_not_an_answer_covers_nothing():
    with pytest.raises(ScrapeError):
        frontier.parse_calendar({"success": False}, "PHL", "MCO")
    with pytest.raises(ScrapeError):
        frontier.parse_calendar({"success": True, "calendarSections": []}, "PHL", "MCO")
    payload = load("lowfare_calendar_PHL_MCO_2026-11.json")
    for week in payload["calendarSections"][0]["weeks"]:
        for day in week["days"]:
            day["isUnavailable"] = True
    assert frontier.parse_calendar(payload, "PHL", "MCO") == ([], [])    # nothing priced: not "everything sold out"


def test_calendar_unavailable_day_is_answered_without_a_fare():
    payload = load("lowfare_calendar_PHL_MCO_2026-11.json")
    day3 = next(d for w in payload["calendarSections"][0]["weeks"] for d in w["days"] if d["dateFormatted"] == "2026-11-03")
    day3["isUnavailable"] = True
    flights, answered = frontier.parse_calendar(payload, "PHL", "MCO")
    assert date(2026, 11, 3) in answered and date(2026, 11, 3) not in {f.departure_date for f in flights}


def test_route_pages_list_destinations_in_app_codes():
    phl = frontier.parse_routes((FX / "routes_from_PHL.html").read_text(), "PHL")
    assert {"MCO", "TPA", "DEN", "SJU", "PBI"} <= phl and "DJT" not in phl and "PHL" not in phl and len(phl) == 20
    bwi = frontier.parse_routes((FX / "routes_from_BWI.html").read_text(), "bwi")
    assert {"MCO", "DEN", "SFO"} <= bwi and len(bwi) == 20
    assert frontier.parse_routes("<html>maintenance</html>", "PHL") == set()
    assert frontier.parse_routes((FX / "routes_from_BWI.html").read_text(), "PHL") == set()   # another airport's page


def test_challenge_pages_are_recognised():
    assert frontier.is_challenge_page('<div id="px-captcha"></div><p>Press &amp; Hold to confirm you are a human</p>')
    assert frontier.is_challenge_page("<h1>Access Denied</h1>")
    assert not frontier.is_challenge_page("<html><title>Select | Frontier Airlines</title></html>")


def test_probe_days_picks_the_cheapest_distinct_days():
    flights, _ = frontier.parse_calendar(load("lowfare_calendar_PHL_MCO_2026-11.json"), "PHL", "MCO")
    days = frontier.probe_days(flights, 3)
    cheapest = sorted(flights, key=lambda f: (f.price, f.departure_date))[:3]
    assert days == sorted(f.departure_date for f in cheapest) and len(set(days)) == 3
    assert frontier.probe_days([], 3) == []
```

- [ ] **Step 2: Run tests to verify they fail** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_frontier.py -q` → FAIL `ModuleNotFoundError: No module named 'frontier'`.

- [ ] **Step 3: Create `frontier.py`**

```python
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
```

- [ ] **Step 4: Run the whole suite** → all pass. If a fixture number differs from an assertion (the tests were written from the spike's printed values), read the fixture, correct the **test's expected value**, and note it in the report; do not bend the parser.

- [ ] **Step 5: Commit**

```bash
git add frontier.py tests/test_frontier.py tests/fixtures/frontier
git commit -m "feat: Frontier parsers for day search, low-fare calendar and route pages"
```

---

### Task 7: Frontier scraper driver and catalog refresh

**Files:**
- Modify: `frontier.py` (append `refresh_catalog`, `FrontierScraper`)
- Test: `tests/test_frontier.py`

**Interfaces:**
- Consumes: Task 6 parsers; `browser.BaseScraper/BlockedError/ScrapeError/ScrapeReport/local_today/answerable_days` (Task 5); `persist.save_calendar/save_day(..., airline=)` (Task 2); `database.upsert_catalog/catalog_checked/route_nonstop/set_route_nonstop/memberships` (Tasks 3-4); `airlines.frontier_search_url` (Task 3); `netutil.open_url(request, timeout)` (existing).
- Produces: `frontier.refresh_catalog(origins: Iterable[str], conn=None, max_age_days: int = 7, now: datetime | None = None, fetch: Callable[[str], str] | None = None) -> int` (routes written; skips origins checked within `max_age_days`, skips everything when `config.OFFLINE` and no `fetch` is injected; a failed or empty fetch writes nothing); `class FrontierScraper(BaseScraper)` with `airline = "F9"`, `member: bool` (from `database.memberships()`), `async warm_up()`, `async scrape_calendar_month(origin, dest, anchor: date) -> tuple[list[Flight], list[date]]`, `async scrape_day(origin, dest, day) -> list[Flight]`, `async run(routes, dates) -> ScrapeReport`, `async _run_routes(routes, wanted: set[date], today: date, report: ScrapeReport) -> None` (the loop, testable without a browser).

Behaviour of `_run_routes`, per route, for `future = sorted(d for d in wanted if d > today)`:
1. Stop the whole airline when two searches in a row raised `BlockedError`: set `report.blocked = True`, log, break.
2. `known = database.route_nonstop("F9", origin, dest)`. `False` → log "no nonstop (rechecked monthly)", skip the route (not attempted).
3. Calendar phase (`calendar`/`full`): one `scrape_calendar_month` per month in `future`, anchored on that month's first future day; keep flights on wanted future days; `covered = answerable_days(answered, wanted, today)`. Collect `(covered, flights)` per month; a month that raises is `report.fail(...)` and contributes nothing.
4. Day phase: `detail` → every day in `future`; `full` → days the calendar priced; `calendar` → `probe_days(calendar flights, config.FRONTIER_PROBE_DAYS)` only when `known is None`, else none.
5. Each day search: `persist.save_day(run_id, origin, dest, day, flights, airline="F9")`; a day with at least one nonstop sets `database.set_route_nonstop(..., True)` and `known = True`.
6. After the day phase: if `known is None`, at least one day search succeeded and none found a nonstop → `database.set_route_nonstop(..., False)`.
7. Save the collected calendar months with `persist.save_calendar(..., airline="F9")` **only if `known is True`** (after step 5, so freshly confirmed routes get their placeholders in the same run), leaving out placeholders for days this run just searched and found no nonstop on (those days stay covered, so they are recorded as no-fare).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_frontier.py`:

```python
import asyncio

import config
import database as db
from browser import BlockedError, ScrapeReport
from models import Flight

TODAY = date(2026, 11, 1)
WANTED = {date(2026, 11, d) for d in range(10, 16)}          # Nov 10-15


@pytest.fixture
def f9db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "f.db")
    db.init_db()
    return tmp_path


def cal_flight(day, price):
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(2026, 11, day), price=price,
                  member_price=price, flight_id="F9CAL-PHL-MCO")


def timed_flight(day, hour=8, price=90.0):
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(2026, 11, day, hour), price=price,
                  base_price=price, member_price=price - 1, duration_minutes=166, flight_number="2415", flight_id="F9-2415-PHL-MCO")


def scraper_with(monkeypatch, mode, calendar=None, days=None):
    """A FrontierScraper whose two page methods are scripted: `calendar` = (flights, answered) or an exception;
    `days` maps a date to a list of flights or an exception (missing date → no nonstop)."""
    s = frontier.FrontierScraper(mode=mode, screenshots=False, max_retries=0,
                                 run_id=db.start_run(mode, [("PHL", "MCO")], airline="F9"))
    s.calls = []

    async def fake_calendar(origin, dest, anchor):
        s.calls.append(("cal", anchor))
        if isinstance(calendar, Exception):
            raise calendar
        return calendar

    async def fake_day(origin, dest, day):
        s.calls.append(("day", day))
        result = (days or {}).get(day, [])
        if isinstance(result, Exception):
            raise result
        return result

    async def no_wait(*_a, **_k):
        return None

    monkeypatch.setattr(s, "scrape_calendar_month", fake_calendar)
    monkeypatch.setattr(s, "scrape_day", fake_day)
    monkeypatch.setattr(s, "_polite_delay", no_wait)
    monkeypatch.setattr(s, "_save_state", no_wait)
    monkeypatch.setattr(s, "_snapshot", no_wait)
    return s


def run_routes(s, routes=(("PHL", "MCO"),)):
    report = ScrapeReport()
    asyncio.run(s._run_routes(list(routes), set(WANTED), TODAY, report))
    return report


def stored():
    return sorted((f.flight_id, f.departure_datetime) for f in db.query_flights(["PHL"], ["MCO"], date(2026, 11, 1), date(2026, 11, 30), airlines=["F9"]))


CAL = ([cal_flight(10, 80), cal_flight(11, 60), cal_flight(12, 70), cal_flight(14, 95)], [date(2026, 11, d) for d in range(1, 31)])


def test_unknown_route_is_probed_then_gets_its_placeholders(f9db, monkeypatch):
    s = scraper_with(monkeypatch, "calendar", calendar=CAL, days={date(2026, 11, 11): [timed_flight(11)]})
    report = run_routes(s)
    assert [c for c in s.calls if c[0] == "day"] == [("day", date(2026, 11, d)) for d in (10, 11, 12)]   # 3 cheapest days
    assert db.route_nonstop("F9", "PHL", "MCO") is True and report.failed == 0
    assert ("F9-2415-PHL-MCO", datetime(2026, 11, 11, 8)) in stored()
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 14)) in stored()                                        # placeholder kept
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 11)) not in stored()                                    # shadowed by the timed row
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 10)) not in stored()                                    # searched: no nonstop that day
    assert db.seen_in_run(s.run_id, "PHL", "MCO")[date(2026, 11, 10)] is None


def test_connecting_only_route_stores_nothing_and_is_skipped_next_time(f9db, monkeypatch):
    s = scraper_with(monkeypatch, "calendar", calendar=CAL, days={})          # every probed day: flights, none nonstop
    run_routes(s)
    assert stored() == [] and db.route_nonstop("F9", "PHL", "MCO") is False
    again = scraper_with(monkeypatch, "calendar", calendar=CAL)
    report = run_routes(again)
    assert again.calls == [] and report.attempted == 0


def test_confirmed_route_in_calendar_mode_needs_no_day_searches(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    s = scraper_with(monkeypatch, "calendar", calendar=CAL)
    run_routes(s)
    assert [c[0] for c in s.calls] == ["cal"]
    assert [d for _id, d in stored()] == [datetime(2026, 11, d) for d in (10, 11, 12, 14)]
    seen = db.seen_in_run(s.run_id, "PHL", "MCO")
    assert seen[date(2026, 11, 13)] is None and seen[date(2026, 11, 10)] == 80 and date(2026, 11, 20) not in seen


def test_full_mode_searches_only_the_priced_days(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    s = scraper_with(monkeypatch, "full", calendar=CAL, days={date(2026, 11, 10): [timed_flight(10)]})
    run_routes(s)
    assert [c[1].day for c in s.calls if c[0] == "day"] == [10, 11, 12, 14]


def test_block_keeps_stored_fares_and_stops_the_airline(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    db.upsert_flights([timed_flight(10), timed_flight(11)])
    s = scraper_with(monkeypatch, "detail", days={d: BlockedError("challenge") for d in WANTED})
    report = run_routes(s, routes=[("PHL", "MCO"), ("MCO", "PHL")])
    assert report.blocked and report.failed == 2 and report.succeeded == 0
    assert len([c for c in s.calls if c[0] == "day"]) == 2                    # two in a row, then stop
    assert len(stored()) == 2 and db.seen_in_run(s.run_id, "PHL", "MCO") == {}


def test_failed_calendar_month_covers_nothing(f9db, monkeypatch):
    db.set_route_nonstop("F9", "PHL", "MCO", True)
    db.upsert_flights([cal_flight(13, 99)])
    s = scraper_with(monkeypatch, "calendar", calendar=frontier.ScrapeError("calendar response missing"))
    report = run_routes(s)
    assert report.failed == 1 and ("F9CAL-PHL-MCO", datetime(2026, 11, 13)) in stored()
    empty = scraper_with(monkeypatch, "calendar", calendar=([], []))            # answered nothing
    run_routes(empty)
    assert ("F9CAL-PHL-MCO", datetime(2026, 11, 13)) in stored() and db.seen_in_run(empty.run_id, "PHL", "MCO") == {}


def test_refresh_catalog_writes_both_directions_and_survives_failures(f9db, monkeypatch):
    pages = {"philadelphia": (FX / "routes_from_PHL.html").read_text(), "baltimore": "<html>maintenance</html>"}

    def fetch(url):
        return pages[url.rsplit("flights-from-", 1)[1]]

    now = datetime(2026, 10, 7, 9)
    assert frontier.refresh_catalog(["PHL", "BWI", "ILG"], now=now, fetch=fetch) == 40          # 20 destinations × 2 directions
    assert ("MCO", "PHL") in db.catalog_routes("F9", now=now) and db.catalog_routes("F9", origins=["BWI"], now=now) == []
    assert frontier.refresh_catalog(["PHL"], now=now, fetch=lambda url: 1 / 0) == 0             # fresh: not fetched at all

    def boom(url):
        raise OSError("offline")

    later = datetime(2026, 10, 20, 9)
    assert frontier.refresh_catalog(["PHL"], now=later, fetch=boom) == 0                        # stale + failure: keep what we have
    assert ("PHL", "MCO") in db.catalog_routes("F9", now=later)
    monkeypatch.setattr(config, "OFFLINE", True)
    assert frontier.refresh_catalog(["BWI"], now=later) == 0                                    # offline, no injected fetch
```

- [ ] **Step 2: Run tests to verify they fail** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_frontier.py -q` → FAIL `module 'frontier' has no attribute 'FrontierScraper'`.

- [ ] **Step 3: Append to `frontier.py`** (extend the imports at the top with `import asyncio`, `import urllib.request`, `from collections.abc import Callable, Iterable`, `from datetime import timedelta`, `from playwright.async_api import TimeoutError as PlaywrightTimeout, async_playwright`, `import config`, `import database`, `import persist`, `from browser import BaseScraper, BlockedError, ScrapeReport, answerable_days, local_today`, `from netutil import open_url`):

```python
# --------------------------------------------------------------------------- #
# Route catalog
# --------------------------------------------------------------------------- #


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENTS[0], "Accept-Language": "en-US,en;q=0.9"})
    with open_url(request, 20) as response:
        return response.read().decode("utf-8", "replace")


def refresh_catalog(origins: Iterable[str], conn=None, max_age_days: int = 7, now: datetime | None = None,
                    fetch: Callable[[str], str] | None = None) -> int:
    """Re-read Frontier's public route pages for home airports whose catalog is older than `max_age_days`.

    Never removes routes: an unreachable or changed page writes nothing, and routes that stop being listed
    age out of `database.catalog_routes` on their own.
    """
    if fetch is None:
        if config.OFFLINE:
            return 0
        fetch = _http_get
    now = now or datetime.now()
    written = 0
    for origin in origins:
        origin = origin.strip().upper()
        slug = config.FRONTIER_ROUTE_PAGES.get(origin)
        if not slug:
            continue
        checked = database.catalog_checked(AIRLINE, origin, conn)
        if checked is not None and now - checked < timedelta(days=max_age_days):
            continue
        try:
            destinations = parse_routes(fetch(config.FRONTIER_ROUTES_URL.format(slug=slug)), origin)
        except Exception as exc:  # noqa: BLE001 - the catalog is best-effort; keep what we have
            log.warning("Frontier route page for %s unavailable: %s", origin, exc)
            continue
        if not destinations:
            log.warning("Frontier route page for %s listed no destinations; keeping the stored catalog", origin)
            continue
        pairs = [(origin, d) for d in destinations] + [(d, origin) for d in destinations]
        written += database.upsert_catalog(AIRLINE, pairs, "route-page", conn, now=now)
    return written


# --------------------------------------------------------------------------- #
# Browser driver
# --------------------------------------------------------------------------- #

_FLIGHT_DATA_READY = "window.FlightDataJson && window.FlightDataJson.journeys"
_FLIGHT_DATA_READ = "JSON.parse(JSON.stringify(window.FlightDataJson))"


class FrontierScraper(BaseScraper):
    airline = AIRLINE

    def __init__(self, *args, member: bool | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.member = database.memberships().get(AIRLINE, False) if member is None else member
        self._blocked_streak = 0

    async def run(self, routes: Iterable[tuple[str, str]], dates: Iterable[date]) -> ScrapeReport:
        """Scrape every route for every future date, writing to SQLite as it goes."""
        wanted, today, report = set(dates), local_today(), ScrapeReport()
        if not any(d > today for d in wanted):
            return report
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless, args=list(config.BROWSER_ARGS))
            try:
                await self._new_page(browser)
                await self.warm_up()
                await self._run_routes(list(routes), wanted, today, report)
            finally:
                await browser.close()
        return report

    async def warm_up(self) -> None:
        """Land on the home page first, as a person would, so the booking site sees a normal referrer and cookies."""
        await self.page.goto(config.FRONTIER_HOME_URL, wait_until="domcontentloaded")
        await self._human_pause()

    async def _run_routes(self, routes: list[tuple[str, str]], wanted: set[date], today: date, report: ScrapeReport) -> None:
        future = sorted(d for d in wanted if d > today)
        for origin, dest in routes:
            origin, dest = origin.upper(), dest.upper()
            if self._blocked_streak >= 2:
                report.blocked = True
                log.error("Frontier is challenging us; stopping this airline for the run")
                break
            known = database.route_nonstop(AIRLINE, origin, dest)
            if known is False:
                log.info("%s→%s: no Frontier nonstop (rechecked monthly), skipping", origin, dest)
                continue

            months: list[tuple[list[date], list[Flight]]] = []
            calendar_flights: list[Flight] = []
            if self.mode in ("calendar", "full"):
                for anchor in sorted({min(d for d in future if (d.year, d.month) == ym) for ym in {(d.year, d.month) for d in future}}):
                    flights, answered = await self._attempt(report, f"{origin}-{dest} calendar {anchor:%b %Y}",
                                                            self.scrape_calendar_month, origin, dest, anchor) or ([], [])
                    flights = [self._tag(f) for f in flights if f.departure_date in wanted and f.departure_date > today]
                    months.append((answerable_days(answered, wanted, today), flights))
                    calendar_flights.extend(flights)
                    if self._blocked_streak >= 2:
                        break

            if self.mode == "detail":
                days = future
            elif self.mode == "full":
                days = sorted({f.departure_date for f in calendar_flights})
            else:
                days = probe_days(calendar_flights, config.FRONTIER_PROBE_DAYS) if known is None else []

            searched, no_nonstop = 0, set()
            for day in days:
                if self._blocked_streak >= 2:
                    break
                flights = await self._attempt(report, f"{origin}-{dest} {day}", self.scrape_day, origin, dest, day)
                if flights is None:
                    continue
                searched += 1
                flights = [self._tag(f) for f in flights]
                report.flights_written += persist.save_day(self.run_id, origin, dest, day, flights, airline=AIRLINE)
                if not flights:
                    no_nonstop.add(day)  # searched just now: the calendar's fare that day is a connection
                elif known is not True:
                    database.set_route_nonstop(AIRLINE, origin, dest, True)
                    known = True
                log.info("F9 %s→%s %s: %d nonstop(s)", origin, dest, day, len(flights))

            if known is None and searched:
                database.set_route_nonstop(AIRLINE, origin, dest, False)
                log.info("F9 %s→%s: %d day(s) searched, no nonstop — treated as connecting-only", origin, dest, searched)
            if known is True:
                for covered, flights in months:
                    flights = [f for f in flights if f.departure_date not in no_nonstop]
                    report.flights_written += persist.save_calendar(self.run_id, origin, dest, covered, flights, airline=AIRLINE)

    async def _attempt(self, report: ScrapeReport, tag: str, fn, *args):
        """One counted search: returns its result, or None after recording the failure."""
        report.attempted += 1
        try:
            result = await self._retry(fn, tag.replace(" ", "-"), *args)
            report.succeeded += 1
            self._blocked_streak = 0
            await self._save_state()
            return result
        except BlockedError as exc:
            self._blocked_streak += 1
            await self._snapshot(tag.replace(" ", "-"), 0)
            report.fail(f"F9 {tag}: blocked ({exc})")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            self._blocked_streak = 0
            report.fail(f"F9 {tag}: {exc}")
        finally:
            await self._polite_delay()
        return None

    async def scrape_calendar_month(self, origin: str, dest: str, anchor: date) -> tuple[list[Flight], list[date]]:
        """The month calendar containing `anchor`: lowest Discount Den total per day."""
        page = self.page
        url = airlines.frontier_search_url(origin, dest, anchor, calendar=True, promo=self.promo_code or "")
        try:
            async with page.expect_response(lambda r: config.FRONTIER_CALENDAR_ENDPOINT in r.url) as info:
                await page.goto(url, wait_until="domcontentloaded")
            payload = await (await info.value).json()
        except PlaywrightTimeout:
            if is_challenge_page(await page.content()):
                raise BlockedError("challenge page instead of the calendar") from None
            raise
        return parse_calendar(payload, origin, dest, member=self.member)

    async def scrape_day(self, origin: str, dest: str, day: date) -> list[Flight]:
        """One day's search: every nonstop with its times and both fares ([] when the day has none)."""
        page = self.page
        await page.goto(airlines.frontier_search_url(origin, dest, day, promo=self.promo_code or ""), wait_until="domcontentloaded")
        try:
            await page.wait_for_function(_FLIGHT_DATA_READY)
        except PlaywrightTimeout:
            if is_challenge_page(await page.content()):
                raise BlockedError("challenge page instead of results") from None
            raise ScrapeError("results page has no FlightDataJson (layout may have changed)") from None
        payload = await page.evaluate(_FLIGHT_DATA_READ)
        if self.promo_code:
            self.promo_applied = True
        return parse_select(payload, origin, dest, day, member=self.member)
```

Note for the implementer: `_attempt` already counts `attempted/succeeded/failed`; the loop must not count again. The tests replace `scrape_calendar_month`/`scrape_day` on the instance and use `max_retries=0`, so `_retry` runs for real but never sleeps.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add frontier.py tests/test_frontier.py
git commit -m "feat: Frontier scraper with nonstop gate, block detection and route-catalog refresh"
```

---

### Task 8: `--airlines` in the CLI; one run per airline

**Files:**
- Modify: `run_scraper.py` (`parse_airlines`, `parse_promos`, `routes_for`, `build_command`, `build_parser`, `_run` split into `_run` + `_run_airline`)
- Test: `tests/test_run_scraper.py`

**Interfaces:**
- Consumes: `config.ACTIVE_AIRLINES/KNOWN_AIRLINES` (Task 3); `database.start_run(airline=)` (Task 1); `database.catalog_routes` (Task 4); `frontier.refresh_catalog`, `frontier.FrontierScraper` (Task 7); `ScrapeReport.blocked` (Task 5).
- Produces: `run_scraper.parse_airlines(spec: str | None) -> list[str]` (unknown → `SystemExit`); `run_scraper.parse_promos(values: list[str] | None) -> dict[str, str]` (`"CODE"` → Avelo, `"F9=CODE"` → that airline); `run_scraper.routes_for(airline, args) -> list[tuple[str, str]]`; `build_command(..., airlines: list[str] | None = None)` appends `--airlines A,B` after `--origins`; `--promo` may be given several times; `main()` exit code = 3 when locked, else the worst per-airline code; one `scrape_runs` row per airline; signals run per airline run, alerts are evaluated once after the last airline with all signal events.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_scraper.py`:

```python
import frontier
from browser import ScrapeReport


def test_parse_airlines_and_promos():
    assert run_scraper.parse_airlines(None) == config.ACTIVE_AIRLINES
    assert run_scraper.parse_airlines("f9,xp,F9") == ["F9", "XP"]
    with pytest.raises(SystemExit):
        run_scraper.parse_airlines("XP,ZZ")
    assert run_scraper.parse_promos(None) == {}
    assert run_scraper.parse_promos(["save20", "f9=den10"]) == {"XP": "SAVE20", "F9": "DEN10"}
    with pytest.raises(SystemExit):
        run_scraper.parse_promos(["ZZ=X"])


def test_build_command_passes_airlines():
    cmd = build_command(["MCO"], date(2026, 11, 10), date(2026, 11, 15), origins=["PHL"], airlines=["f9"], python="py")
    assert cmd[cmd.index("--airlines") + 1] == "F9"
    assert "--airlines" not in build_command(["MCO"], date(2026, 11, 10), date(2026, 11, 15), python="py")


def test_frontier_routes_come_from_the_catalog(isolated, monkeypatch):
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL"), ("PHL", "DEN"), ("DEN", "PHL"), ("BWI", "MCO"), ("MCO", "BWI")], "route-page")
    args = build_parser().parse_args(["--airlines", "F9", "--origins", "PHL", "--destinations", "MCO"])
    assert run_scraper.routes_for("F9", args) == [("MCO", "PHL"), ("PHL", "MCO")]
    everything = build_parser().parse_args(["--airlines", "F9", "--origins", "PHL"])
    assert len(run_scraper.routes_for("F9", everything)) == 4
    assert ("ILG", "TPA") in run_scraper.routes_for("XP", build_parser().parse_args(["--destinations", "TPA", "--origins", "ILG"]))


class FakeScraper:
    made = []

    def __init__(self, **kw):
        self.kw = kw
        FakeScraper.made.append(self)

    async def run(self, routes, dates):
        r = ScrapeReport(attempted=2, succeeded=2, flights_written=5)
        if self.kw.get("promo_code") == "FAIL":
            r = ScrapeReport(attempted=2, succeeded=1, failed=1, failures=["x"], blocked=True)
        return r


def test_one_run_per_airline_with_its_own_status_and_promo(isolated, monkeypatch):
    FakeScraper.made = []
    monkeypatch.setattr(run_scraper, "scraper_class", lambda code: FakeScraper)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    database.init_db()
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    code = run_scraper.main(["--airlines", "XP,F9", "--origins", "ILG,PHL", "--destinations", "MCO,TPA", "--days", "5",
                             "--no-alerts", "--promo", "save20", "--promo", "F9=fail"])
    assert code == 1                                                     # the Frontier part was partial
    runs = {r["airline"]: r for r in database.last_runs(2)}
    assert (runs["XP"]["status"], runs["F9"]["status"]) == ("ok", "partial")
    assert runs["F9"]["summary"]["blocked"] is True and runs["XP"]["promo_code"] == "SAVE20"
    assert [s.kw["promo_code"] for s in FakeScraper.made] == ["SAVE20", "FAIL"]
    assert runs["F9"]["routes"] == ["MCO-PHL", "PHL-MCO"]


def test_airline_without_routes_is_skipped_not_failed(isolated, monkeypatch):
    monkeypatch.setattr(run_scraper, "scraper_class", lambda code: FakeScraper)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    assert run_scraper.main(["--airlines", "F9", "--origins", "PHL", "--destinations", "MCO", "--days", "3", "--no-alerts"]) == 0
    assert database.last_runs(1) == []                                    # empty catalog: nothing to do


def test_mock_mode_is_avelo_only(isolated):
    assert run_scraper.main(["--mock", "--airlines", "XP,F9", "--destinations", "TPA", "--days", "7", "--no-alerts", "--origins", "ILG"]) == 0
    assert [r["airline"] for r in database.last_runs(5)] == ["XP"]
```

- [ ] **Step 2: Run tests to verify they fail** — `AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_run_scraper.py -q` → FAIL `no attribute 'parse_airlines'`.

- [ ] **Step 3: Implement in `run_scraper.py`**

Add after `parse_origins`:

```python
def parse_airlines(spec: str | None) -> list[str]:
    if not spec:
        return list(config.ACTIVE_AIRLINES)
    codes = [c.strip().upper() for c in spec.split(",") if c.strip()]
    unknown = [c for c in codes if c not in config.KNOWN_AIRLINES]
    if unknown:
        raise SystemExit(f"Unknown airline(s): {', '.join(unknown)} (known: {', '.join(config.KNOWN_AIRLINES)})")
    return list(dict.fromkeys(codes))


def parse_promos(values: list[str] | None) -> dict[str, str]:
    """--promo CODE applies to Avelo; --promo F9=CODE to that airline."""
    promos: dict[str, str] = {}
    for value in values or []:
        airline, sep, code = value.strip().partition("=")
        airline, code = (airline.upper(), code) if sep else ("XP", airline)
        if airline not in config.KNOWN_AIRLINES:
            raise SystemExit(f"Unknown airline in --promo: {value}")
        if code.strip():
            promos[airline] = code.strip().upper()
    return promos


def scraper_class(airline: str):
    """Imported lazily so --mock works without Playwright."""
    if airline == "F9":
        from frontier import FrontierScraper
        return FrontierScraper
    from scraper import AveloScraper
    return AveloScraper


def routes_for(airline: str, args: argparse.Namespace) -> list[tuple[str, str]]:
    """The directional routes one airline's scraper should visit for this request."""
    if args.routes:
        return parse_routes(args.routes)
    destinations = parse_destinations(args.destinations) or None
    origins = parse_origins(args.origins)
    if airline == "F9":
        import frontier

        frontier.refresh_catalog(origins)
        return database.catalog_routes("F9", origins=origins, destinations=destinations)
    avelo = [d for d in destinations if d in config.DESTINATIONS] if destinations else None
    if destinations and not avelo:
        return []
    return config.default_routes(avelo, origins)
```

`build_command`: add `airlines: list[str] | None = None` (after `origins`) and, right after the `--origins` block:

```python
    if airlines:
        cmd += ["--airlines", ",".join(a.upper() for a in airlines)]
```

`build_parser`: add `p.add_argument("--airlines", help="Comma list of airline codes: XP (Avelo), F9 (Frontier). Default: AVELO_AIRLINES")`; change `--promo` to `action="append"` with help `"Promo code for every search: CODE (Avelo) or F9=CODE; repeatable"`; update the description to `"Scrape fares from your home airports (AVELO_HOME_AIRPORTS) into SQLite."`.

Replace `_run` with:

```python
def _run(args: argparse.Namespace, log: logging.Logger) -> int:
    database.init_db()
    housekeeping = database.maintenance()
    log.info("maintenance: %s", ", ".join(f"{k}={v}" for k, v in housekeeping.items()))
    dates = build_dates(args.start, args.end, args.days)
    promos = parse_promos(args.promo)
    airline_codes = ["XP"] if args.mock else parse_airlines(args.airlines)  # the mock schedule is Avelo's

    work = [(code, routes_for(code, args)) for code in airline_codes]
    for code, routes in work:
        if not routes:
            log.info("%s: no routes for this request, skipping", code)
    work = [(code, routes) for code, routes in work if routes]

    exit_code, signal_events = 0, []
    for index, (code, routes) in enumerate(work):
        last = index == len(work) - 1
        log.info("%s: routes=%d (%s) dates=%s..%s", code, len(routes), ",".join(f"{o}-{d}" for o, d in routes), dates[0], dates[-1])
        exit_code = max(exit_code, _run_airline(code, routes, dates, promos.get(code), args, log, signal_events, last))
    return exit_code


def _run_airline(code: str, routes: list[tuple[str, str]], dates: list[date], promo: str | None,
                 args: argparse.Namespace, log: logging.Logger, signal_events: list, last: bool) -> int:
    """One airline's scrape = one scrape_runs row. Alerts are evaluated once, after the last airline."""
    run_id = database.start_run("mock" if args.mock else args.mode, routes, promo, airline=code)
    summary: dict = {}
    status, exit_code = "failed", 1
    try:
        if args.mock:
            flights = generate_mock_flights(dates[0], len(dates), routes, seed=args.mock_seed, discount=args.mock_sale)
            written = database.upsert_flights(flights)
            for origin, dest in routes:
                if config.PRIMARY_HOME in (origin, dest):  # the mock schedule only flies PRIMARY_HOME routes
                    database.record_seen(run_id, origin, dest, scheduled_days(origin, dest, dates), flights)
            log.info("mock mode: wrote %d flights", written)
            summary = {"flights_written": written, "mock": True}
            status, exit_code = "ok", 0
        else:
            scraper = scraper_class(code)(headless=not args.headful and config.HEADLESS, mode=args.mode,
                                          promo_code=promo, run_id=run_id)
            started = datetime.now()
            report = asyncio.run(scraper.run(routes, dates))
            log.info(
                "%s done in %s: %d flights written, %d/%d searches ok, %d failed, %d routes skipped (not scheduled)",
                code, datetime.now() - started, report.flights_written, report.succeeded, report.attempted,
                report.failed, len(report.skipped_routes),
            )
            if report.skipped_routes:
                log.info("skipped (no flights in range): %s", ", ".join(report.skipped_routes))
            for failure in report.failures:
                log.warning("failed: %s", failure)
            summary = {
                "flights_written": report.flights_written, "attempted": report.attempted, "succeeded": report.succeeded,
                "failed": report.failed, "skipped": report.skipped_routes, "failures": report.failures,
                "blocked": report.blocked,
            }
            if report.failed == 0 and not report.blocked:
                status, exit_code = "ok", 0
            else:
                status, exit_code = ("partial" if report.succeeded else "failed"), 1

        if not args.no_alerts:
            events = signals.run_detection(run_id, allow_sold_out=(status == "ok"))
            signal_events.extend(events)
            summary["signals"] = len(events)
            if last:
                summary["alerts"] = alerts.evaluate_and_deliver(run_id, signal_events)
    except Exception as exc:  # noqa: BLE001 - always close the run row
        log.exception("%s scrape crashed: %s", code, exc)
        summary["error"] = repr(exc)[:500]
        status, exit_code = "failed", 1
    finally:
        database.finish_run(run_id, summary, status=status)
    return exit_code
```

Existing tests that monkeypatch `scraper.AveloScraper` through `run_scraper` (if any patch `run_scraper.AveloScraper` or the lazy import) switch to `monkeypatch.setattr(run_scraper, "scraper_class", lambda code: Fake)`. Existing callers pass `promo=` as one string to `build_command`; `--promo` being `append` still parses it. Add `python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full` to the module docstring examples.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add run_scraper.py tests/test_run_scraper.py
git commit -m "feat: --airlines; one scrape run per airline with per-airline promo and status"
```

---

### Task 9: Live check and documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `.env.example`

- [ ] **Step 1: Migration dry run on a copy of the real database**

```bash
cd ~/avelo-finder && mkdir -p .superpowers/probe && sqlite3 data/flights.db ".backup '.superpowers/probe/flights-copy.db'"
AVELO_DB_PATH=.superpowers/probe/flights-copy.db AVELO_OFFLINE=1 .venv/bin/python - <<'EOF'
import database
conn = database.get_connection()
print("airlines:", dict(conn.execute("SELECT airline, COUNT(*) FROM avelo_flights GROUP BY airline").fetchall()))
print("member_price set on member fares:", conn.execute("SELECT COUNT(*) FROM avelo_flights WHERE is_member_fare = 1 AND member_price IS NULL").fetchone()[0] == 0)
print("run airlines:", dict(conn.execute("SELECT airline, COUNT(*) FROM scrape_runs GROUP BY airline").fetchall()))
print("index:", "idx_flights_airline_route" in {r["name"] for r in conn.execute("PRAGMA index_list(avelo_flights)")})
print("catalog table:", conn.execute("SELECT COUNT(*) FROM route_catalog").fetchone()[0])
EOF
rm -f .superpowers/probe/flights-copy.db*
```

Expected: every fare is `XP`; `member_price set on member fares: True`; all runs `XP`; `index: True`; `catalog table: 0`. Paste the output into the report.

- [ ] **Step 2: Live Frontier check (controller runs this with the owner present — implementers skip it)**

```bash
.venv/bin/python run_scraper.py --airlines F9 --origins PHL --destinations MCO --start <a Tuesday ~5 weeks out> --end <that Sunday> --mode full --no-alerts
```

Expected: exit 0; log shows the route-page refresh (or its warning), `calendar` attempts, `N nonstop(s)` lines; `scrape_runs` gains one `F9` row with status `ok`; `SELECT flight_id, departure_datetime, price, base_price, member_price FROM avelo_flights WHERE airline='F9'` shows `F9-<number>-PHL-MCO` rows with `price == base_price` and `member_price` slightly lower; the dashboard's packages are unchanged (Avelo only). Then run an Avelo calendar scrape of one destination and confirm the Frontier rows are still there. If the site challenges the run (`blocked`), stop and report — do not loosen the stealth settings without the owner.

- [ ] **Step 3: Documentation**

`.env.example` — add:

```bash
# Airlines a scrape covers when --airlines is not given: XP (Avelo), F9 (Frontier). Frontier fares are
# stored but not shown in packages until Phase 3.
AVELO_AIRLINES=XP
```

`CLAUDE.md`: add layout rows for `airlines.py` ("airline registry: codes, membership programs, owner-price rule, Frontier URLs/site codes"), `browser.py` ("BaseScraper shared by every airline scraper: stealth context, per-airline cookie file `data/browser_state_<code>.json`, retry, snapshot (.png + .html), ScrapeReport"), `frontier.py` ("Frontier parsers over `window.FlightDataJson` + calendar JSON + public route pages; FrontierScraper; nonstop gate"); update the `database.py` row (airline + member_price columns, `scrape_runs.airline`, `route_catalog`, memberships); add a section "How Frontier's site works" (copy the three bullets from `frontier.py`'s module docstring, plus: headless works with the shared stealth setup; `DJT` = West Palm Beach = app code `PBI`; most flights are connections); add to Conventions: "Every fare delete is scoped to one airline", "One scrape run per airline; baselines compare same-airline runs", "An index on a migrated column is created in `_migrate`, not `SCHEMA`", "Until Phase 3, packages/alerts read `XP` rows only (`optimizer.find_itineraries`)"; add the `--airlines F9` example to the run commands; update the test count to the number you observe.

`README.md`: mention Frontier scraping (`--airlines F9`), the new modules, and that Frontier fares are stored but not yet shown.

- [ ] **Step 4: Final full suite and commit**

Run: `AVELO_OFFLINE=1 .venv/bin/python -m pytest -q` → all pass.

```bash
git add CLAUDE.md README.md .env.example
git commit -m "docs: phase 2 — airline data model, shared browser base, Frontier scraper"
```
