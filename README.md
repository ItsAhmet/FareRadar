# Avelo Finder — Flexible Getaways from ILG

Personal getaway finder for nonstop flights out of **Wilmington, DE (ILG)**,
**Philadelphia (PHL)** and **Baltimore (BWI)** on two airlines: **Avelo Airlines**
and **Frontier**. It scrapes both airlines' fares into SQLite, prices each
fare at what *you* pay (Avelo PLUS member fare, Frontier Discount Den fare only
if you tell it you are a member), then builds **weekend packages** that fit a
recurring free window such as "Wednesday 6 pm to Sunday midnight". Packages mix
airports (fly into TPA and home from LAL, or into CHS and home from SAV) and
airlines (out on one, back on the other), and can return to a different home
airport if you allow it.

Avelo's 16 nonstop cities from ILG:

| Region | Airports |
|---|---|
| Florida | LAL, SRQ, TPA, MCO, RSW, DAB, FLL, PBI |
| Carolinas & Georgia | MYR, ILM, SAV, CHS, GSP, RDU |
| Tennessee | BNA |
| Caribbean | SJU |

Frontier's destinations come from the same fixed airport list in `config.py`; which of
them are offered from PHL and BWI follows Frontier's public route pages once a scrape
has read them (airports not in the list are ignored).

```
avelo-finder/
├── app.py            Streamlit dashboard (system map, filters, packages)
├── views.py          pure dashboard logic: served destinations, scrape plan, airline comparison, map arcs, program meter
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
pytest -q                          # 461 tests, no network needed
```

The project lives in `~/avelo-finder`, outside iCloud-synced folders. To start
the dashboard with one click, use `~/Desktop/Avelo Finder.command`.

## Filter-before-scrape

Nothing is scraped when the dashboard loads. Pick home airports and airlines,
destinations (region checkboxes or **Select all routes**), your weekly free window,
and a date span, then press **Execute Target Scrape**. The button builds a
`run_scraper.py` command from exactly those inputs, e.g.

```bash
python run_scraper.py --airlines XP --origins ILG --destinations TPA,LAL --start 2026-10-01 --end 2026-10-14 --mode calendar --purge
```

