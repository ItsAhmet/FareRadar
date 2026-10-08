# CLAUDE.md — Avelo Finder

Personal getaway finder for Avelo Airlines and Frontier nonstops from **Wilmington, DE (ILG)**, Philadelphia (PHL)
and Baltimore (BWI). Scrapes Avelo PLUS member fares and Frontier fares into SQLite, builds mix-and-match weekend
packages that fit a weekly free window, watches fares nightly, pushes alerts,
and hands off to Avelo's site for booking. Single user (the owner is an Avelo
PLUS member). Python 3.13, Streamlit, Playwright, SQLite. Git repo: `main` is the
baseline, feature work goes on a branch (Phase 4 is on `phase4-dashboard`). All four phases of the
multi-airline spec (`docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md`) are built.

## Run / test

The project lives in `~/avelo-finder` — deliberately **outside** the iCloud-synced
Desktop: iCloud "Optimize Mac Storage" evicted ~20k `.venv` files (`ls -lO` shows
`dataless`) and Python hung on import. The only thing on the Desktop is
`~/Desktop/Avelo Finder.command`, which `cd`s here and runs `launch.py`.
`requirements.lock.txt` pins the exact versions the venv was rebuilt from.

```bash
cd ~/avelo-finder
source .venv/bin/activate            # venv already built; playwright chromium installed
pytest -q                            # tests, no network; AppTest boots the dashboard (461 passed)
streamlit run app.py                 # dashboard (nothing scrapes on load)
python launch.py [--detach|--stop]   # start if needed + open browser; ~/Desktop/Avelo Finder.command calls it
python run_scraper.py --destinations TPA,LAL --start 2026-10-01 --end 2026-10-14   # targeted live scrape
python run_scraper.py --mock         # synthetic data; --mock-sale 0.25 simulates a sale
python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full   # Frontier (shown in packages, signals and alerts)
python run_scraper.py --from-rules   # what the nightly job runs: targets come from saved rules, watches and bookings
python run_scraper.py --airlines XP,F9 --routes ILG-TPA,PHL-MCO   # each airline gets only the routes it could fly
python handoff.py --legs ILG-TPA:2026-10-09,TPA-ILG:2026-10-12   # visible browser, stops at results (airline defaults to XP)
python handoff.py --legs PHL-MCO:2026-10-09:F9,MCO-PHL:2026-10-12:F9   # Frontier legs open its results page in the default browser
```

Settings live in `.env` (git-ignored; `.env.example` documents every key).
`AVELO_NTFY_TOPIC` is set to the owner's private ntfy topic — never print or
change it unless asked. `AVELO_OFFLINE=1` disables all network calls (weather,
geocoding, routing, pushes); tests set `config.OFFLINE` via monkeypatch.
`AVELO_AIRLINES` (default `XP,F9`) picks the airlines a scrape covers when `--airlines` is not given;
`XP` alone opts out of Frontier. `--from-rules` (the nightly job) is capped by it too: rules and watches never add an
airline it leaves out, and stored airport codes the scraper does not know are skipped with a warning. So Frontier
rules, watches and bookings are refreshed nightly unless the owner sets `AVELO_AIRLINES=XP`.
`AVELO_CARRY_ON_XP` / `AVELO_CARRY_ON_F9` (default 0 / 55) are the carry-on fee estimates in `config.CARRY_ON_FEE`.

A root `conftest.py` plus `pytest.ini` make any test that opens the owner's database (`data/flights.db`, or
whatever `AVELO_DB_PATH` names) fail. `pytest.ini` pins pytest's rootdir so the guard loads from any working
directory; keep both files. A scratch `AVELO_DB_PATH` for a test run may have any name.

## Layout (flat modules, all importable from the project root)

