# Avelo Finder — Flexible Getaways from ILG

Personal getaway finder for Avelo Airlines nonstops out of **Wilmington, DE
(ILG)**. It scrapes Avelo's fares (Avelo PLUS member pricing) into SQLite, then
builds **weekend packages** that fit a recurring free window such as
"Wednesday 6 pm to Sunday midnight", mixing airports where it makes sense: fly
into TPA and home from LAL, or into CHS and home from SAV.

Destinations (all 16 nonstop cities Avelo serves from ILG):

| Region | Airports |
|---|---|
| Florida | LAL, SRQ, TPA, MCO, RSW, DAB, FLL, PBI |
| Carolinas & Georgia | MYR, ILM, SAV, CHS, GSP, RDU |
| Tennessee | BNA |
| Caribbean | SJU |

```
avelo-finder/
├── app.py            Streamlit dashboard (system map, filters, packages)
├── scraper.py        Avelo Playwright scraper (+ pure HTML parsers, target planner)
├── frontier.py       Frontier scraper (+ pure parsers, route-catalog refresh)
├── browser.py        BaseScraper shared by every airline scraper (stealth, cookies, retry, snapshots)
├── airlines.py       airline registry: codes, membership programs, owner-price rule
├── optimizer.py      Flexible Getaway engine: windows, mix-and-match, ranking
├── database.py       SQLite schema, migrations, upserts and queries
├── models.py         the shared Flight dataclass
├── config.py         home airports (ILG, PHL, BWI), destinations by region with coordinates, selectors
├── mock_data.py      synthetic schedule for demos and tests
├── run_scraper.py    CLI entry point; the dashboard builds its argv from here
├── requirements.txt
├── .env.example
├── data/             flights.db lives here (git-ignored)
└── tests/            pytest suite, incl. parsers vs. captured Avelo HTML
```

## Setup

Python 3.10+ is required (developed on 3.13).

```bash
cd ~/avelo-finder
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # one-time browser download (~100 MB)
cp .env.example .env               # optional: tweak headless mode, DB path, etc.
pytest -q                          # 285 tests, no network needed
```

The project lives in `~/avelo-finder`, outside iCloud-synced folders. To start
the dashboard with one click, use `~/Desktop/Avelo Finder.command`.

## Filter-before-scrape

Nothing is scraped when the dashboard loads. Pick destinations (region
checkboxes or **Select all routes**), your weekly free window, and a date
span, then press **Execute Target Scrape**. The button builds a
`run_scraper.py` command from exactly those inputs, e.g.

```bash
python run_scraper.py --destinations TPA,LAL --start 2026-10-01 --end 2026-10-14 --mode calendar --purge
```

