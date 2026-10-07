# Phase 3 — Packages, Signals, Alerts and Hand-off Across Airlines — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stored Frontier fares usable: packages mix Avelo and Frontier legs, signals and alerts work per airline, alerts stop repeating or getting lost, the nightly job scrapes what rules and watches need, and "open on the airline's site" works for both airlines.

**Architecture:** `GetawayQuery.airlines` selects which airlines' rows the optimizer reads; `Itinerary` learns each leg's airline, per-airline add-on costs and the "program would save" figure. `route_floor` and `events` gain an `airline` key so signals are computed per airline. Alert evaluation returns *pending state writes* that are applied only after a push was delivered. `run_scraper --from-rules` derives targets from saved rules, watches and bookings at run time.

**Tech Stack:** Python 3.13, SQLite, Streamlit ≥1.63, pytest (offline).

**Spec:** `docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md` §5 (this is §8 Phase 3), plus items Phase 2 deferred here: per-airline `route_floor`/`events`, airline-aware dashboard readers, lifting the Avelo-only gates.

**Not in this phase (Phase 4):** sidebar controls for airlines/home airports/open-jaw/carry-on, settings screen (memberships, parking), "Airline vs airline" tab, Discount Den meter widget, multi-hub map. Phase 3 shows mixed packages in the existing table with new columns and nothing else new on screen.

## Global Constraints

- Project root `~/avelo-finder` (never `~/Desktop/avelo-finder`). Branch `phase3-cross-airline` from `main`. Baseline before Task 1: **304 passed**.
- EVERY python/pytest command an agent runs starts with `AVELO_DB_PATH=<project>/.superpowers/probe/sdd-scratch/agent-sandbox.db AVELO_OFFLINE=1`. `data/flights.db` is off limits; for real-shaped data use `sqlite3 "file:data/flights.db?mode=ro" ".backup '<copy>'"` and point `AVELO_DB_PATH` at the copy. The root `conftest.py` guard already refuses the owner's database under pytest — do not weaken it.
- No network in tests. Never print `AVELO_NTFY_TOPIC`. Never send a real push from a test (`config.OFFLINE` or monkeypatched `notify_ntfy`).
- Schema: new tables in `SCHEMA`, new columns in `MIGRATIONS[(table, column)]`, indexes on migrated columns in `_migrate`. A primary-key change is a guarded table rebuild inside `_migrate` (see Task 3).
- Airline codes `XP` (Avelo), `F9` (Frontier); names come from `airlines.get(code).name`.
- `price` = what the owner pays (membership applied at save by `persist.owner_priced`); `base_price` = public; `member_price` = program fare. Never recompute `price` in the optimizer.
- `GetawayQuery.airlines` defaults to every known airline. A **rule saved before this phase** (no `airlines` key) keeps meaning Avelo only; one with no `origins` keeps meaning the primary home.
- Add-ons: Avelo PLUS perks (`config.MEMBER_BENEFITS`, $0) apply to Avelo legs only. Carry-on is counted only when `count_carry_on` is on, per leg, from `config.CARRY_ON_FEE` (owner-editable estimates via env).
- A watch/booking push that could not be delivered must be retried on the next run: its alert state is written only after delivery succeeds (or when no push channel is configured).
- Commits end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; stage only touched files.

## Review Focus

1. **Existing data**: `route_floor` rows and `events` rows written before this phase must survive the rebuild/migration as Avelo (`XP`), and a second open must be a no-op. → tests in Task 3.
2. **Existing rules, watches and bookings** (no `airlines`, no partner fields, no `alert_base`) must behave exactly as before on their first evaluation: no burst of "new match" pushes because Frontier packages now exist, no false price-drop. → tests in Task 4.
3. **A push that fails** (ntfy down) must not consume the alert: the same drop is announced on the next run, once. → tests in Task 4.
4. **A Frontier sale must never move Avelo's floor or raise an "Avelo sale"** on a route both fly (and vice versa). → tests in Task 3.
5. **A mixed-airline package** must cost what its two fares cost: Avelo perks never zero a Frontier carry-on, and the "program would save" figure counts only legs where the owner is not a member. → tests in Task 2.
6. **A hand-off with a Frontier leg** must never drive Avelo's form for it, and must still never book anything. → tests in Task 6.

---

## File Structure

| File | Change |
|---|---|
| `optimizer.py` | `GetawayQuery.airlines`, `count_carry_on`; per-leg add-ons; `Itinerary.airlines/airline_label/carry_on_cost/program_savings`; medians per airline; dataframe columns |
| `config.py` | `CARRY_ON_FEE` |
| `database.py` | `route_medians` keyed by airline; `route_floor` rebuild; `events.airline`; `get_floor/set_floor(airline=)`; `add_events` stores airline; `selection_stats/route_freshness(airlines=)` |
| `signals.py` | `Event.airline`; per-airline detection for every airline; airline in labels |
| `alerts.py` | airlines + partner window in rules; `alert_base`; pending state writes; airline-aware pushes, titles, labels, `watched_routes` |
| `bookings.py` | airline per leg; Avelo-only Plus ROI; `program_savings` |
| `handoff.py` | legs carry an airline; Frontier legs open a URL |
| `run_scraper.py`, `scheduler.py` | `--from-rules` |
| `app.py` | cache key, new columns, hand-off legs, airline-aware readers |
| tests | `tests/test_optimizer.py`, `test_database.py`, `test_signals.py`, `test_alerts.py`, `test_bookings.py`, `test_handoff.py`, `test_run_scraper.py`, `test_scheduler.py`, `test_app.py` |

---

### Task 1: Packages read every selected airline

**Files:**
- Modify: `optimizer.py` (`GetawayQuery`, `Itinerary`, `enrich`, `find_itineraries`, `to_dataframe`), `database.py` (`route_medians`), `alerts.py` (`package_label`), `app.py` (`run_search` cache key; packages `column_order`)
- Test: `tests/test_optimizer.py`, `tests/test_database.py`

**Interfaces:**
- Produces: `GetawayQuery(..., airlines: set[str] = <all known>)` (upper-cased; empty → `ValueError`); `Itinerary.airlines -> tuple[str, str]` (outbound, inbound codes); `Itinerary.airline_label -> str` (`"Avelo"` or `"Avelo out · Frontier back"`); dataframe column `"Airlines"` after `"Home"`; `database.route_medians(...) -> dict[tuple[str, str, str], float]` keyed `(airline, origin, destination)`; `enrich(itineraries, medians, previous)` reads that key; `alerts.package_label` ends with `" · <airline_label>"`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_optimizer.py`:

```python
def leg(airline, origin, dest, day, hour, price, base=None, den=None):
    return Flight(airline=airline, origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour),
                  price=price, base_price=base if base is not None else price, member_price=den, duration_minutes=150,
                  flight_id=f"{airline}-{origin}-{dest}-{hour}")


def test_packages_mix_airlines_and_label_each_leg():
    conn = db.get_connection(":memory:")
    db.upsert_flights([leg("XP", "PHL", "TPA", 8, 8, 90), leg("F9", "PHL", "TPA", 8, 9, 60, den=59),
                       leg("XP", "TPA", "PHL", 11, 15, 80), leg("F9", "TPA", "PHL", 11, 16, 50, den=49)], conn)
    its = find_itineraries(q(origins=["PHL"]), conn=conn)
    assert {i.airlines for i in its} == {("XP", "XP"), ("XP", "F9"), ("F9", "XP"), ("F9", "F9")}
    cheapest = rank_by_price(its)[0]
    assert cheapest.airlines == ("F9", "F9") and cheapest.airline_label == "Frontier" and cheapest.total_price == 110
    mixed = next(i for i in its if i.airlines == ("XP", "F9"))
    assert mixed.airline_label == "Avelo out · Frontier back"
    assert list(to_dataframe([mixed])["Airlines"]) == ["Avelo out · Frontier back"]
    only_avelo = find_itineraries(q(origins=["PHL"], airlines={"xp"}), conn=conn)
    assert {i.airlines for i in only_avelo} == {("XP", "XP")}
    with pytest.raises(ValueError):
        q(airlines=set())
    conn.close()


def test_typical_price_is_per_airline():
    conn = db.get_connection(":memory:")
    db.upsert_flights([leg("XP", "PHL", "TPA", 8, 8, 200), leg("F9", "PHL", "TPA", 8, 9, 60),
                       leg("XP", "TPA", "PHL", 11, 15, 200), leg("F9", "TPA", "PHL", 11, 16, 60)], conn)
    its = {i.airlines: i for i in find_itineraries(q(origins=["PHL"]), conn=conn)}
    assert its[("F9", "F9")].typical_total == 120 and its[("XP", "XP")].typical_total == 400
    assert its[("XP", "F9")].typical_total == 260          # each leg against its own airline's median
    conn.close()
```

Replace `test_packages_use_avelo_rows_only_until_phase_3` in `tests/test_optimizer.py` with:

```python
def test_packages_include_frontier_rows_by_default():
    conn = db.get_connection(":memory:")
    db.upsert_flights([leg("F9", "PHL", "TPA", 8, 9, 60), leg("F9", "TPA", "PHL", 11, 16, 60),
                       f("ILG", "TPA", 8, hour=8), f("TPA", "ILG", 11, hour=15)], conn)
    its = find_itineraries(q(origins=["ILG", "PHL"]), conn=conn)
    assert {i.airlines for i in its} == {("XP", "XP"), ("F9", "F9")}
    conn.close()