| File | Role |
|---|---|
| `config.py` | home airports (`HOME_CODES` from `AVELO_HOME_AIRPORTS`, `PRIMARY_HOME` = first), 16 destinations with region + lat/lon, CSS selectors, thresholds, env settings |
| `trips.py` | trip keys: `airline\|origin\|dest\|date\|time-or-*`; watches/rules/bookings store these |
| `persist.py` | writes one search result; deletes fares that vanished from covered days (never today or past days) and fares on days `routes/all` no longer lists (`clear_unscheduled`) |
| `runlock.py` | `data/scrape.lock`, one scrape at a time; `main()` exits 3 when held |
| `airlines.py` | airline registry: codes, membership programs, owner-price rule, Frontier URLs/site codes |
| `browser.py` | `BaseScraper` shared by every airline scraper: stealth context, per-airline cookie file `data/browser_state_<code>.json`, retry, snapshot (.png + .html), `ScrapeReport` |
| `frontier.py` | Frontier parsers over `window.FlightDataJson` + calendar JSON + public route pages; `FrontierScraper`; nonstop gate; `refresh_catalog` |
| `models.py` | `Flight` frozen dataclass (price = member fare, `base_price` = public, `time_known`) |
| `database.py` | SQLite schema + in-place migrations; flights (+ `airline`, `member_price`), price_history (+ `airline`), `route_catalog` (who flies what, with a nonstop flag), memberships (`settings`, default XP yes / F9 no; `price` follows `airlines.owner_price`),  scrape_runs (+ `airline`, one run per airline; + `status` ok/partial/failed; only finished `ok` runs, or legacy NULL-status ones, are signal baselines), route_day_seen, route_floor, events, watchlist, bookings, places, drive_times, settings (JSON key/value; `home_costs` = per-home-airport drive minutes + parking/day, via `set_home_cost`) |
| `scraper.py` | Playwright driver for Avelo's form + **pure parsers** (`parse_schedule`, `parse_lowfare_calendar`, `parse_select_page`, `plan_targets`) |
| `optimizer.py` | `Availability`/`Window`, `GetawayQuery` (`origins`, `airlines`, `allow_open_jaw`, `count_carry_on`), `build_itineraries` (mixed-airline packages, each leg labelled; `Itinerary.airlines`, `program_savings` = what a membership would take off legs the owner pays public fare on), rankings (price/fit/sun/door-to-door), `enrich*`, `wildcard` |
| `signals.py` | sale wave / sold out / floor detection between consecutive runs of the **same airline**; floors, baselines and events are per airline, so a Frontier sale never moves Avelo's floor. Like with like only: per sighting `basis` (`fare` = bookable, `calendar` = Frontier's calendar price), and a promo or mock run is compared only with a run of the same kind; floors come from plain runs' `fare` sightings alone |
| `alerts.py` | watchlist rules + packages, push selection, ntfy + SMTP delivery; alert state is written only after a push is delivered (`StateWrite` / `apply_writes`; with `AVELO_OFFLINE` or no push channel the in-app feed is the delivery); watches compare on fares only (carry-on never counts as a price change); a membership change raises no package-watch or route-signal alert (a booked trip, or a rule with a price cap, can still alert when the new price basis makes it cheaper); route signals are pushed only for the watched airline and titles name it; watches/rules evaluated once after all airlines' runs, an airline's route signals pushed right after its run when another airline follows |
| `bookings.py` | booked-trip tracker (each leg knows its airline; old bookings are Avelo) + Plus ROI meter (Avelo legs only; `program_savings(airline)`) |
| `planner.py` | season planner (one trip per window under a budget) |
| `holidays.py` | US federal holidays, computed |
| `places.py` | geocode (Open-Meteo) + drive times (OSRM public, haversine fallback), cached in DB |
| `weather.py` | Open-Meteo 16-day forecast, sun score |
| `scheduler.py` | launchd plist install/remove (cron line elsewhere); the job runs `run_scraper.py --from-rules`, so targets are read when it runs, not baked into the plist |
| `handoff.py` | assisted booking, never books: legs are `ORG-DST:DATE[:AIRLINE]` (default `XP`); Avelo legs drive the headful browser, Frontier legs open its results page in the default browser (promo is Avelo-only) |
| `netutil.py` | urllib with certifi CA bundle (python.org CPython on macOS has no roots) |
| `run_scraper.py` | CLI (`--airlines`, per-airline `--promo F9=CODE`, `--from-rules` for the nightly job); one run per airline; `--routes` is split per airline when several are requested; `build_command()` is what the dashboard button runs |
| `mock_data.py` | deterministic synthetic ILG schedule |
| `views.py` | pure dashboard view logic, no Streamlit: served destinations per airline and home airport (`served_destinations`, with documented fallbacks while an airline's catalog is empty), labels, scrape plan and its caption (`scrape_estimate`, `describe_plan`: exact, "up to" or "at least"), the mock-run selection, airline comparison, map arcs and view, program meter |
| `app.py` | Streamlit dashboard: wires widgets to `views.py`, `optimizer` and `database`; sidebar "From & with" (home airports, airlines, return to a different home airport, count a carry-on bag), a destination list that follows them, Settings panel (memberships, parking and drive per home airport), 7 tabs including "Airline vs airline", Discount Den meter, system map and fare calendar split by airline |
| `launch.py` | one-click launcher (venv re-exec, health-check reuse, detach/stop via `data/streamlit.pid`); `~/Desktop/Avelo Finder.command` calls it |
| `conftest.py`, `pytest.ini` | refuse to open the owner's database under pytest; pin the rootdir so that guard always loads |
| `tests/` | pytest; `tests/fixtures/` holds HTML/JSON captured from the live site (Sep 2026) |
| `docs/superpowers/specs/2026-09-07-getaway-features-design.md` | the 12-feature spec; all 3 phases built |
| `docs/superpowers/specs/2026-10-06-multi-airline-frontier-design.md` | the multi-airline spec; all 4 phases built (plans in `docs/superpowers/plans/`) |

## How Avelo's site actually works (learned by probing; don't re-derive)

- `https://www.aveloair.com/flight-search/search` is a **Blazor WebAssembly** app.
  URL query parameters are ignored; the fare search is gRPC-web (binary). The
  scraper drives the form with real clicks and reads the rendered DOM.
- On load the page GETs `/flight-search/services/routes/all` (JSON: every
  route and the exact dates it flies). We capture that response
  (`expect_response`) and intersect it with the request in `plan_targets`.
  A bare curl gets 406; it must come from the page context.
- Form controls: `button.location` ×2 (origin/destination; the list is
  rendered twice, click the **visible** one, don't type — typing fires a search
  per keystroke and can stick on "Loading Locations…"), hidden trip-type radios
  (click via JS), `#lowfare-checkbox`, `td.selectable[aria-label$='September 16, 2026']`,
  `button.submit-button`.
- Low Fare Calendar toggle → `/flight-search/lowfare`: `td.day.selectable[aria-label="September, 03 2026"]`
  with `.price.subscription` (Avelo PLUS) and `.price.standard`; month tabs `div.month[id='10-2026']`.
  One page load per route covers months → `calendar` mode. **Today** renders as
  `td.day.today.disabled` with no fare even while today's flights are bookable, so
  today and past days are never treated as an answer (`scraper.answerable_days`).
- Plain search → `/flight-search/select`. **Oct 2026 layout** (fixture `select_page_bundles.html`): one
  `div.main-block` per flight with `.flight-number` ("Flight 533" → `flight_number`), `.flight-block-duration`,
  `.scheduled-time` ×2, then three `button.bundle` cards; only the first ("Basic fare only") holds fares
  (`.price.subscription` / `.price.standard`), the others show `+$61`-style upsells. The Sep 2026 layout
  (`button.flight-block-wrapper-v2`, fixture `select_page.html`) is still parsed. `SELECTORS["flight_card"]`
  matches both and is only for *waiting*; the parser tries layouts in order (the old button contains a
  `div.main-block`, so matching both double-counts). Ids stay `LOWFARE-ORG-DST` / `AVELO-ORG-DST` so price
  history is continuous. When a detail scrape times out on every day, suspect this selector first.
- Promo: "Have a promo code" (`a.link`) opens a popover that renders a **second**
  `#promo` input; the popover leaves a `.popover-background` overlay that
  swallows clicks → `_close_popovers()` after applying, and submit falls back
  to `dispatch_event("click")`. Avelo reports any code as "Applied" at the form
  stage; that does not prove a discount.
- Currently served from ILG (Sep 2026): TPA, RSW, SRQ (from Nov 19) and more;
  PGD/PIE are **not** flown. Unscheduled pairs are skipped, not errors.
- A scheduled day that has **sold out** is still selectable in the date picker. The select
  page then shows "There are no flights available for the selected date. Please select a
  different date." and no cards (seen Sep 2 2026, SRQ→HVN Sep 7). `scrape_day` waits for a card
  *or* that text and raises `DateUnavailable` for the latter; `_pick_date` raises it only when the
  picker rendered other days **of the wanted month**. A picker with no selectable day at all, or one
  not showing the wanted month (no next-month paging yet), is a `ScrapeError` (retry; stored fares kept).
- Stealth: one UA/viewport per run, cookies persisted per airline to `data/browser_state_<code>.json`
  (Avelo is `browser_state_XP.json`; the pre-phase-2 `data/browser_state.json` is adopted once, by
  rename, on the first Avelo run; **cookies only** — persisting localStorage stops the app re-fetching routes/all),
  webdriver masked, trackers/images blocked, human pauses.

## How Frontier's site works (probed 2026-10-06, fixtures in `tests/fixtures/frontier/`)

- `booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov 12, 2026&ADT=1&mon=true` renders a
  server-side page that embeds every flight as JSON in `window.FlightDataJson`: fares for Discount Den and
  standard, local times, stops. Most flights are connections; only `stopCount == 0` single-leg flights count.
- Adding `&c=true` shows a month calendar that POSTs `/Flight/GetLowFareCalendarAvailability`; the response
  holds the lowest **Discount Den** total per day (amount + taxesAndFees). It does not say whether that fare
  is a nonstop, so calendar rows are placeholders (`F9CAL-ORG-DST`, 00:00) that a day search confirms
  (`F9-<number>-ORG-DST`).
- `flights.flyfrontier.com/en/flights-from-<city>` (plain HTTP) lists the destinations it sells from a city.
- Headless works with the shared stealth setup.
- Frontier calls West Palm Beach `DJT`; the app's code is `PBI`. Converted at the scraper boundary only
  (`config.FRONTIER_SITE_CODES`).
- Most flights are connections, and a route with only connections must never yield stored fares
  (the nonstop gate probes one day per weekday, `config.FRONTIER_PROBE_DAYS`, stops at the first nonstop, and
  records the answer in `route_catalog.nonstop`; "no nonstop" is recorded, and the route skipped for 30 days,
  only when every probe answered and all seven weekdays are accounted for: `frontier.no_nonstop_proven`).
- A day-search page must name the requested route and day (`frontier.check_select`); a page for anything else
  is a failed search, never "no nonstop that day".
- Fare semantics: `price` = what the owner would pay (program fare if a member of that airline's program, else
  public), `base_price` = public fare, `member_price` = program fare. Frontier fares are shown in packages, the fare calendar and the freshness table (which lists each airline's
  routes separately; `route_freshness` and `selection_stats` take `airlines=`), and Frontier runs raise signals.
- **Known limitations.** The public route page is paginated: only about 20 of ~30 destinations per home airport
  reach `route_catalog`, so unlisted routes need `--routes` (e.g. `--airlines F9 --routes PHL-MCO`).
  `frontier.is_challenge_page` markers are **unverified**: no real challenge page has been captured, so a
  missed one shows up as a plain `ScrapeError` (nothing deleted) instead of stopping the run as blocked.
  The live Frontier check (plan Task 9 Step 2) is done with the owner; when a real challenge or results page is
  captured, save it under `tests/fixtures/frontier/` and correct `_CHALLENGE_MARKERS`.

## Conventions

- Parsers and detectors are pure functions tested against fixtures; the browser
  driver is thin. Add a fixture from a saved page when markup changes.
- `database.py` functions take an optional `conn`; tests use `":memory:"`.
  Schema changes go in `SCHEMA` (new tables) or `MIGRATIONS` (new columns).
- Calendar rows are stored at 00:00 with `time_known=False`; the optimizer
  matches them by date and flags packages `time_verified=False`. All writes go
  through `persist.py`: a detail result (`save_day`) deletes **every** stored row
  for that day (placeholders and timed rows) before writing; a calendar result
  (`save_calendar`) clears covered days that came back without a fare and drops
  placeholders shadowed by detail rows. A month that rendered without one parsed
  fare is not covered; neither is today or any past day (a `DateUnavailable` for
  today in detail mode is skipped, not cleared). `AveloScraper.run` also deletes
  stored fares on requested future days the schedule no longer lists
  (`persist.clear_unscheduled`, no sighting recorded).
- Streamlit ≥1.63: use `width="stretch"` (not `use_container_width`); give
  every `st.plotly_chart` a unique `key`; widgets driven from callbacks use
  `session_state` keys, not `value=`. `tests/test_app.py` uses `AppTest`.
- Dashboard logic goes in `views.py` as pure functions with unit tests; `app.py` only wires widgets to them.
  Session-state keys (exact): `home_select` (home airport codes), `airline_select` (airline codes), `open_jaw` (bool),
  `count_carry_on` (bool), `dest_select` (destination codes). Supporting keys: `dest_offered` (what the current homes and
  airlines serve; `dest_select` is pruned to it), `dest_kept` (last selection made while something was offered),
  `select_all` / `region_<name>`, `pending_scrape` (the "This weekend" request, with the homes and airlines chosen at click time),
  `member_<code>` / `parking_<code>` / `drive_<code>` (Settings).
- The sidebar defaults reproduce the pre-Phase-4 screen for an owner with only Avelo data: every home airport, every known airline,
  open-jaw off, carry-on off. A destination is offered when at least one selected airline serves it from at least one selected home
  airport (route catalog; `views._fallback_pairs` while an airline's catalog is empty for the selected homes: Frontier from
  the homes with a route page, Avelo from `config.AVELO_FALLBACK_HOMES`, so never Avelo from PHL or BWI). Selecting no airline or no home airport
  shows a message and keeps the destination selection (it is the state passed through when the only selected airline or home is
  swapped); destinations the new choice no longer serves drop out of `dest_select`.
- The scrape button runs exactly what the caption says (`run_scraper.py --airlines ... --origins ...`). The caption counts routes
  per airline: Frontier's count is "up to N" until its route list has been fetched for those homes and "at least N" while a stale list
  is refreshing first; `--mock` runs Avelo from the primary home only, and the caption says so. "This weekend" scrapes the
  destinations the sidebar offers, with the homes and airlines selected when it was clicked.
- Settings (memberships, parking, drive minutes) are saved through `database.set_membership` / `set_home_cost`; saving re-prices
  stored fares (no package-watch or route-signal alert is raised; a booked trip or a rule with a price cap can still alert) and clears Streamlit's cached readers and the search cache. Memberships default to
  Avelo PLUS yes, Frontier Discount Den no. The Discount Den meter (`views.program_meter`) appears while the owner is not a member
  and says what the program would save on booked legs and on the best listed package; each program's meter counts only its own
  airline's legs.
- The private ntfy topic is never rendered; the sidebar only says whether push alerts are on (`tests/test_app.py::_rendered`
  scans every element's protobuf, not a list of element kinds).
- Every cached reader in `app.py` takes `version` (`_db_version()`), never `_version`: Streamlit leaves an underscore
  parameter out of the cache key, and a scrape the page did not launch would then never reach the screen.
- The coverage line, freshness table and fare calendar follow the sidebar's Airlines filter (`airlines=` on the three readers).
- A Frontier calendar price is never shown as a confirmed cost: the Airline vs airline tab has an `Unverified` column and marks
  the verdict (`views.airline_comparison`), the fare calendar has a footnote (`views.calendar_note`); the rule is
  `airlines.unverified_calendar_names`, shared with `alerts.unverified_note`.
- Packages table: the Airlines cell carries a dot in the airline's colour (`Airline.dot`, `views.airline_tag`; the CSV keeps
  plain names) and "Program would save" is drawn only when some row has a saving (`views.hidden_package_columns`).
- Package/watch identity is the trip key, never the flight_id row.
- Every fare delete is scoped to one airline (an Avelo run never deletes or shadows Frontier rows, and vice versa).
- One scrape run per airline; baselines compare same-airline runs. Watches, rules and bookings are evaluated
  once, after all airlines' runs; an airline's route signals are pushed right after its own run when another
  airline follows (its finished run is the next baseline, so a scrape killed later would lose them); the dashboard's "latest run" is each airline's newest run (`database.latest_run_ids`).
- An index on a migrated column is created in `_migrate`, not `SCHEMA` (`SCHEMA` runs first, so on an
  existing database the column does not exist yet).
- Packages read every airline in `GetawayQuery.airlines` (default all known); a rule saved without `airlines` is
  Avelo-only, and one without `origins` means the primary home, so old rules behave as before. The fare calendar
  is split by airline in the dashboard (`database.route_calendar(..., by_airline=True)`: one row per airline, origin, destination,
  day; the default groups across airlines). The packages table's signal badge is keyed by airline too.
- Alert state is written only after a push is delivered (`alerts.StateWrite` / `apply_writes`), so a failed push is
  retried on the next run; with no push channel (or `AVELO_OFFLINE`) the in-app feed is the delivery.
- Signals, floors and baselines are per airline: `route_floor` is keyed `(airline, origin, destination)` and `events`
  carries an airline; `route_floor` rows from before Phase 3 were backfilled `XP`, while `events.airline` is NULL for watch-level
  events and for rows from before the column (readers treat NULL as `XP`; don't filter `WHERE airline='XP'`). Route signals are pushed only for the watched airline.
- `run_scraper --from-rules` is what the nightly job runs; `AVELO_AIRLINES` caps which airlines it scrapes.
- Carry-on fees are estimates in `config.CARRY_ON_FEE`, counted only when a query asks (`count_carry_on`), per leg
  by that leg's airline. Avelo PLUS perks ($0 add-ons) apply to Avelo legs only.
- Known limits: Frontier's public route page lists about 20 of ~30 destinations per home airport (pass others with
  `--routes`); carry-on fees are estimates (`config.CARRY_ON_FEE`); bot-challenge detection has never been seen live
  (see "How Frontier's site works"); Frontier route floors come only from detail/full runs (a calendar-only run never sets
  one). The sidebar has the airline, home airport, open-jaw and carry-on controls and a Settings panel for memberships.
- A run's `status` (ok/partial/failed) decides whether it can be a signal baseline.
- `route_day_seen.basis`: `persist.save_calendar` records `calendar` for an airline whose calendar price is not a
  bookable fare (`Airline.calendar_bookable` False: Frontier, lowest Discount Den total of any routing), everything else
  is `fare`. `signals.run_detection` compares one basis at a time against the previous run holding that basis, and
  `database.previous_run_for_route` only returns a run of the same kind (same promo code; mock with mock). Route floors
  are written and tested only by plain runs' `fare` sightings (never promo, mock or Frontier calendar prices). Opening
  a database from before the column drops Frontier's sightings and floors once (they mixed both bases).
- A rule match or watched-package drop with a Frontier calendar placeholder leg says "from $X (unverified Frontier
  calendar price …)" (`alerts.unverified_note`); the nightly job is calendar-only, so that is what Frontier rules push.