and the scraper visits only ILG⇄TPA and ILG⇄LAL on days Avelo actually flies
them (it captures Avelo's own schedule feed first and intersects). Routes with
no flight in the span are reported as skipped.

### Frontier (experimental)

Frontier Airlines can be scraped alongside Avelo. It is off by default; opt in per run or in `.env`:

```bash
python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full
python run_scraper.py --airlines XP,F9 --routes ILG-TPA,PHL-MCO   # each airline gets only the routes it could fly
AVELO_AIRLINES=XP,F9                                              # in .env: the default when --airlines is omitted
```

Frontier fares are **stored but not yet shown in the product**: packages, watch rules and the fare calendar read
Avelo rows only until Phase 3, and signal detection (so alerts, pushes and the dashboard's signal feed) skips
Frontier runs. Frontier data does still appear in two dashboard places that are not airline-aware yet: the
freshness table and the "N fares on M routes" selection counts. Only nonstop Frontier flights are kept. Each
airline gets its own scrape run, so baselines compare like with like. Frontier's public route page lists only about 20 of ~30 destinations per home airport, so
for a route that is not listed pass it with `--routes`.

Other useful invocations:

```bash
python run_scraper.py --mock                    # synthetic data, no network
python run_scraper.py                           # every home-airport route, next 60 days
python run_scraper.py --routes ILG-CHS --headful
python run_scraper.py --mode full --days 21     # add departure/arrival times per day
```

| Mode       | What it does                                                        | Cost                    |
|------------|---------------------------------------------------------------------|-------------------------|
| `calendar` | Lowest fare per route per day from the Low Fare Calendar (default)  | ~1 page load per route  |
| `detail`   | Standard search per day: departure/arrival times, duration, fare     | ~1 page load per day    |
| `full`     | `calendar`, then `detail` for every scheduled day                    | both                    |

Calendar rows are stored with a 00:00 departure time and a `LOWFARE-` id; the
optimizer matches them by date and flags the package **Times Verified = no**
until a `detail` run replaces them. Avelo shows no flight numbers anywhere in
its booking flow, so `flight_number` stays empty.

### Promo codes

Type a code into **Active Promo Code (Optional)** in the sidebar (or pass
`--promo CODE` on the CLI). The scraper opens the promo field on Avelo's
search form, types the code and applies it before every search. If Avelo
rejects it the run continues without it and says so in the log; fares scraped
with an accepted code carry it in the `promo_code` column.

### Avelo PLUS pricing

Every fare on Avelo's site shows two prices: the Avelo PLUS member fare and the
public fare. The parsers record the member fare as `price`
(`is_member_fare = 1`) and keep the public fare in `base_price`, so the
dashboard can show the savings. If a cell only shows one price it is stored as
the public fare with `is_member_fare = 0`. Databases created by earlier
versions are migrated in place (columns added on open); rescrape to fill them.

Scheduling: any cron/launchd entry works, e.g. nightly at 03:00:

```
0 3 * * * cd /path/to/avelo-finder && .venv/bin/python run_scraper.py --purge >> data/scrape.log 2>&1
```

### Scraper behaviour, stealth and tuning

* Headless by default; `AVELO_HEADLESS=false` in `.env` or `--headful` shows the window.
* One randomised user-agent and viewport per run (kept across retries so the
  fingerprint stays consistent); cookies (only cookies, not localStorage) persist
  between runs in a per-airline file `data/browser_state_<code>.json` (Avelo is
  `browser_state_XP.json`; the older `data/browser_state.json` is adopted once on
  the first Avelo run) so each visit looks like a returning visitor;
  `navigator.webdriver` and friends are masked; Chromium runs with
  automation-control features disabled.
* Human-like pacing: 2–5 s between page loads (`AVELO_DELAY_MIN` /
  `AVELO_DELAY_MAX`), 0.3–1.4 s between form actions, idle mouse movement and
  the occasional scroll. Trackers, images and fonts are blocked.
* The scrape runs as a background process from the dashboard with a live log,
  a cancel button and a hard timeout (`AVELO_SCRAPE_TIMEOUT_MIN`, default 60).
* Each search retries up to `MAX_RETRIES` with exponential backoff and a fresh
  browser context; a full-page screenshot of each failure lands in `data/screenshots/`.
* All CSS selectors live in `config.SELECTORS`. If a scrape suddenly reports
  "no fares parsed", Avelo changed its markup: run with `--headful -v`, compare
  against the fixtures in `tests/fixtures/`, and update the selectors.

## Watching fares for you

Every scrape is recorded as a run, with the lowest fare seen per route-day
(or "not selectable"). Comparing consecutive runs of a route yields signals:

| Signal | Meaning |
|---|---|
| 🏷 sale wave | ≥ 40% of the comparable days (min 4) dropped ≥ 15% |
| ⛔ sold out | a day priced last run is no longer selectable |
| 🔥 new low | the route hit its lowest fare ever recorded |
| 🔥 floor | a day sits at the route's lowest-ever fare (quiet, in-app only) |
| ⬆ floor gone | a day that was at the floor moved up |

**Watchlist.** In *Trend & weather*, **★ Watch this package** alerts when that
pair's total drops by $5 or 5%, or a leg disappears. In the sidebar, **Save
filters as an alert rule** stores the current destinations, free window,
nights, budget and pairing rules; after each run you're told about packages
that newly match. Route signals are pushed only for routes some watch cares
about; everything lands in the *Watchlist & alerts* tab (unread count in the
title).

**Delivery.** Set `AVELO_NTFY_TOPIC` in `.env` to an unguessable topic name
and subscribe to it in the ntfy app (iOS/Android/desktop). Optional email via
`AVELO_SMTP_*`. `run_scraper.py --no-alerts` skips detection and pushes.

**Nightly job.** The *⏰ Nightly scrape* sidebar panel installs a launchd
agent (macOS) that runs the scraper at the chosen time for the union of your
saved rules' destinations and spans; on other systems it prints the cron
line. Output goes to `data/scheduled.log`.

Demo the signals without the network:

```bash
AVELO_DB_PATH=/tmp/demo.db python run_scraper.py --mock --destinations TPA,LAL --days 30
AVELO_DB_PATH=/tmp/demo.db python run_scraper.py --mock --mock-sale 0.25 --destinations TPA,LAL --days 30
```

## Smarter windows and rankings

* **Holidays.** With *Extend around US holidays* on (default), a federal
  holiday on the day after a window extends it (Columbus Day Monday makes
  Wed→Sun into Wed→Mon) and a holiday on the window's first day pulls the
  start back an evening. Holidays are computed in `holidays.py`, no
  dependency, observed-day rules included. Windows show a 🎉 label.
* **⚡ This weekend.** One click: every destination, the next five days,
  scrape now.
* **Travel partner.** Enter their weekly window; only the overlap is searched.
* **Sunniest under budget.** Inside Open-Meteo's 16-day horizon each package
  gets a sun score (warm and dry over its days) and a Δ°F versus Wilmington;
  the ranking puts scored packages first, sunniest at the top.
* **📍 Place-based mode.** Type where you're really going ("Siesta Key, FL").
  The place is geocoded (Open-Meteo, keyless) and each airport's drive time is
  fetched from the public OSRM router (straight-line estimate if it's down),
  cached in SQLite. Airports beyond your max drive drop out, a *Door-to-door*
  ranking prices the drive at your $/hour, and the table shows drive minutes.

## Booking, tracking and planning

* **✈️ Open on Avelo** (Trend & weather tab): launches a *visible* browser,
  searches each leg of the package with your promo applied, and stops on the
  results page. It never selects a fare or books; you finish by hand. Runs
  detached, so closing the dashboard doesn't close the browser.
  CLI: `python handoff.py --legs ILG-TPA:2026-10-09,LAL-ILG:2026-10-12`.
* **✅ Mark as booked**: record what you paid. After every scrape the trip's
  current fare is compared with it ("you beat the market by $X", or a push
  alert when it is $10+ cheaper and rebooking might pay). Bookings live in
  *Watchlist & alerts → My booked trips*.
* **Plus ROI meter** (header): fare savings on booked legs plus the value of
  the free carry-on and seat selection (`AVELO_PLUS_VALUE_PER_LEG`, default
  $50 per leg) against the membership (`AVELO_PLUS_ANNUAL_COST`, default $99).
* **Season plan** tab: one getaway per free window under a season budget,
  optionally never the same city twice, optimised for cost, window fit or sun.
  Exhaustive search up to 10 windows, greedy beyond. *Watch all planned trips*
  adds them to the watchlist.

## Housekeeping

`database.maintenance()` runs when the dashboard starts and before every
scrape: departed flights are deleted and price history older than
`AVELO_HISTORY_DAYS` (default 120) is pruned.

## Dashboard

```bash
streamlit run app.py
```

* **System map** at the top: your primary home airport as the hub, with arcs to every selected destination,
  coloured by region; unselected airports stay grey. It redraws as you change
  the selection.
* **Sidebar**: region checkboxes and Select-all, airport fine-tuning, the
  weekly free window (weekday + time for "free from" and "back by"), the date
  span to search, minimum nights, budget, mix-and-match toggle with a maximum
  airport-to-airport distance, ranking mode, and the scraper controls.
* **Header badge**: *Avelo Plus Pricing Active (Free Carry-On & Seat
  Selection Applied)*. Add-on fees are locked at $0 in the optimizer, so
  Total Getaway Cost is exactly the two member fares.
* **Main panel**: coverage line, a per-route **last scraped** table with age,
  metrics, a **Wildcard weekend** button (a random package under budget,
  weighted towards cheap ones), the ranked package table (with **Deal vs
  Typical** against the route medians and a **Trend** arrow versus the
  previous scrape), a **Trend & weather** tab (`st.line_chart` of each leg and
  the total across scraper runs, plus an Open-Meteo outlook for the
  destination on the trip dates), the fare calendar and the timeline.

## How the Flexible Getaway optimizer works

`Availability.windows()` expands "Wednesday 18:00 → Sunday 23:59" into every
concrete window inside the search span. For each window,
`build_itineraries` takes ILG departures inside it and pairs them with returns
that leave after the outbound lands and are home before the window closes.

A return may leave from a different airport than the outbound arrived at when
both are in the same region and within `max_pair_miles` (default 100 mi):
TPA↔LAL, TPA↔SRQ, MCO↔DAB, FLL↔PBI, CHS↔SAV, MYR↔ILM qualify; TPA↔FLL and
GSP↔CHS do not. Both tickets are summed into **Total Getaway Cost**.

* **Cheapest package** sorts by total cost, then fit.
* **Best window fit** sorts by fit (share of the free window the trip uses),
  then cost.

Tune `can_pair` or `fit_score` in `optimizer.py` to change the rules.

## Database

`data/flights.db`, table `avelo_flights`:

| column             | notes                                              |
|--------------------|----------------------------------------------------|
| flight_id          | `LOWFARE-ILG-TPA` or `AVELO-ILG-TPA`               |
| origin, destination| IATA codes                                          |
| departure_datetime | ISO text, local time; 00:00 for calendar rows       |
| arrival_datetime   | ISO text or NULL                                    |
| price, currency    | Avelo PLUS member fare when shown, else public, USD |
| base_price         | public fare (NULL if unknown)                       |
| is_member_fare     | 1 when `price` is the Avelo PLUS fare               |
| airline            | `XP` (Avelo) or `F9` (Frontier)                     |
| member_price       | the airline's program fare, when shown              |
| promo_code         | promo applied when the fare was observed, else NULL |
| duration_minutes   | NULL for calendar rows                              |
| flight_number      | always NULL (Avelo doesn't publish it in the flow)  |
| timestamp_scraped  | ISO text                                            |

Primary key is `(flight_id, departure_datetime)`; re-scraping upserts the
price and timestamp, so the table always holds the freshest fare per flight.

New tables: `scrape_runs`, `route_day_seen` (what each run saw),
`route_floor`, `events` (signals and alerts, with read state), `watchlist`,
`places` and `drive_times` (place-mode cache), `bookings`, `route_catalog` (which
airline flies which route, with a nonstop flag).

`price_history` is append-only: every upsert also records
`(flight_id, departure_datetime, price, base_price, promo_code, scraped_at)`.
It is seeded from existing rows the first time an older database is opened
and feeds the trend chart and the deal score.