```

In `tests/test_database.py`, update every assertion on `route_medians` results to the three-part key (e.g. `{("XP", "ILG", "TPA"): 80, ...}`) and add:

```python
def test_route_medians_are_keyed_by_airline(conn):
    db.upsert_flights([flight(origin="PHL", dest="MCO", day=10, price=100, number="XP1"), f9(price=60.0)], conn)
    med = db.route_medians(["PHL"], ["MCO"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)
    assert med == {("XP", "PHL", "MCO"): 100, ("F9", "PHL", "MCO"): 60}
    assert db.route_medians(["PHL"], ["MCO"], date(2026, 10, 1), date(2026, 10, 31), conn=conn, airlines=["F9"]) == {("F9", "PHL", "MCO"): 60}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `AVELO_DB_PATH=$PWD/.superpowers/probe/sdd-scratch/agent-sandbox.db AVELO_OFFLINE=1 .venv/bin/python -m pytest tests/test_optimizer.py tests/test_database.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'airlines'` (GetawayQuery), `AttributeError: 'Itinerary' object has no attribute 'airlines'`.

- [ ] **Step 3: `database.route_medians`** — select `airline` too, order by `airline, origin, destination, price`, and key the result `(r["airline"], r["origin"], r["destination"])`; update the return annotation and docstring ("per airline and directional route").

- [ ] **Step 4: `optimizer.py`**

Add `import airlines as airline_registry` to the imports. `GetawayQuery`: add the field after `allow_open_jaw`:

```python
    airlines: set[str] = field(default_factory=lambda: set(config.KNOWN_AIRLINES))  # whose fares to build from
```

and in `__post_init__`:

```python
        self.airlines = {a.strip().upper() for a in self.airlines}
        if not self.airlines:
            raise ValueError("at least one airline is required")
```

`Itinerary`: add after `home_label`:

```python
    @property
    def airlines(self) -> tuple[str, str]:
        """(outbound airline, inbound airline) codes."""
        return self.outbound.airline, self.inbound.airline

    @property
    def airline_label(self) -> str:
        out, back = (airline_registry.get(code).name for code in self.airlines)
        return out if out == back else f"{out} out · {back} back"
```

`enrich`: medians lookups become

```python
        m_out = medians.get((it.outbound.airline, it.outbound.origin, it.outbound.destination))
        m_in = medians.get((it.inbound.airline, it.inbound.origin, it.inbound.destination))
```

`find_itineraries`: replace the three `airlines=["XP"]` arguments with `airlines=sorted(query.airlines)` and delete the "Phase 3 makes the airline set…" comment. `to_dataframe`: add `"Airlines": it.airline_label,` after `"Home"`.

- [ ] **Step 5: Callers** — `alerts.package_label`: append `f" · {it.airline_label}"` to the returned string (update its docstring, and any test asserting the exact old label text). `app.py`: add `tuple(sorted(query.airlines)),` to the `run_search` cache key tuple; add `"Airlines"` after `"Home"` in the packages table `column_order`. Run `grep -rn "route_medians(" --include='*.py' . | grep -v .venv` and fix any other reader of the old two-part key.

- [ ] **Step 6: Run the whole suite** → all pass.

- [ ] **Step 7: Commit**

```bash
git add optimizer.py database.py alerts.py app.py tests/test_optimizer.py tests/test_database.py tests/test_alerts.py
git commit -m "feat: packages are built from every selected airline, each leg labelled"
```

---

### Task 2: Per-airline costs — perks, carry-on, "program would save"

**Files:**
- Modify: `config.py` (`CARRY_ON_FEE`), `optimizer.py` (`GetawayQuery.count_carry_on`, `Itinerary` cost properties, `build_itineraries` budget test, `enrich`, `to_dataframe`), `app.py` (cache key + two columns)
- Test: `tests/test_optimizer.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Itinerary.airlines` (Task 1); `Flight.member_price/base_price/is_member_fare`.
- Produces: `config.CARRY_ON_FEE: dict[str, float]` (env `AVELO_CARRY_ON_XP` default `0`, `AVELO_CARRY_ON_F9` default `55`); `GetawayQuery.count_carry_on: bool = False`; `Itinerary.count_carry_on: bool = False` (set by `build_itineraries` from the query); `Itinerary.carry_on_cost -> float`; `Itinerary.addon_fees -> float` (Avelo PLUS perks on Avelo legs + `carry_on_cost`); `Itinerary.total_price` = fares + `addon_fees`; `Itinerary.program_savings -> float` (sum over legs where the owner is **not** paying the program fare of `price - member_price`, when positive); dataframe columns `"Carry-on"` and `"Program would save"`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_optimizer.py`:

```python
def test_carry_on_is_charged_per_leg_by_airline(monkeypatch):
    monkeypatch.setattr(config, "CARRY_ON_FEE", {"XP": 0.0, "F9": 55.0})
    outs = [leg("XP", "PHL", "TPA", 8, 8, 90), leg("F9", "PHL", "TPA", 8, 9, 60)]
    backs = [leg("XP", "TPA", "PHL", 11, 15, 80), leg("F9", "TPA", "PHL", 11, 16, 50)]
    plain = {i.airlines: i for i in build_itineraries(outs, backs, q(origins=["PHL"]))}
    assert plain[("F9", "F9")].total_price == 110 and plain[("F9", "F9")].carry_on_cost == 0
    counted = {i.airlines: i for i in build_itineraries(outs, backs, q(origins=["PHL"], count_carry_on=True))}
    assert counted[("F9", "F9")].carry_on_cost == 110 and counted[("F9", "F9")].total_price == 220
    assert counted[("XP", "F9")].carry_on_cost == 55 and counted[("XP", "F9")].total_price == 90 + 50 + 55
    assert counted[("XP", "XP")].total_price == 170
    assert rank_by_price(list(counted.values()))[0].airlines == ("XP", "XP")     # bags flip the ranking
    assert "Carry-on" in to_dataframe(list(counted.values())).columns


def test_budget_counts_the_carry_on_when_asked(monkeypatch):
    monkeypatch.setattr(config, "CARRY_ON_FEE", {"XP": 0.0, "F9": 55.0})
    outs, backs = [leg("F9", "PHL", "TPA", 8, 9, 60)], [leg("F9", "TPA", "PHL", 11, 16, 50)]
    assert len(build_itineraries(outs, backs, q(origins=["PHL"], max_budget=150))) == 1
    assert build_itineraries(outs, backs, q(origins=["PHL"], max_budget=150, count_carry_on=True)) == []


def test_program_savings_counts_only_legs_paid_at_the_public_fare():
    out = leg("F9", "PHL", "TPA", 8, 9, 84, base=84, den=83)                 # not a member: pays 84, Den is 83
    back_member = Flight(airline="XP", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 15),
                         price=70, base_price=99, member_price=70, is_member_fare=True, duration_minutes=150, flight_id="XP-b")
    (it,) = build_itineraries([out], [back_member], q(origins=["PHL"]))
    assert it.program_savings == 1.0                                          # only the Frontier leg
    assert to_dataframe([it])["Program would save"].iloc[0] == 1.0
    no_den = leg("F9", "TPA", "PHL", 11, 16, 50)                              # program fare unknown
    (it2,) = build_itineraries([out], [no_den], q(origins=["PHL"]))
    assert it2.program_savings == 1.0
```

Append to `tests/test_config.py`:

```python
def test_carry_on_fee_estimates():
    assert set(config.CARRY_ON_FEE) == set(config.KNOWN_AIRLINES)
    assert config.CARRY_ON_FEE["XP"] >= 0 and config.CARRY_ON_FEE["F9"] > 0
```

- [ ] **Step 2: Run tests to verify they fail** — expected `TypeError: ... 'count_carry_on'`.

- [ ] **Step 3: `config.py`** — after `DEFAULT_MEMBERSHIPS`:

```python
# What one carry-on bag costs per flight leg, per airline. Estimates the owner can change (env): only added to a
# package's cost when the carry-on toggle is on, so a bare fare is never compared with a fare that includes a bag.
CARRY_ON_FEE: dict[str, float] = {
    "XP": float(os.getenv("AVELO_CARRY_ON_XP", "0")),
    "F9": float(os.getenv("AVELO_CARRY_ON_F9", "55")),
}
```

Add both keys to `.env.example` with a one-line comment.

- [ ] **Step 4: `optimizer.py`** — `GetawayQuery`: add `count_carry_on: bool = False  # add each airline's carry-on fee per leg`. `Itinerary`: add the field `count_carry_on: bool = False` (after `parking_cost`) and replace `addon_fees`/`total_price` with:

```python
    @property
    def carry_on_cost(self) -> float:
        """One carry-on per leg at each leg's airline's fee, when the query asked to count bags."""
        if not self.count_carry_on:
            return 0.0
        return round(sum(config.CARRY_ON_FEE.get(code, 0.0) for code in self.airlines), 2)

    @property
    def addon_fees(self) -> float:
        """Avelo PLUS perks ($0) on Avelo legs, plus carry-on fees when they are being counted."""
        perks = BENEFITS.total if "XP" in self.airlines else 0.0
        return round(perks + self.carry_on_cost, 2)

    @property
    def total_price(self) -> float:
        """Total Getaway Cost: what the owner pays for both fares plus add-on fees."""
        return round(self.fare_total + self.addon_fees, 2)

    @property
    def program_savings(self) -> float:
        """What the airlines' membership programs would take off legs the owner pays the public fare on."""
        saved = 0.0
        for f in (self.outbound, self.inbound):
            if not f.is_member_fare and f.member_price is not None:
                saved += max(f.price - f.member_price, 0.0)
        return round(saved, 2)
```

`build_itineraries`: build the itinerary first and test the budget on its total:

```python
                    it = Itinerary(out, back, w, fit_score(out, back, w), count_carry_on=query.count_carry_on)
                    if query.max_budget is not None and it.total_price > query.max_budget:
                        continue
                    results.append(it)
```

(remove the old `out.price + back.price > query.max_budget` test). `enrich`: the typical and previous totals add `it.addon_fees` as today — keep. `to_dataframe`: add `"Carry-on": it.carry_on_cost,` after `"Add-on Fees"` and `"Program would save": it.program_savings,` after `"Savings vs Public"`.

- [ ] **Step 5: `app.py`** — add `query.count_carry_on,` to the `run_search` cache key; add `"Program would save"` after `"Savings vs Public"` in `column_order` with `st.column_config.NumberColumn(format="$%d", help="What the airline's membership program (e.g. Frontier Discount Den) would take off the legs you pay the public fare on.")`.

- [ ] **Step 6: Run the whole suite** → all pass (existing totals unchanged: perks are $0 and carry-on is off by default).

- [ ] **Step 7: Commit**

```bash
git add config.py optimizer.py app.py .env.example tests/test_optimizer.py tests/test_config.py
git commit -m "feat: per-airline add-ons, optional carry-on cost, and what a membership program would save"
```

---

### Task 3: Signals per airline

**Files:**
- Modify: `database.py` (`SCHEMA` route_floor, `MIGRATIONS` events.airline, `_migrate` rebuild, `get_floor`, `set_floor`, `add_events`), `signals.py` (`Event.airline`, `_label`, `detect_route`, `run_detection`), `app.py` (signals table shows the airline if it lists `origin`)
- Test: `tests/test_database.py`, `tests/test_signals.py`

**Interfaces:**
- Produces: `route_floor(airline, origin, destination, floor_price, seen_at, PRIMARY KEY (airline, origin, destination))`; `events.airline TEXT` (NULL for watch-level events); `database.get_floor(origin, destination, conn=None, airline: str = "XP")`, `database.set_floor(origin, destination, price, conn=None, now=None, airline: str = "XP")`; `signals.Event(kind, origin, destination, day, message, detail={}, notify=True, airline: str | None = None)`; `signals.detect_route(origin, destination, prev, cur, floor, allow_sold_out=True, airline: str = "XP")` (events carry the airline; labels start with the airline name); `signals.run_detection` detects for **every** airline using the run's airline; `signals.SIGNAL_AIRLINES` is removed; `list_events` rows include `airline`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_floors_are_per_airline_and_old_floors_survive(tmp_path):
    path = tmp_path / "old.db"
    db.get_connection(path).close()
    raw = sqlite3.connect(path)
    raw.executescript("""
        DROP TABLE route_floor;
        CREATE TABLE route_floor (origin TEXT NOT NULL, destination TEXT NOT NULL, floor_price REAL NOT NULL,
            seen_at TEXT NOT NULL, PRIMARY KEY (origin, destination));
        INSERT INTO route_floor VALUES ('ILG', 'TPA', 49, '2026-10-01T03:15:00');
    """)
    raw.commit()
    raw.close()
    conn = db.get_connection(path)
    assert db.get_floor("ILG", "TPA", conn) == 49 and db.get_floor("ILG", "TPA", conn, airline="F9") is None
    db.set_floor("ILG", "TPA", 19, conn, airline="f9")
    assert (db.get_floor("ILG", "TPA", conn), db.get_floor("ILG", "TPA", conn, airline="F9")) == (49, 19)
    conn.close()
    conn = db.get_connection(path)                                  # second open: no rebuild, nothing lost
    assert conn.execute("SELECT COUNT(*) FROM route_floor").fetchone()[0] == 2
    conn.close()


def test_events_store_their_airline(conn):
    from signals import Event

    db.add_events(None, [Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "m", airline="F9"),
                         Event("rule_match", None, None, None, "watch-level")], conn)
    rows = {r["kind"]: r["airline"] for r in db.list_events(conn=conn)}
    assert rows == {"sale_wave": "F9", "rule_match": None}
```

Append to `tests/test_signals.py`:

```python
def test_each_airline_has_its_own_floor_and_labels():
    conn = db.get_connection(":memory:")
    days = [D(2), D(4), D(6), D(9), D(11)]

    def priced(airline, price):
        return [Flight(airline=airline, origin="PHL", destination="MCO", departure_datetime=datetime(2026, 10, d.day),
                       price=price, flight_id=f"{airline}CAL") for d in days]

    xp = db.start_run("calendar", [("PHL", "MCO")], conn=conn)
    db.record_seen(xp, "PHL", "MCO", days, priced("XP", 100), conn)
    run_detection(xp, conn)
    db.finish_run(xp, {}, conn)
    f9_run = db.start_run("calendar", [("PHL", "MCO")], conn=conn, airline="F9")
    db.record_seen(f9_run, "PHL", "MCO", days, priced("F9", 40), conn)
    events = run_detection(f9_run, conn)
    assert events and all(e.airline == "F9" and e.message.startswith("Frontier ") for e in events)
    assert "new_floor" not in kinds(events)                          # 40 is Frontier's first floor, not a new low for Avelo
    assert (db.get_floor("PHL", "MCO", conn), db.get_floor("PHL", "MCO", conn, airline="F9")) == (100, 40)
    db.finish_run(f9_run, {}, conn)
    xp2 = db.start_run("calendar", [("PHL", "MCO")], conn=conn)
    db.record_seen(xp2, "PHL", "MCO", days, priced("XP", 100), conn)
    assert "sale_wave" not in kinds(run_detection(xp2, conn))        # Avelo compares with Avelo's run, not Frontier's
    conn.close()
```

Delete the Phase 2 tests that assert Frontier runs raise no signals (search `tests/` for `SIGNAL_AIRLINES` and "raise no signals").

- [ ] **Step 2: Run tests to verify they fail** — expected `TypeError: get_floor() got an unexpected keyword argument 'airline'`.

- [ ] **Step 3: `database.py`** — in `SCHEMA` replace the `route_floor` definition with:

```sql
CREATE TABLE IF NOT EXISTS route_floor (
    airline      TEXT NOT NULL DEFAULT 'XP',
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    floor_price  REAL NOT NULL,
    seen_at      TEXT NOT NULL,
    PRIMARY KEY (airline, origin, destination)
);
```

Add `("events", "airline"): "ALTER TABLE events ADD COLUMN airline TEXT",` to `MIGRATIONS`. In `_migrate`, before the column loop, add the guarded rebuild (SQLite cannot change a primary key in place):

```python
    # route_floor's key gained the airline. Rebuild once; every existing floor is Avelo's.
    if "airline" not in {row["name"] for row in conn.execute("PRAGMA table_info(route_floor)")}:
        conn.executescript("""
            BEGIN;
            ALTER TABLE route_floor RENAME TO route_floor_pre_airline;
            CREATE TABLE route_floor (
                airline TEXT NOT NULL DEFAULT 'XP', origin TEXT NOT NULL, destination TEXT NOT NULL,
                floor_price REAL NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (airline, origin, destination));
            INSERT INTO route_floor (airline, origin, destination, floor_price, seen_at)
                SELECT 'XP', origin, destination, floor_price, seen_at FROM route_floor_pre_airline;
            DROP TABLE route_floor_pre_airline;
            COMMIT;
        """)
```

`get_floor` / `set_floor`: add `airline: str = "XP"` (last parameter), filter/insert on `airline.upper()`, and change the conflict target to `(airline, origin, destination)`. `add_events`: store `getattr(e, "airline", None)` in the new column (add it to the INSERT column list). `list_events` already selects `*`.

- [ ] **Step 4: `signals.py`** — `Event`: add `airline: str | None = None` as the last field. Delete `SIGNAL_AIRLINES` and its comment. `_label`:

```python
def _label(origin: str, destination: str, airline: str = "XP") -> str:
    place = airport_name(destination) if config.is_home(origin) else airport_name(origin)
    return f"{airline_registry.get(airline).name} {origin}→{destination} ({place})"