and the scraper visits only ILG⇄TPA and ILG⇄LAL on days Avelo actually flies
them (it captures Avelo's own schedule feed first and intersects). Routes with
no flight in the span are reported as skipped. The caption under the button
states what will run, per airline ("Avelo 8 · Frontier up to 4 (route list not
fetched yet)"): Frontier's route count is an estimate ("up to" or "at least")
until its route list for the selected home airports has been fetched.

### Frontier

Frontier Airlines is scraped alongside Avelo by default (`AVELO_AIRLINES=XP,F9`), and the dashboard's **Airlines** filter lists both. To scrape Avelo only, pass `--airlines XP` or set `AVELO_AIRLINES=XP` in `.env`:

```bash
python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full
python run_scraper.py --airlines XP,F9 --routes ILG-TPA,PHL-MCO   # each airline gets only the routes it could fly
AVELO_AIRLINES=XP,F9                                              # in .env: the default when --airlines is omitted (XP alone opts Frontier out)
```

The nightly job (`run_scraper.py --from-rules`) takes its targets from saved rules, watches and bookings, but only
for airlines listed in `AVELO_AIRLINES`: with the default `XP,F9` a rule that allows Frontier scrapes it; set `AVELO_AIRLINES=XP` to keep the nightly job Avelo-only.

Frontier fares are shown next to Avelo's: packages mix airlines (fly out on one, back on the other) with each leg
labelled, the fare calendar and the freshness table read both, and every airline's runs raise their own signals, floors and
alerts (a Frontier sale never moves Avelo's floor or raises an "Avelo sale"). A rule saved before Frontier existed stays
Avelo-only. Only nonstop Frontier flights are kept. Each airline gets its own scrape run, and signals compare like with
like only: a Frontier calendar price (the day's lowest Discount Den total of any routing, often a connection) is compared
with earlier calendar prices, a day search's nonstop fare with earlier day searches, never one with the other. So a
`--mode full` run followed by the nightly calendar run raises nothing while fares stand still. A route floor is the lowest
fare you could simply book: Frontier calendar prices, promo-code runs and `--mock` runs never set or test it, and a
promo or mock run is a baseline only for a later run of the same kind (same code, or mock). A rule match or watched
package priced from a Frontier calendar price is pushed as "from $X (unverified Frontier calendar price …)" until a
detail or full run confirms the nonstop's times and fare. Carry-on bags are estimated per leg (`AVELO_CARRY_ON_XP`, `AVELO_CARRY_ON_F9`) and counted only when a query asks;
Avelo PLUS perks apply to Avelo legs only, and a package shows what a membership would save on legs you pay the public
fare on.

Known limits: Frontier's public route page lists only about 20 of ~30 destinations per home airport, so for a route that
is not listed pass it with `--routes`. Carry-on fees are estimates, not scraped. Frontier's bot-challenge page has never been
seen live, so a blocked run would show as a plain scrape error. A Frontier route floor is set only by detail or full runs,
never by the calendar-only nightly job.

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
0 3 * * * cd /path/to/avelo-finder && .venv/bin/python run_scraper.py --from-rules >> data/scrape.log 2>&1
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
(or "not selectable"). Comparing consecutive runs of a route, per airline, yields signals:

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
title). Watches and booked trips compare on fares only (a carry-on estimate never counts as a price change). A membership change
raises no package-watch or route-signal alert (baselines are reset), but a booked trip can still alert, and a rule
with a price cap may announce packages the new price basis brings under the cap, because repricing makes them cheaper. Route signals are pushed only for the airline you watch, and their titles name it.

**Delivery.** Set `AVELO_NTFY_TOPIC` in `.env` to an unguessable topic name
and subscribe to it in the ntfy app (iOS/Android/desktop). Optional email via
`AVELO_SMTP_*`. `run_scraper.py --no-alerts` skips detection and pushes. A watch or booking alert is recorded only after its push is
delivered, so if ntfy is down the same drop is announced on the next run; with no push channel (or `AVELO_OFFLINE=1`)
the in-app feed is the delivery.

**Nightly job.** The *⏰ Nightly scrape* sidebar panel installs a launchd
agent (macOS) that runs `run_scraper.py --from-rules` at the chosen time; the
targets (destinations, origins, spans, airlines) are read from your saved rules,
watches and bookings when it runs, capped by `AVELO_AIRLINES`. Stored airport codes
the scraper does not know are skipped with a warning. On other systems it prints the cron
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

* **✈️ Open on airline site** (Trend & weather tab): Avelo legs open in a
  *visible* assisted browser that searches each leg with your promo applied
  and stops on the results page. Frontier legs open Frontier's results page as
  a link in your default browser (the promo applies to Avelo only). It never
  selects a fare or books; you finish by hand. The assisted browser runs
  detached, so closing the dashboard doesn't close it.
  CLI: `python handoff.py --legs ILG-TPA:2026-10-09,LAL-ILG:2026-10-12`; each
  leg is `ORG-DST:DATE[:AIRLINE]` and the airline defaults to `XP` (Avelo), so
  a Frontier leg is `PHL-MCO:2026-10-09:F9`.
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

* **System map** at the top: every selected home airport as a hub, with one arc per
  airline that flies each route to the destinations on offer (coloured by airline),
  destination dots coloured by region; unselected airports stay grey. It redraws as you
  change the selection.
* **Sidebar, "From & with"**: **Home airports** (ILG, PHL, BWI; all selected by default),
  **Airlines** (Avelo and Frontier; both by default), **Return to a different home
  airport** (open-jaw: off means every trip comes back to the airport it left from, where
  your car is; it needs two home airports), and **Count a carry-on bag** (adds each
  airline's estimated carry-on fee per leg; off by default). The destination list below
  offers only airports that at least one selected airline serves from at least one
  selected home airport, and drops a selected destination the new choice no longer serves.
  With no airline or no home airport selected the page says so instead of showing an empty
  table.
* **Sidebar, the rest**: region checkboxes and Select-all, airport fine-tuning, the
  weekly free window (weekday + time for "free from" and "back by"), the date
  span to search, minimum nights, budget, mix-and-match toggle with a maximum
  airport-to-airport distance, ranking mode, and the scraper controls.
* **⚙️ Settings** (sidebar expander): tick **Avelo PLUS member** and **Discount Den
  member** to say which programs you belong to, and set parking $/day and the drive
  from home (minutes) for each home airport. **Save settings** re-prices every stored
  fare to what you would pay (no package-watch or route-signal alert is raised by the change; a booked trip or a rule with a price cap can still alert) and
  refreshes the page; parking and drive feed the "home side" cost and door-to-door ranking.
* **Header badge**: *Avelo Plus Pricing Active (Free Carry-On & Seat
  Selection Applied)*. Total Getaway Cost is what you pay per leg
  (member fare where a membership applies, public fare otherwise, e.g. Frontier while you are not a member) plus
  add-ons; Avelo PLUS perks ($0 add-ons) apply to Avelo legs only, and carry-on is added only when **Count a carry-on
  bag** is ticked (or a saved rule asks). While you are not a Discount Den member a **💡 Discount Den would save:** pill
  shows what membership would have saved on your booked Frontier legs and on the best package listed now.
* **Main panel**: coverage line, a per-route **last scraped** table with age
  (both, like the fare calendar, count only the airlines selected in the sidebar),
  metrics, a **Wildcard weekend** button (a random package under budget,
  weighted towards cheap ones), the ranked package table (with **Deal vs
  Typical** against the route medians and a **Trend** arrow versus the
  previous scrape), an **Airline vs airline** tab (for each free window and destination, the best
  Avelo-only and Frontier-only package side by side, who is cheaper and by how much; with data
  from one airline only it says so; a Frontier figure that is still a calendar price, not a fare a day
  search confirmed, is marked **Unverified**, and so is the verdict when Frontier is the cheaper side), a **Trend & weather** tab (`st.line_chart` of each leg and
  the total across scraper runs, plus an Open-Meteo outlook for the
  destination on the trip dates), the fare calendar (one row per airline and route, lowest fare per
  day) and the timeline. The push topic is never shown on screen; the sidebar only says whether
  push alerts are on.

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
`route_floor` (one floor per airline and route), `events` (signals and alerts, with read state and airline), `watchlist`,
`places` and `drive_times` (place-mode cache), `bookings`, `route_catalog` (which
airline flies which route, with a nonstop flag).

`price_history` is append-only: every upsert also records
`(flight_id, departure_datetime, price, base_price, promo_code, scraped_at)`.
It is seeded from existing rows the first time an older database is opened
and feeds the trend chart and the deal score.
