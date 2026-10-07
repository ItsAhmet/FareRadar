# CLAUDE.md — Avelo Finder

Personal getaway finder for Avelo Airlines nonstops from **Wilmington, DE (ILG)**.
Scrapes Avelo PLUS member fares into SQLite, builds mix-and-match weekend
packages that fit a weekly free window, watches fares nightly, pushes alerts,
and hands off to Avelo's site for booking. Single user (the owner is an Avelo
PLUS member). Python 3.13, Streamlit, Playwright, SQLite. Git repo: `main` is the
baseline, feature work goes on a branch (Phase 1 is on `phase1-multi-home`).

## Run / test

The project lives in `~/avelo-finder` — deliberately **outside** the iCloud-synced
Desktop: iCloud "Optimize Mac Storage" evicted ~20k `.venv` files (`ls -lO` shows
`dataless`) and Python hung on import. The only thing on the Desktop is
`~/Desktop/Avelo Finder.command`, which `cd`s here and runs `launch.py`.
`requirements.lock.txt` pins the exact versions the venv was rebuilt from.

```bash
cd ~/avelo-finder
source .venv/bin/activate            # venv already built; playwright chromium installed
pytest -q                            # 281 tests, no network; AppTest boots the dashboard
streamlit run app.py                 # dashboard (nothing scrapes on load)
python launch.py [--detach|--stop]   # start if needed + open browser; ~/Desktop/Avelo Finder.command calls it
python run_scraper.py --destinations TPA,LAL --start 2026-10-01 --end 2026-10-14   # targeted live scrape
python run_scraper.py --mock         # synthetic data; --mock-sale 0.25 simulates a sale
python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full   # Frontier (stored, not yet shown)
python run_scraper.py --airlines XP,F9 --routes ILG-TPA,PHL-MCO   # each airline gets only the routes it could fly
python handoff.py --legs ILG-TPA:2026-10-09,TPA-ILG:2026-10-12   # visible browser, stops at results
```

Settings live in `.env` (git-ignored; `.env.example` documents every key).
`AVELO_NTFY_TOPIC` is set to the owner's private ntfy topic — never print or
change it unless asked. `AVELO_OFFLINE=1` disables all network calls (weather,
geocoding, routing, pushes); tests set `config.OFFLINE` via monkeypatch.
`AVELO_AIRLINES` (default `XP`) picks the airlines a scrape covers when `--airlines` is not given;
`XP,F9` opts in to Frontier.

A root `conftest.py` plus `pytest.ini` make any test that opens the owner's database (`data/flights.db`, or
whatever `AVELO_DB_PATH` names) fail. `pytest.ini` pins pytest's rootdir so the guard loads from any working
directory; keep both files. Two guard self-tests assert that the refused path ends in `flights.db`, so a
scratch `AVELO_DB_PATH` for a test run should too (e.g. `/tmp/x/flights.db`).

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
| `optimizer.py` | `Availability`/`Window`, `GetawayQuery`, `build_itineraries`, rankings (price/fit/sun/door-to-door), `enrich*`, `wildcard` |
| `signals.py` | sale wave / sold out / floor detection between consecutive runs |
| `alerts.py` | watchlist rules + packages, push selection, ntfy + SMTP delivery; evaluated once after all airlines' runs |
| `bookings.py` | booked-trip tracker + Plus ROI meter |
| `planner.py` | season planner (one trip per window under a budget) |
| `holidays.py` | US federal holidays, computed |
| `places.py` | geocode (Open-Meteo) + drive times (OSRM public, haversine fallback), cached in DB |
| `weather.py` | Open-Meteo 16-day forecast, sun score |
| `scheduler.py` | launchd plist install/remove (cron line elsewhere) |
| `handoff.py` | assisted booking: headful browser, per-leg search, never books |
| `netutil.py` | urllib with certifi CA bundle (python.org CPython on macOS has no roots) |
| `run_scraper.py` | CLI (`--airlines`, per-airline `--promo F9=CODE`); one run per airline; `--routes` is split per airline when several are requested; `build_command()` is what the dashboard button runs |
| `mock_data.py` | deterministic synthetic ILG schedule |
| `app.py` | Streamlit dashboard |
| `launch.py` | one-click launcher (venv re-exec, health-check reuse, detach/stop via `data/streamlit.pid`); `~/Desktop/Avelo Finder.command` calls it |
| `conftest.py`, `pytest.ini` | refuse to open the owner's database under pytest; pin the rootdir so that guard always loads |
| `tests/` | pytest; `tests/fixtures/` holds HTML/JSON captured from the live site (Sep 2026) |
| `docs/superpowers/specs/2026-09-07-getaway-features-design.md` | the 12-feature spec; all 3 phases built |

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
  (the nonstop gate probes a few days and records the answer in `route_catalog.nonstop`).
- Fare semantics: `price` = what the owner would pay (program fare if a member of that airline's program, else
  public), `base_price` = public fare, `member_price` = program fare. Frontier fares are stored, not yet shown.
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
- Package/watch identity is the trip key, never the flight_id row.
- Every fare delete is scoped to one airline (an Avelo run never deletes or shadows Frontier rows, and vice versa).
- One scrape run per airline; baselines compare same-airline runs. Alerts are evaluated once, after all
  airlines' runs; the dashboard's "latest run" is each airline's newest run (`database.latest_run_ids`).
- An index on a migrated column is created in `_migrate`, not `SCHEMA` (`SCHEMA` runs first, so on an
  existing database the column does not exist yet).
- Until Phase 3, packages/alerts read `XP` rows only (`optimizer.find_itineraries`).
- A run's `status` (ok/partial/failed) decides whether it can be a signal baseline.
- Total Getaway Cost = member fares only; `MemberBenefits` locks add-ons at $0.
- Long scrapes run as a `Popen` from the dashboard with a log tail, cancel, and timeout.
- Learning/explanatory output style: give brief `★ Insight` blocks when coding.

## State of the data (local, git-ignored)

`data/flights.db` holds real ILG⇄TPA/LAL fares (Oct 2026) plus older HVN rows
from the first version (harmless; purged as they depart). `price_history`
has several observations for ILG→LAL. `scrape_runs` held 15 runs on Oct 6 2026;
runs from before the `status` column have NULL status (finished ones still count
as `ok` baselines, the two that never finished do not). The owner has
subscribed to the ntfy topic and was asked to save a rule, watch packages, and
install the nightly job from the sidebar.

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