```

(`import airlines as airline_registry`). `detect_route`: add `airline: str = "XP"` (last), compute `label = _label(origin, destination, airline)`, and pass `airline=airline` to every `Event(...)` it creates. `run_detection`:

```python
def run_detection(run_id: int, conn: sqlite3.Connection | None = None, allow_sold_out: bool = True) -> list[Event]:
    """Detect signals for every route this run saw, against the previous clean run and the floor of the same airline."""
    airline = database.run_airline(run_id, conn) or "XP"
    all_events: list[Event] = []
    for origin, dest in database.routes_in_run(run_id, conn):
        cur = database.seen_in_run(run_id, origin, dest, conn)
        prev_id = database.previous_run_for_route(run_id, origin, dest, conn)
        prev = database.seen_in_run(prev_id, origin, dest, conn) if prev_id is not None else None
        floor = database.get_floor(origin, dest, conn, airline=airline)
        events, new_floor = detect_route(origin, dest, prev, cur, floor, allow_sold_out=allow_sold_out, airline=airline)
        if new_floor is not None and new_floor != floor:
            database.set_floor(origin, dest, new_floor, conn, airline=airline)
        all_events.extend(events)
    database.add_events(run_id, all_events, conn)
    if all_events:
        log.info("signals: %s", ", ".join(f"{e.kind}×{sum(1 for x in all_events if x.kind == e.kind)}"
                                          for e in {x.kind: x for x in all_events}.values()))
    return all_events
```

Existing signal tests that assert message text starting with `"ILG→TPA"` now start with `"Avelo ILG→TPA"` — update those assertions.

- [ ] **Step 5: Run the whole suite** → all pass.

- [ ] **Step 6: Commit**

```bash
git add database.py signals.py app.py tests/test_database.py tests/test_signals.py tests/test_frontier.py tests/test_run_scraper.py
git commit -m "feat: fare signals and floors are per airline; Frontier runs raise signals"
```

---

### Task 4: Alerts that do not repeat, do not get lost, and know the airline

**Files:**
- Modify: `alerts.py` (`rule_payload`, `rule_query`, evaluation functions, `watched_routes`, `select_pushes`, `build_message`, `deliver`, `evaluate_and_deliver`), `bookings.py` (`evaluate_bookings` returns pending writes), `app.py` (rule save passes airlines and the partner window)
- Test: `tests/test_alerts.py`, `tests/test_bookings.py`

**Interfaces:**
- Consumes: `GetawayQuery.airlines` (Task 1); `Event.airline` (Task 3); `optimizer.intersect_windows`.
- Produces:
  - `rule_payload(..., origins=None, allow_open_jaw=False, airlines: list[str] | None = None, partner: Availability | None = None, count_carry_on: bool = False)` → adds `"airlines"` (sorted codes, default all known), `"partner"` (`None` or `{"start_weekday", "start_time", "end_weekday", "end_time"}`), `"count_carry_on"`.
  - `rule_query`: `airlines` from the payload or `{"XP"}` when the key is absent; windows intersected with the partner's when present.
  - `alerts.StateWrite(kind: str, row_id: int, state: dict, alerting: bool)` (frozen dataclass; `kind` is `"watch"` or `"booking"`); `alerts.apply_writes(writes, delivered: bool, conn=None) -> int`; `alerts.pending_packages(conn=None) -> tuple[list[Event], list[StateWrite]]`; `alerts.pending_rules(conn=None, today=None, max_new=3) -> tuple[list[Event], list[StateWrite]]`; `bookings.pending_bookings(conn=None) -> tuple[list[Event], list[StateWrite]]`. `evaluate_packages`, `evaluate_rules`, `evaluate_bookings` keep their signatures and behave as "evaluate, then apply every write".
  - Watch state gains `"alert_base"`: the total the owner was last told about (or the total when first watched). `price_drop` is measured against it.
  - `watched_routes(conn=None) -> set[tuple[str, str, str]]` — `(airline, origin, destination)`; `select_pushes(events, watched)` matches route signals on `(e.airline or "XP", e.origin, e.destination)`.
  - `build_message(events)` titles name the airlines involved; `Delivery.delivered -> bool` (True when nothing had to be sent, when no channel is configured, or when at least one configured channel succeeded).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_alerts.py`:

```python
def watched(conn, out_price=100, back_price=80):
    out, back = f("ILG", "TPA", 8, 8, out_price), f("TPA", "ILG", 11, 15, back_price)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    wid, _ = alerts.watch_package(it, conn)
    return wid


def reprice(conn, out_price):
    db.upsert_flights([f("ILG", "TPA", 8, 8, out_price)], conn)


def test_gradual_drops_add_up_to_an_alert(conn):
    wid = watched(conn)                                   # total 180, alert_base 180
    seen = []
    for price in (98, 96, 95):                            # totals 178, 176, 175: never $5 in one step
        reprice(conn, price)
        seen.append([e.kind for e in alerts.evaluate_packages(conn)])
    assert seen == [[], [], ["price_drop"]]               # $5 below what the owner last knew: 180 → 175
    state = db.list_watches("package", conn)[0]["state"]
    assert state["alert_base"] == 175 and state["last_total"] == 175
    reprice(conn, 94)
    assert alerts.evaluate_packages(conn) == []           # only $1 below what was announced


def test_legacy_watch_state_gets_a_base_without_alerting(conn):
    wid = watched(conn)
    db.update_watch_state(wid, {"last_total": 180.0}, conn)        # state written before alert_base existed
    assert alerts.evaluate_packages(conn) == []
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 180.0


def test_a_failed_push_is_retried_on_the_next_run(conn, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    sent = []
    outcome = {"ok": False}

    def fake_ntfy(title, body, topic=None, server=None):
        sent.append(title)
        return outcome["ok"]

    monkeypatch.setattr(alerts, "notify_ntfy", fake_ntfy)
    watched(conn)
    reprice(conn, 80)                                               # 180 → 160
    first = alerts.evaluate_and_deliver(None, [], conn)
    state = db.list_watches("package", conn)[0]["state"]
    assert first["ntfy"] is False and state["last_total"] == 180 and state.get("alert_base", 180) == 180   # nothing recorded
    outcome["ok"] = True
    second = alerts.evaluate_and_deliver(None, [], conn)
    assert second["ntfy"] is True and len(sent) == 2
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 160
    third = alerts.evaluate_and_deliver(None, [], conn)
    assert third["pushed"] == 0 and len(sent) == 2                  # announced once, not again


def test_no_push_channel_still_records_the_alert(conn, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    watched(conn)
    reprice(conn, 80)
    alerts.evaluate_and_deliver(None, [], conn)
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 160    # in-app feed is the delivery


def test_rules_carry_airlines_and_the_partner_window(conn):
    mine, partner = Availability(2, time(18), 6, time(23, 59)), Availability(4, time(17), 6, time(20))
    payload = alerts.rule_payload(["TPA"], mine, 1, 300, True, 100, span_days=14, origins=["ILG"],
                                  airlines=["f9", "XP"], partner=partner, count_carry_on=True)
    assert payload["airlines"] == ["F9", "XP"] and payload["partner"]["start_weekday"] == 4 and payload["count_carry_on"]
    q = alerts.rule_query(payload, today=date(2026, 10, 1))
    assert q.airlines == {"XP", "F9"} and q.count_carry_on
    assert q.windows[0].start == datetime(2026, 10, 9, 17) and q.windows[0].end == datetime(2026, 10, 11, 20)   # both free
    old = {k: v for k, v in payload.items() if k not in ("airlines", "partner", "count_carry_on")}
    legacy = alerts.rule_query(old, today=date(2026, 10, 1))
    assert legacy.airlines == {"XP"} and legacy.windows[0].start == datetime(2026, 10, 7, 18)


def test_old_rule_is_not_flooded_by_frontier_packages(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 100), f("TPA", "ILG", 11, 15, 80)], conn)
    old = {k: v for k, v in alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                                span_days=14).items() if k not in ("airlines", "partner", "count_carry_on")}
    db.add_watch("rule", "Tampa", old, conn)
    today = date(2026, 10, 1)
    assert len(alerts.evaluate_rules(conn, today=today)) == 1
    db.upsert_flights([Flight(airline="F9", origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 9), price=40,
                              duration_minutes=150, flight_id="F9-1-PHL-TPA"),
                       Flight(airline="F9", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 16), price=40,
                              duration_minutes=150, flight_id="F9-2-TPA-PHL")], conn)
    assert alerts.evaluate_rules(conn, today=today) == []           # a pre-phase-3 rule stays Avelo-only


def test_route_signals_are_pushed_only_for_the_watched_airline(conn):
    payload = alerts.rule_payload(["MCO"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100, origins=["PHL"], airlines=["F9"])
    db.add_watch("rule", "Frontier Orlando", payload, conn)
    watched_set = alerts.watched_routes(conn)
    assert ("F9", "PHL", "MCO") in watched_set and ("XP", "PHL", "MCO") not in watched_set
    f9_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Frontier PHL→MCO: sale wave", airline="F9")
    xp_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Avelo PHL→MCO: sale wave", airline="XP")
    assert alerts.select_pushes([f9_sale, xp_sale], watched_set) == [f9_sale]
    assert alerts.build_message([f9_sale])[0] == "Frontier sale on a watched route"
    assert alerts.build_message([f9_sale, xp_sale])[0] == "Avelo & Frontier sale on a watched route"
    assert alerts.build_message([Event("booked_drop", None, "TPA", None, "m")])[0] == "A booked trip got cheaper"
```

Append to `tests/test_bookings.py`:

```python
def test_booked_drop_push_that_fails_is_retried(conn, monkeypatch):
    import alerts

    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    results = iter([False, True])
    monkeypatch.setattr(alerts, "notify_ntfy", lambda *a, **k: next(results))
    it = package(conn)
    db.add_booking("trip", bookings.booking_payload(it), 300.0, conn=conn)      # paid well above today's fares
    alerts.evaluate_and_deliver(None, [], conn)
    assert "alerted_total" not in db.list_bookings(conn)[0]["state"]
    alerts.evaluate_and_deliver(None, [], conn)
    assert db.list_bookings(conn)[0]["state"]["alerted_total"] == it.total_price
```

(import `config` in `tests/test_bookings.py` if it is not imported.)

- [ ] **Step 2: Run tests to verify they fail** — expected `TypeError: rule_payload() got an unexpected keyword argument 'airlines'` and `AttributeError: ... 'alert_base'`/`KeyError`.

- [ ] **Step 3: Rules** — in `alerts.py` extend `rule_payload` (new parameters as in Interfaces) so the dict also holds:

```python
        "airlines": sorted({a.strip().upper() for a in (airlines or config.KNOWN_AIRLINES)}),
        "count_carry_on": bool(count_carry_on),
        "partner": None if partner is None else {
            "start_weekday": partner.start_weekday, "start_time": partner.start_time.strftime("%H:%M"),
            "end_weekday": partner.end_weekday, "end_time": partner.end_time.strftime("%H:%M"),
        },
```

In `rule_query`, after computing `windows`:

```python
    p = payload.get("partner")
    if p:
        theirs = Availability(p["start_weekday"], time.fromisoformat(p["start_time"]),
                              p["end_weekday"], time.fromisoformat(p["end_time"]))
        windows = intersect_windows(windows, theirs.windows(today + timedelta(days=1),
                                                            today + timedelta(days=int(payload.get("span_days", 60))),
                                                            holiday_extend=bool(payload.get("holiday_extend", False))))
```

(import `intersect_windows` from `optimizer`), and pass to `GetawayQuery`:

```python
        airlines=set(payload.get("airlines") or ["XP"]),   # rules saved before airlines existed meant Avelo
        count_carry_on=bool(payload.get("count_carry_on", False)),
```