- Total Getaway Cost = the fares the owner pays per leg (member fare where a membership applies, public fare
  otherwise, e.g. Frontier while not a member) plus add-ons, including carry-on when a query counts it; Avelo PLUS
  perks ($0 add-ons) apply to Avelo legs only.
- Long scrapes run as a `Popen` from the dashboard with a log tail, cancel, and timeout.
- Learning/explanatory output style: give brief `★ Insight` blocks when coding.

## State of the data (local, git-ignored)

`data/flights.db` holds real ILG⇄TPA/LAL fares (Oct 2026) plus older HVN rows
from the first version (harmless; purged as they depart). `price_history`
has several observations for ILG→LAL. `scrape_runs` held 18 runs (811 Avelo fares, no watches, rules or bookings yet) on Oct 7 2026;
runs from before the `status` column have NULL status (finished ones still count
as `ok` baselines, the two that never finished do not). The owner has
subscribed to the ntfy topic and was asked to save a rule, watch packages, and
install the nightly job from the sidebar.

## What remains after the four phases

- Frontier's public route page lists only about 20 of ~30 destinations per home airport ("route list page 2" is not fetched),
  so the rest need `--routes`.
- Carry-on fees (`AVELO_CARRY_ON_XP` / `AVELO_CARRY_ON_F9`) are estimates, not scraped.
- `frontier.is_challenge_page` has never matched a real challenge page; a missed one is a plain `ScrapeError`.
- Frontier route floors are set only by detail/full runs; the calendar-only nightly job never sets one.
- A live two-airline pass over the dashboard (scrape both airlines into a copy of the database, then look at the screen) is a
  manual step with the owner; tests never scrape. The AppTest dry run against a copy of the real database passes.

## Ideas not yet built (from the brainstorm)

Email alerts (code exists; needs `AVELO_SMTP_*`), promo-code sniffer from
Avelo's deals page, shareable PNG trip card, plain-English query via Claude,
fare-volatility badge, cheapest-weekday analysis, mix-and-match savings report,
book-by reminders, calendar (.ics) export, travelers count > 1.

## Gotchas

- Never put the project back in an iCloud-synced folder (Desktop/Documents). If it moves, rebuild the venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt && .venv/bin/python -m playwright install chromium` (each Playwright version needs its own browser build).

- Playwright locators: prefer `.filter(visible=True)`; many Avelo elements exist twice (desktop/mobile).
- `AppTest.from_file` needs an absolute path; the first `st.dataframe` on the page is the freshness table, not the packages table.
- Don't mix mock and live data in `data/flights.db`; point `AVELO_DB_PATH` elsewhere for demos.
- The OSRM demo router is rate-limited; results are cached per (place, airport).