- [ ] **Step 4: Pending state writes** — add to `alerts.py` (after the imports' section):

```python
@dataclass(frozen=True)
class StateWrite:
    """A watch/booking state update waiting for its alert to be delivered."""

    kind: str        # "watch" | "booking"
    row_id: int
    state: dict
    alerting: bool   # True when this state records an alert the owner must actually receive first


def apply_writes(writes: list[StateWrite], delivered: bool, conn: sqlite3.Connection | None = None) -> int:
    """Store the pending states. Alerting ones are skipped when the push failed, so the next run raises
    the same alert again instead of losing it."""
    applied = 0
    for w in writes:
        if w.alerting and not delivered:
            continue
        if w.kind == "watch":
            database.update_watch_state(w.row_id, w.state, conn)
        else:
            database.update_booking_state(w.row_id, w.state, conn)
        applied += 1
    return applied
```

Rewrite `evaluate_packages` as `pending_packages` + a thin wrapper:

```python
def pending_packages(conn: sqlite3.Connection | None = None) -> tuple[list[Event], list[StateWrite]]:
    events: list[Event] = []
    writes: list[StateWrite] = []
    for watch in database.list_watches("package", conn):
        key, state = watch["payload"], dict(watch["state"])
        out_k, in_k = trips.payload_trip(key, "out"), trips.payload_trip(key, "in")
        out, back = package_legs(key, conn)
        if out is None or back is None:
            alerting = not state.get("unavailable")
            if alerting:
                events.append(Event("watch_unavailable", None, key.get("destination"), None,
                                    f"Watched package {watch['label']} is no longer available",
                                    {"watch_id": watch["watch_id"]}))
            writes.append(StateWrite("watch", watch["watch_id"], {**state, "unavailable": True}, alerting))
            continue
        total = round(out.price + back.price, 2)
        base = state.get("alert_base", state.get("last_total"))   # what the owner was last told (or first saw)
        alerting = False
        if base is None:
            base = total
        else:
            drop = base - total
            if drop >= config.ALERT_MIN_DROP_USD or (base and drop / base >= config.ALERT_MIN_DROP_PCT):
                events.append(Event("price_drop", out.origin, out.destination, out.departure_date,
                                    f"Watched: {watch['label']} dropped ${drop:.0f} to ${total:.0f}",
                                    {"watch_id": watch["watch_id"], "from": base, "to": total}, airline=out.airline))
                base, alerting = total, True
            elif total - base >= config.ALERT_MIN_DROP_USD:
                events.append(Event("price_rise", out.origin, out.destination, out.departure_date,
                                    f"Watched: {watch['label']} rose ${total - base:.0f} to ${total:.0f}",
                                    {"watch_id": watch["watch_id"], "from": base, "to": total}, notify=False,
                                    airline=out.airline))
                base = total                                         # later drops are measured from the new level
        upgraded = {**key, "out_trip": trips.upgrade(out_k, out), "in_trip": trips.upgrade(in_k, back)}
        if upgraded != key:
            database.update_watch_payload(watch["watch_id"], upgraded, conn)
        writes.append(StateWrite("watch", watch["watch_id"],
                                 {**state, "last_total": total, "alert_base": base, "unavailable": False,
                                  "checked_at": datetime.now().isoformat(timespec="seconds")}, alerting))
    return events, writes


def evaluate_packages(conn: sqlite3.Connection | None = None) -> list[Event]:
    events, writes = pending_packages(conn)
    apply_writes(writes, True, conn)
    return events
```

Do the same for rules: rename the body of `evaluate_rules` to `pending_rules`, replace its `database.update_watch_state(...)` call with `writes.append(StateWrite("watch", watch["watch_id"], {**state, <the same updates>}, alerting=bool(new_keys)))`, pass `airline=it.outbound.airline` to each `rule_match` event that names an itinerary, return `(events, writes)`, and keep `evaluate_rules(conn, today, max_new)` as the wrapper that applies every write. In `bookings.py` do the same for `evaluate_bookings` → `pending_bookings` (the state that sets `alerted_total` is `alerting=True`; the plain refresh is `alerting=False`); import `StateWrite`/`apply_writes` from `alerts` inside the function, as the module already does for `package_key`.

- [ ] **Step 5: Delivery** — `Delivery`: add

```python
    @property
    def delivered(self) -> bool:
        """Nothing to send, no channel configured (the in-app feed is the delivery), or a channel succeeded."""
        outcomes = [o for o in (self.ntfy_ok, self.email_ok) if o is not None]
        return self.pushed == 0 or not outcomes or any(outcomes)
```

`watched_routes`: return `(airline, origin, destination)` triples — for a rule, every airline in `p.get("airlines") or ["XP"]` × homes × destinations, both directions; for a package, `(tk.airline, tk.origin, tk.destination)` per leg. `select_pushes`: `if (e.airline or "XP", e.origin, e.destination) in watched`. `build_message`:

```python
def build_message(events: list[Event]) -> tuple[str, str]:
    kinds = {e.kind for e in events}
    codes = sorted({e.airline for e in events if e.airline})
    who = " & ".join(sorted(airline_registry.get(c).name for c in codes)) or "Fare"
    if "price_drop" in kinds:
        title = f"{who} fare dropped"
    elif "booked_drop" in kinds:
        title = "A booked trip got cheaper"
    elif "rule_match" in kinds:
        title = f"New {who} getaway matches" if codes else "New getaway matches"
    elif "sale_wave" in kinds:
        title = f"{who} sale on a watched route"
    else:
        title = f"{who} fare update" if codes else "Fare update"
    body = "\n".join(f"• {e.message}" for e in events[:12])
    if len(events) > 12:
        body += f"\n… and {len(events) - 12} more"
    return title, body
```

(`import airlines as airline_registry`). `evaluate_and_deliver`:

```python
def evaluate_and_deliver(run_id: int | None, signal_events: list[Event], conn: sqlite3.Connection | None = None) -> dict:
    """Run after signal detection: evaluate watches, push, and only then record what was announced."""
    from bookings import pending_bookings  # local import avoids a cycle

    watch_events: list[Event] = []
    writes: list[StateWrite] = []
    for pending in (pending_packages, pending_rules, pending_bookings):
        events, state_writes = pending(conn)
        watch_events += events
        writes += state_writes
    pushes = select_pushes(signal_events + watch_events, watched_routes(conn))
    delivery = deliver(pushes)
    if delivery.delivered:
        database.add_events(run_id, watch_events, conn)      # the feed shows an alert once, when it was announced
    apply_writes(writes, delivery.delivered, conn)
    summary = {
        "watch_events": len(watch_events),
        "pushed": delivery.pushed,
        "ntfy": delivery.ntfy_ok,
        "email": delivery.email_ok,
        "delivered": delivery.delivered,
    }
    log.info("alerts: %s", json.dumps(summary))
    return summary
```

- [ ] **Step 6: `app.py`** — the rule save passes the new arguments from the sidebar state it already has: `airlines=sorted(config.KNOWN_AIRLINES)`, `partner=<the partner Availability the sidebar returns, or None>`, `count_carry_on=False`. Store the partner availability in `st.session_state.filters["partner"]` where the sidebar builds `filters`.

- [ ] **Step 7: Run the whole suite** → all pass. Existing tests that set `last_total` and expect a drop still pass (`alert_base` falls back to `last_total`). Existing tests asserting push titles ("Avelo fare dropped", "New Avelo getaway matches") keep passing when their events carry `airline="XP"`; update titles in tests whose events have no airline.

- [ ] **Step 8: Commit**

```bash
git add alerts.py bookings.py app.py tests/test_alerts.py tests/test_bookings.py
git commit -m "fix: alerts accumulate gradual drops, survive failed pushes, and know their airline"
```

---

### Task 5: Bookings per airline

**Files:**
- Modify: `bookings.py` (`booking_payload`, `fare_savings`, `plus_roi`, new `program_savings`), `app.py` (nothing new on screen; ROI badge keeps working)
- Test: `tests/test_bookings.py`

**Interfaces:**
- Produces: booking payloads also hold `out_airline`, `in_airline`, `out_member`, `in_member` (the program fares at booking time); `bookings.fare_savings(payload, airline: str | None = None) -> float` (member savings actually obtained, optionally for one airline's legs); `bookings.plus_roi(conn=None)` counts **Avelo legs only** (`"legs"` key added; perks = Avelo legs × `PLUS_VALUE_PER_LEG`); `bookings.program_savings(airline: str, conn=None) -> dict` → `{"airline", "program", "legs", "would_have_saved"}` = over booked legs of that airline paid at the public fare, the sum of `price - member_price`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_bookings.py`:

```python
def mixed_booking(conn):
    out = Flight(airline="F9", origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 9), price=84,
                 base_price=84, member_price=79, duration_minutes=150, flight_id="F9-1-PHL-TPA")
    back = Flight(airline="XP", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 15), price=61,
                  base_price=101, member_price=61, is_member_fare=True, duration_minutes=150, flight_id="AVELO-TPA-PHL")
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))], origins=["PHL"]))
    db.add_booking("mixed", bookings.booking_payload(it), it.total_price, conn=conn)
    return it


def test_plus_roi_counts_only_avelo_legs(conn, monkeypatch):
    monkeypatch.setattr(config, "PLUS_VALUE_PER_LEG", 50.0)
    mixed_booking(conn)
    payload = db.list_bookings(conn)[0]["payload"]
    assert (payload["out_airline"], payload["in_airline"], payload["out_member"]) == ("F9", "XP", 79)
    roi = bookings.plus_roi(conn)
    assert roi["legs"] == 1 and roi["fare_savings"] == 40 and roi["perk_value"] == 50      # the Avelo leg only


def test_program_savings_is_what_discount_den_would_have_saved(conn):
    mixed_booking(conn)
    den = bookings.program_savings("f9", conn)
    assert den == {"airline": "F9", "program": "Discount Den", "legs": 1, "would_have_saved": 5.0}
    assert bookings.program_savings("XP", conn)["would_have_saved"] == 0.0     # already paid the member fare


def test_bookings_saved_before_airlines_count_as_avelo(conn, monkeypatch):
    monkeypatch.setattr(config, "PLUS_VALUE_PER_LEG", 50.0)
    it = package(conn)
    old = {k: v for k, v in bookings.booking_payload(it).items() if not k.endswith(("_airline", "_member"))}
    db.add_booking("old", old, it.total_price, conn=conn)
    assert bookings.plus_roi(conn)["legs"] == 2 and bookings.program_savings("F9", conn)["legs"] == 0
```

- [ ] **Step 2: Run tests to verify they fail** — expected `KeyError: 'out_airline'` / `AttributeError: ... 'program_savings'`.

- [ ] **Step 3: Implement in `bookings.py`** — `booking_payload` also stores:

```python
            "out_airline": it.outbound.airline, "in_airline": it.inbound.airline,
            "out_member": it.outbound.member_price, "in_member": it.inbound.member_price,
```

Replace `fare_savings` and `plus_roi`, and add `program_savings` (`import airlines as airline_registry`):

```python
def _leg_airline(payload: dict, leg: str) -> str:
    """Bookings saved before airlines existed were Avelo."""
    return (payload.get(f"{leg}_airline") or "XP").upper()


def fare_savings(payload: dict, airline: str | None = None) -> float:
    """Member-fare savings obtained on the two legs at booking time (0 if unknown), optionally one airline's legs."""
    total = 0.0
    for leg in ("out", "in"):
        if airline is not None and _leg_airline(payload, leg) != airline.upper():
            continue
        base, price = payload.get(f"{leg}_base"), payload.get(f"{leg}_price")
        if base is not None and price is not None:
            total += max(float(base) - float(price), 0.0)
    return round(total, 2)


def plus_roi(conn: sqlite3.Connection | None = None) -> dict:
    """Avelo PLUS value from booked Avelo legs: fare savings plus the value of the free carry-on and seat."""
    booked = database.list_bookings(conn)
    legs = sum(1 for b in booked for leg in ("out", "in") if _leg_airline(b["payload"], leg) == "XP")
    fare = round(sum(fare_savings(b["payload"], "XP") for b in booked), 2)
    perks = round(legs * config.PLUS_VALUE_PER_LEG, 2)
    total = round(fare + perks, 2)
    cost = config.PLUS_ANNUAL_COST
    return {
        "bookings": len(booked), "legs": legs, "fare_savings": fare, "perk_value": perks, "total_value": total,
        "annual_cost": cost, "paid_off": cost > 0 and total >= cost, "ratio": round(total / cost, 2) if cost > 0 else None,
    }


def program_savings(airline: str, conn: sqlite3.Connection | None = None) -> dict:
    """What an airline's membership program would have saved on booked legs paid at the public fare."""
    code = airline.strip().upper()
    legs, saved = 0, 0.0
    for b in database.list_bookings(conn):
        p = b["payload"]
        for leg in ("out", "in"):
            if _leg_airline(p, leg) != code or f"{leg}_airline" not in p:
                continue
            legs += 1
            price, member = p.get(f"{leg}_price"), p.get(f"{leg}_member")
            if price is not None and member is not None:
                saved += max(float(price) - float(member), 0.0)
    return {"airline": code, "program": airline_registry.get(code).member_program, "legs": legs, "would_have_saved": round(saved, 2)}
```

The existing ROI test expects perks for 2 legs per Avelo booking — unchanged for all-Avelo bookings.

- [ ] **Step 4: Run the whole suite** → all pass.

- [ ] **Step 5: Commit**

```bash
git add bookings.py tests/test_bookings.py
git commit -m "feat: bookings know each leg's airline; Plus ROI is Avelo-only; program savings per airline"
```

---

### Task 6: Hand-off per airline

**Files:**
- Modify: `handoff.py` (`parse_legs`, `handoff_command`, `run`, `main`), `app.py` (the "Open on Avelo" button builds legs with airlines; label "✈️ Open on airline site")
- Test: `tests/test_handoff.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `airlines.get(code).booking_url(origin, destination, day)` (Phase 2: a URL for Frontier, `None` for Avelo).
- Produces: a leg is `(origin, destination, day, airline)`; `--legs` items are `ORG-DST:YYYY-MM-DD[:AIRLINE]` (airline defaults to `XP`, unknown airline → `SystemExit`); `handoff.split_legs(legs) -> tuple[list[tuple[str, str, date]], list[str]]` (Avelo legs for the assisted browser, booking URLs for the rest); `handoff.open_urls(urls, opener=webbrowser.open) -> int`; `run()` opens the URLs first and starts the assisted browser only when there are Avelo legs.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_handoff.py`:

```python
def test_legs_carry_an_airline_and_default_to_avelo():
    legs = handoff.parse_legs("ILG-TPA:2026-10-09,TPA-PHL:2026-10-12:f9")
    assert legs == [("ILG", "TPA", date(2026, 10, 9), "XP"), ("TPA", "PHL", date(2026, 10, 12), "F9")]
    cmd = handoff.handoff_command(legs, promo="save20", python="py")
    assert cmd[cmd.index("--legs") + 1] == "ILG-TPA:2026-10-09:XP,TPA-PHL:2026-10-12:F9"
    assert handoff.parse_legs(cmd[cmd.index("--legs") + 1]) == legs
    with pytest.raises(SystemExit):
        handoff.parse_legs("ILG-TPA:2026-10-09:ZZ")


def test_frontier_legs_become_links_and_never_reach_avelos_form():
    legs = [("ILG", "TPA", date(2026, 10, 9), "XP"), ("TPA", "PHL", date(2026, 10, 12), "F9")]
    avelo, urls = handoff.split_legs(legs)
    assert avelo == [("ILG", "TPA", date(2026, 10, 9))]
    assert urls == ["https://booking.flyfrontier.com/Flight/InternalSelect?o1=TPA&d1=PHL&dd1=Oct%2012%2C%202026&ADT=1&mon=true&promo="]
    opened = []
    assert handoff.open_urls(urls, opener=opened.append) == 1 and opened == urls


def test_all_frontier_handoff_starts_no_browser(monkeypatch):
    def no_browser(*_a, **_k):
        raise AssertionError("the assisted Avelo browser must not start for Frontier-only legs")

    opened = []
    monkeypatch.setattr(handoff, "async_playwright", no_browser)
    monkeypatch.setattr(handoff.webbrowser, "open", lambda url: opened.append(url) or True)
    code = asyncio.run(handoff.run([("PHL", "MCO", date(2026, 11, 12), "F9")], promo=None, hold=0))
    assert code == 0 and len(opened) == 1 and "o1=PHL&d1=MCO" in opened[0]
```

(add `import asyncio` and `import pytest` to `tests/test_handoff.py` if missing.)

- [ ] **Step 2: Run tests to verify they fail** — expected: `parse_legs` returns 3-tuples; `AttributeError: ... 'split_legs'`.

- [ ] **Step 3: Implement in `handoff.py`** (add `import webbrowser`, `import airlines`; update the module docstring: Avelo legs are driven in a visible browser, other airlines' legs open as links in your default browser; it never selects a fare or books):

```python
Leg = tuple[str, str, date, str]  # origin, destination, day, airline code


def parse_legs(spec: str) -> list[Leg]:
    legs = []
    for item in spec.split(","):
        route, _, rest = item.strip().partition(":")
        day, _, airline = rest.partition(":")
        o, _, d = route.upper().partition("-")
        code = (airline or "XP").strip().upper()
        if not (o and d and day):
            raise SystemExit(f"Bad leg '{item}', expected ORG-DST:YYYY-MM-DD[:AIRLINE]")
        if code not in airlines.AIRLINES:
            raise SystemExit(f"Unknown airline in leg '{item}'")
        legs.append((o, d, date.fromisoformat(day), code))
    return legs


def handoff_command(legs: list[Leg], promo: str | None = None, python: str | None = None) -> list[str]:
    cmd = [python or sys.executable, str(config.PROJECT_ROOT / "handoff.py"),
           "--legs", ",".join(f"{o}-{d}:{day.isoformat()}:{code}" for o, d, day, code in legs)]
    if promo:
        cmd += ["--promo", promo.upper()]
    return cmd


def split_legs(legs: list[Leg]) -> tuple[list[tuple[str, str, date]], list[str]]:
    """(legs for the assisted Avelo browser, booking links for airlines whose site takes URL parameters)."""
    assisted, urls = [], []
    for o, d, day, code in legs:
        url = airlines.get(code).booking_url(o, d, day)
        if url is None:
            assisted.append((o, d, day))
        else:
            urls.append(url)
    return assisted, urls


def open_urls(urls: list[str], opener=None) -> int:
    opener = opener or webbrowser.open
    for url in urls:
        opener(url)
        log.info("opened %s", url.split("?")[0])
    return len(urls)
```

`run(legs, promo, hold)`: start with

```python
    assisted, urls = split_legs(legs)
    open_urls(urls)
    if not assisted:
        return 0
```

and loop the existing assisted flow over `assisted` (three-tuples, as before). The promo is applied to Avelo's form only.

- [ ] **Step 4: `app.py`** — where the button builds `legs`, add each leg's airline as the fourth element (`it.outbound.airline`, `it.inbound.airline`); rename the button to `"✈️ Open on airline site"` with help text "Avelo legs open in an assisted browser with the search filled in; Frontier legs open Frontier's results page. Never books." Update the `tests/test_app.py` assertion that looks for `"Open on Avelo"`.

- [ ] **Step 5: Run the whole suite** → all pass.

- [ ] **Step 6: Commit**

```bash
git add handoff.py app.py tests/test_handoff.py tests/test_app.py
git commit -m "feat: hand-off opens each leg on its own airline's site"
```

---

### Task 7: Nightly targets from rules, watches and bookings

**Files:**
- Modify: `run_scraper.py` (`--from-rules`, `targets_from_rules`), `scheduler.py` (`job_command` uses it; `targets_from_rules` there delegates or is removed), `app.py` (install button builds the new command)
- Test: `tests/test_run_scraper.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: rule payloads (`destinations`, `origins`, `airlines`, `span_days`), `trips.payload_trip/parse` for package watches and bookings.
- Produces: `run_scraper.targets_from_rules(conn=None, default_days: int = 60) -> dict` → `{"airlines": [...], "origins": [...], "destinations": [...], "days": int}` (union over rules, watches and bookings; with nothing saved: active airlines, active homes, all Avelo destinations, `default_days`); CLI flag `--from-rules` (when set, `--airlines/--origins/--destinations/--days` come from that dict unless given explicitly); `scheduler.job_command(mode="calendar", promo=None, python=None) -> list[str]` = `[python, run_scraper.py, "--from-rules", "--mode", mode]` (+ promo).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_scraper.py`:

```python
def test_targets_come_from_rules_watches_and_bookings(isolated):
    import alerts
    from optimizer import Availability
    from datetime import time

    database.init_db()
    assert run_scraper.targets_from_rules() == {"airlines": list(config.ACTIVE_AIRLINES), "origins": list(config.HOME_CODES),
                                                "destinations": list(config.DESTINATIONS), "days": 60}
    av = Availability(2, time(18), 6, time(23, 59))
    database.add_watch("rule", "r1", alerts.rule_payload(["TPA"], av, 1, 300, True, 100, span_days=30, origins=["ILG"], airlines=["XP"]))
    database.add_watch("rule", "r2", alerts.rule_payload(["MCO", "DEN"], av, 1, 300, True, 100, span_days=45, origins=["PHL"], airlines=["F9"]))
    database.add_watch("package", "w", {"out_trip": "F9|BWI|LAS|2026-12-01|*", "in_trip": "F9|LAS|BWI|2026-12-05|*"})
    database.add_booking("b", {"out_trip": "XP|ILG|SRQ|2026-11-20|08:00", "in_trip": "XP|SRQ|ILG|2026-11-23|15:00"}, 100)
    t = run_scraper.targets_from_rules()
    assert t["airlines"] == ["F9", "XP"] and t["origins"] == ["BWI", "ILG", "PHL"]
    assert t["destinations"] == ["DEN", "LAS", "MCO", "SRQ", "TPA"] and t["days"] >= 45


def test_from_rules_flag_fills_the_request(isolated, monkeypatch):
    monkeypatch.setattr(run_scraper, "targets_from_rules", lambda **_k: {"airlines": ["F9"], "origins": ["PHL"], "destinations": ["MCO"], "days": 21})
    seen = {}

    def fake_routes_for(airline, args, *rest):
        seen.update(airline=airline, origins=args.origins, destinations=args.destinations, days=args.days)
        return []

    monkeypatch.setattr(run_scraper, "routes_for", fake_routes_for)
    assert run_scraper.main(["--from-rules", "--no-alerts"]) == 0
    assert seen == {"airline": "F9", "origins": "PHL", "destinations": "MCO", "days": 21}
    run_scraper.main(["--from-rules", "--no-alerts", "--days", "7", "--airlines", "F9"])
    assert seen["days"] == 7                                         # an explicit flag wins
```

Replace the `job_command`/`targets_from_rules` assertions in `tests/test_scheduler.py` with:

```python
def test_job_command_reads_targets_at_run_time():
    cmd = scheduler.job_command(mode="calendar", promo="save20", python="py")
    assert cmd == ["py", str(config.PROJECT_ROOT / "run_scraper.py"), "--from-rules", "--mode", "calendar", "--promo", "save20"]
```

- [ ] **Step 2: Run tests to verify they fail** — expected `AttributeError: module 'run_scraper' has no attribute 'targets_from_rules'`.

- [ ] **Step 3: Implement in `run_scraper.py`** (`import trips`):

```python
def targets_from_rules(conn=None, default_days: int = 60) -> dict:
    """What the saved rules, watched packages and booked trips need scraped: decided when the job runs,
    so a rule added today is covered tonight without reinstalling the job."""
    airline_codes: set[str] = set()
    origins: set[str] = set()
    destinations: set[str] = set()
    days = 0
    today = date.today()
    for watch in database.list_watches(conn=conn):
        p = watch["payload"]
        if watch["kind"] == "rule":
            airline_codes.update(p.get("airlines") or ["XP"])
            origins.update(p.get("origins") or [config.PRIMARY_HOME])
            destinations.update(p.get("destinations", []))
            days = max(days, int(p.get("span_days", default_days)))
    payloads = [w["payload"] for w in database.list_watches("package", conn)] + [b["payload"] for b in database.list_bookings(conn)]
    for p in payloads:
        for leg in ("out", "in"):
            key = trips.payload_trip(p, leg)
            if not key:
                continue
            k = trips.parse(key)
            if k.day < today:
                continue
            airline_codes.add(k.airline)
            home, away = (k.origin, k.destination) if config.is_home(k.origin) else (k.destination, k.origin)
            origins.add(home)
            destinations.add(away)
            days = max(days, (k.day - today).days + 1)
    if not (airline_codes and origins and destinations):
        return {"airlines": list(config.ACTIVE_AIRLINES), "origins": list(config.HOME_CODES),
                "destinations": list(config.DESTINATIONS), "days": default_days}
    return {"airlines": sorted(airline_codes), "origins": sorted(origins), "destinations": sorted(destinations),
            "days": max(days, 1)}
```

`build_parser`: `p.add_argument("--from-rules", action="store_true", help="Take airlines, home airports, destinations and days from saved rules, watches and bookings")`. In `_run`, before anything reads the request:

```python
    if args.from_rules:
        targets = targets_from_rules()
        args.airlines = args.airlines or ",".join(targets["airlines"])
        args.origins = args.origins or ",".join(targets["origins"])
        args.destinations = args.destinations or ",".join(targets["destinations"])
        if args.days is None and args.end is None:
            args.days = targets["days"]
        log.info("targets from rules: %s", targets)
```

The test above uses future dates relative to "today"; if a test date is in the past when the suite runs, the implementer makes the test compute dates from `date.today()` instead of literals.

- [ ] **Step 4: `scheduler.py` and `app.py`** — `job_command(mode="calendar", promo=None, python=None)` returns the `--from-rules` command; delete `scheduler.targets_from_rules` and update the dashboard's "Install / update" code to call `scheduler.job_command(mode=..., promo=...)` (the caption that listed destinations and days now reads "targets follow your saved rules, watches and bookings").

- [ ] **Step 5: Run the whole suite** → all pass.

- [ ] **Step 6: Commit**

```bash
git add run_scraper.py scheduler.py app.py tests/test_run_scraper.py tests/test_scheduler.py
git commit -m "feat: the nightly job takes its targets from rules, watches and bookings at run time"
```

---

### Task 8: Airline-aware dashboard readers

**Files:**
- Modify: `database.py` (`selection_stats`, `route_freshness` gain `airlines=`; `route_freshness` returns the airline), `app.py` (pass airlines; freshness table shows the airline; calendar heatmap reads every airline's lowest fare; signals table shows the airline; the Avelo-only comments and gates are removed)
- Test: `tests/test_database.py`, `tests/test_app.py`

**Interfaces:**
- Produces: `database.selection_stats(origins, destinations, start_date, end_date, conn=None, airlines: Iterable[str] | None = None)`; `database.route_freshness(origins, destinations, conn=None, airlines: Iterable[str] | None = None)` → rows grouped by `(airline, origin, destination)` with an `airline` key; `app.load_calendar` no longer passes `airlines=["XP"]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_database.py`:

```python
def test_dashboard_readers_know_the_airline(conn):
    db.upsert_flights([flight(origin="PHL", dest="MCO", day=10, price=100, number="XP1"), f9(price=60.0)], conn)
    fresh = db.route_freshness(["PHL"], ["MCO"], conn=conn)
    assert {(r["airline"], r["origin"], r["destination"], r["fares"]) for r in fresh} == {("XP", "PHL", "MCO", 1), ("F9", "PHL", "MCO", 1)}
    assert [r["airline"] for r in db.route_freshness(["PHL"], ["MCO"], conn=conn, airlines=["f9"])] == ["F9"]
    assert db.selection_stats(["PHL"], ["MCO"], date(2026, 10, 1), date(2026, 10, 31), conn=conn)["flights"] == 2
    assert db.selection_stats(["PHL"], ["MCO"], date(2026, 10, 1), date(2026, 10, 31), conn=conn, airlines=["XP"])["flights"] == 1
```

Append to `tests/test_app.py`:

```python
def test_dashboard_shows_frontier_packages_with_their_airline(mock_db, monkeypatch):
    monkeypatch.setattr(config, "HOME_CODES", ["ILG", "PHL"])
    with database.connection() as conn:
        conn.execute("""
            INSERT INTO avelo_flights (flight_id, origin, destination, departure_datetime, arrival_datetime, price, currency,
                duration_minutes, flight_number, base_price, member_price, is_member_fare, promo_code, airline, timestamp_scraped)
            SELECT 'F9-' || flight_id, CASE origin WHEN 'ILG' THEN 'PHL' ELSE origin END,
                   CASE destination WHEN 'ILG' THEN 'PHL' ELSE destination END, departure_datetime, arrival_datetime,
                   price - 40, currency, duration_minutes, flight_number, price - 40, price - 41, 0, promo_code, 'F9', timestamp_scraped
            FROM avelo_flights WHERE (origin = 'ILG' OR destination = 'ILG') AND arrival_datetime IS NOT NULL
        """)
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, at.exception
    tables = [df.value for df in at.dataframe if "Airlines" in df.value.columns]
    assert tables and (tables[0]["Airlines"] == "Frontier").any()
    frontier_rows = tables[0][tables[0]["Airlines"] == "Frontier"]
    assert (frontier_rows["Program would save"] > 0).all()
    assert any("airline" in df.value.columns and (df.value["airline"] == "F9").any() for df in at.dataframe)   # freshness table
```

- [ ] **Step 2: Run tests to verify they fail** — expected `KeyError: 'airline'` in the freshness rows and no dataframe with an `airline` column.

- [ ] **Step 3: `database.py`** — `selection_stats` and `route_freshness`: add `airlines: Iterable[str] | None = None`; when given, append `AND airline IN (...)` exactly as `query_flights` does (empty list → the function's empty result). `route_freshness` selects `airline`, groups by `airline, origin, destination` and orders `origin NOT IN (homes), destination, origin, airline`.

- [ ] **Step 4: `app.py`** — `load_calendar`: drop `airlines=["XP"]` and its comment (the heatmap shows each route-day's lowest fare over every airline; `route_calendar` already takes the minimum). The freshness expander table keeps every column the reader returns, so the `airline` column appears; put it first. Where the signals table builds its rows from `list_events`, add the event's `airline` (blank when `None`). Remove any remaining "Avelo only until Phase 3" comment: `grep -n "Phase 3" app.py optimizer.py signals.py database.py` must print nothing.

- [ ] **Step 5: Run the whole suite** → all pass.

- [ ] **Step 6: Commit**

```bash
git add database.py app.py tests/test_database.py tests/test_app.py
git commit -m "feat: dashboard tables know the airline; Frontier fares are shown"
```

---

### Task 9: Verify on real data and document

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `.env.example`

- [ ] **Step 1: Migration and behaviour dry run on a copy of the real database**

```bash
cd ~/avelo-finder && mkdir -p .superpowers/probe && rm -f .superpowers/probe/p3-copy.db && sqlite3 "file:data/flights.db?mode=ro" ".backup '.superpowers/probe/p3-copy.db'"
AVELO_DB_PATH=$PWD/.superpowers/probe/p3-copy.db AVELO_OFFLINE=1 .venv/bin/python - <<'EOF'
import database, alerts, signals
conn = database.get_connection()
print("floor columns:", [r["name"] for r in conn.execute("PRAGMA table_info(route_floor)")])
print("floors by airline:", dict(conn.execute("SELECT airline, COUNT(*) FROM route_floor GROUP BY airline").fetchall()))
print("events airline column:", "airline" in {r["name"] for r in conn.execute("PRAGMA table_info(events)")})
print("first evaluation events:", [(e.kind, e.message[:50]) for e in alerts.evaluate_packages(conn) + alerts.evaluate_rules(conn)])
database.get_connection().close()
print("second open ok")
EOF
rm -f .superpowers/probe/p3-copy.db
```

Expected: `floor columns` starts with `airline`; every existing floor is `XP` and the count equals the old count; `events airline column: True`; no `rule_match`/`price_drop` burst on first evaluation; `second open ok`. Paste the output into the report.

- [ ] **Step 2: Live check (controller runs this with the owner — implementers skip it)** — on a copy of the real database: `run_scraper.py --airlines XP,F9 --origins ILG,PHL --destinations TPA,MCO --start <Fri ~4 weeks out> --end <that Mon> --mode full --no-alerts`; then start the dashboard against the copy and confirm mixed packages with the Airlines column, a Frontier-only package's "Program would save", and that "Open on airline site" opens Frontier's results page for a Frontier leg (without booking). Confirm a second scrape raises per-airline signals only.

- [ ] **Step 3: Documentation** — `CLAUDE.md`: update the layout rows for `optimizer.py`, `signals.py`, `alerts.py`, `bookings.py`, `handoff.py`, `run_scraper.py`, `scheduler.py`, `database.py`; replace the convention "Until Phase 3, packages/alerts read XP rows only" with: "Packages read every airline in `GetawayQuery.airlines` (default all); a rule saved without `airlines` is Avelo-only"; add: "Alert state is written only after a push is delivered (`alerts.StateWrite` / `apply_writes`)", "Signals, floors and baselines are per airline", "`run_scraper --from-rules` is what the nightly job runs", "Carry-on fees are estimates in `config.CARRY_ON_FEE`, counted only when a query asks". Update the run commands (`handoff.py --legs ...:F9`, `--from-rules`) and the test count. `README.md`: the same user-facing points, plus the "Booking, tracking and planning" hand-off bullet: rename it to "Open on airline site", say Avelo legs use the assisted browser and Frontier legs open Frontier's results page in the default browser, promo applies to Avelo only, and show the `ORG-DST:DATE[:AIRLINE]` form with an `F9` example (Task 6 already applied this bullet). `.env.example`: `AVELO_CARRY_ON_XP`, `AVELO_CARRY_ON_F9`.

- [ ] **Step 4: Final full suite and commit**

```bash
git add CLAUDE.md README.md .env.example
git commit -m "docs: phase 3 — packages, signals, alerts and hand-off across airlines"
```
