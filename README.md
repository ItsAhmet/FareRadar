# FareRadar

FareRadar finds cheap nonstop weekend trips on Avelo Airlines and Frontier from
Wilmington, DE (ILG), Philadelphia (PHL) and Baltimore (BWI), then keeps watching
the fares after you've picked one.

You tell it when you're free each week, say Wednesday 6 pm to Sunday midnight. It
scrapes both airlines' fares into a local SQLite database, prices each one at what
you would pay, and lists the round trips that fit inside that window. A trip can
fly into one airport and come home from a nearby one (into Tampa, back from
Lakeland), and it can go out on one airline and back on the other.

It started as a tool for one Avelo PLUS member in Delaware, and the defaults show
it: three home airports, Avelo PLUS pricing on, Frontier's Discount Den off. You
can change all of that.

FareRadar never books anything. When you pick a trip it opens the airline's site
with the search filled in, and you finish there.

> Not affiliated with Avelo Airlines or Frontier Airlines. See the [disclaimer](#disclaimer).

## What it does

- Scrapes only what you ask for. Pick home airports, airlines, destinations and
  dates, and it visits just those routes on the days the airline actually flies
  them. Nothing runs when the dashboard opens.
- Builds weekend packages that fit your free window, and stretches the window
  around US federal holidays.
- Ranks packages by total price, by how much of the window they use, by the sun
  forecast at the destination, or door to door (drive time and parking at your
  home airport included).
- Puts the two airlines side by side in an **Airline vs airline** tab.
- Watches fares between runs: sale waves, sold-out days, new lows, and price
  drops on packages you starred. Alerts go to your phone through
  [ntfy](https://ntfy.sh) and to a feed inside the app.
- Remembers trips you booked and tells you when the same trip gets $10 or more
  cheaper.
- Plans a season: one trip per free window under a total budget.

## Requirements

- macOS or Linux. The nightly job installs itself with launchd on macOS; anywhere
  else FareRadar prints a cron line for you to add.
- Python 3.10 or newer (developed on 3.13).
- About 100 MB of disk for Playwright's Chromium.

## Install

```bash
git clone https://github.com/ItsAhmet/FareRadar.git ~/FareRadar
cd ~/FareRadar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # requirements.lock.txt pins the exact versions it was tested with
playwright install chromium         # one-time browser download
cp .env.example .env                # every setting is optional
pytest -q                           # 465 tests, no network needed
```

On a Mac, keep the folder out of iCloud-synced places like Desktop and Documents.
With "Optimize Mac Storage" on, iCloud evicts files inside `.venv` and Python
hangs on import.

## First run

Try it on synthetic data first. Point it at a separate database so fake fares
never mix with real ones:

```bash
export AVELO_DB_PATH=data/demo.db
python run_scraper.py --mock --destinations TPA,LAL --days 30                    # fake schedule, no network
python run_scraper.py --mock --mock-sale 0.25 --destinations TPA,LAL --days 30   # a 25% sale, to see the alerts fire
streamlit run app.py
```

For real fares, run `unset AVELO_DB_PATH` (the default is `data/flights.db`) and
start the dashboard:

```bash
python launch.py            # starts the dashboard if it isn't running and opens your browser
python launch.py --detach   # same, in the background
python launch.py --stop     # stops the background server
```

In the sidebar, choose your home airports, airlines and destinations, set the
weekly free window and a date span, then press **Execute Target Scrape**. The
caption under the button tells you exactly what it's about to run.

For a one-click start on macOS, save this as `~/Desktop/FareRadar.command` and
`chmod +x` it. Closing its Terminal window stops the dashboard.

```zsh
#!/bin/zsh
cd ~/FareRadar || exit 1
exec .venv/bin/python launch.py
```

## Settings

Settings live in `.env`. `.env.example` documents every key; these are the ones
you're most likely to touch. (They keep the `AVELO_` prefix from the project's
first name.)

| Setting | Default | What it does |
|---|---|---|
| `AVELO_HOME_AIRPORTS` | `ILG,PHL,BWI` | Airports you fly from. The first one is the primary (weather comparison, `--mock`). |
| `AVELO_AIRLINES` | `XP,F9` | Airlines a scrape covers when `--airlines` isn't given. `XP` is Avelo, `F9` is Frontier; `XP` alone leaves Frontier out, nightly job included. |
| `AVELO_NTFY_TOPIC` | empty | ntfy topic for push alerts. Pick a name nobody could guess and subscribe to it in the ntfy app. |
| `AVELO_SMTP_*` | empty | Optional email alerts. |
| `AVELO_DAYS_AHEAD` | `60` | How far ahead a scrape looks when you don't give dates. |
| `AVELO_HEADLESS` | `true` | Set `false` to watch the browser while it scrapes. |
| `AVELO_DELAY_MIN`, `AVELO_DELAY_MAX` | `2.0`, `5.0` | Seconds to wait between page loads. |
| `AVELO_CARRY_ON_XP`, `AVELO_CARRY_ON_F9` | `0`, `55` | Estimated carry-on fee per leg. Added only when you tick **Count a carry-on bag** or a saved rule asks for it. |
| `AVELO_SCRAPE_TIMEOUT_MIN` | `60` | Minutes before the dashboard stops a scrape it started. A first Frontier scrape of every route can take over an hour. |
| `AVELO_DB_PATH` | `data/flights.db` | Where the database lives. |
| `AVELO_OFFLINE` | off | `1` turns off weather, geocoding, routing and push calls. Handy for demos. |

Memberships, parking and drive times aren't in `.env`. You set them in the
dashboard's **Settings** panel.

## The dashboard

```bash
streamlit run app.py      # or python launch.py
```

The sidebar's "From & with" section has four controls:

- **Home airports**: ILG, PHL and BWI, all selected by default.
- **Airlines**: Avelo and Frontier, both by default.
- **Return to a different home airport**: off means every trip comes back to the
  airport it left from, where your car is. It needs two home airports.
- **Count a carry-on bag**: adds each airline's estimated carry-on fee per leg.

The destination list under them only offers airports that a selected airline
serves from a selected home airport. Below that are region checkboxes, the weekly
free window, the date span, minimum nights, budget, the mix-and-match toggle (with
a maximum distance between the two airports), the ranking, and the scraper
controls. **This weekend** scrapes the next five days for everything on offer in
one click.

In **Settings**, tick the programs you belong to (Avelo PLUS, Frontier Discount
Den) and set parking per day and the drive from home for each home airport.
Saving re-prices every stored fare to what you'd pay. While you're not a Discount
Den member, a small meter shows what membership would have saved you.

The main panel has a system map of every route, a "last scraped" table per route,
the ranked package table (with a deal score against each route's typical price
and a trend arrow against the previous scrape), the **Airline vs airline** tab, a
trend and weather tab, a fare calendar per airline and route, the watchlist, and
the season planner. **Wildcard weekend** picks a random package under your
budget, leaning toward the cheap ones.

Total Getaway Cost is what you pay per leg (the member fare where a membership
applies, the public fare otherwise) plus add-ons. Avelo PLUS perks, a free
carry-on and seat, only apply to Avelo legs.

## Scraping from the command line

The dashboard button builds a `run_scraper.py` command, and you can run the same
thing yourself:

```bash
python run_scraper.py --airlines XP --origins ILG --destinations TPA,LAL --start 2026-10-01 --end 2026-10-14
python run_scraper.py --airlines F9 --origins PHL --destinations MCO --days 21 --mode full
python run_scraper.py --airlines XP,F9 --routes ILG-TPA,PHL-MCO   # each airline only gets routes it could fly
python run_scraper.py --routes ILG-CHS --headful                  # watch the browser
python run_scraper.py --from-rules                                # what the nightly job runs
```

Before it searches anything, the Avelo scraper reads Avelo's own schedule feed and
keeps only the days a route actually flies. Routes with no flight in your span
are reported as skipped.

| Mode | What it gets | Cost |
|---|---|---|
| `calendar` (default) | Lowest fare per route per day from the low-fare calendar | about one page load per route |
| `detail` | A search per day: departure and arrival times, duration, fare | about one page load per day |
| `full` | `calendar`, then `detail` for every scheduled day | both |

Calendar fares are stored at 00:00 and marked **Times Verified = no** in the
package table until a `detail` run fills in the real times.

Other flags: `--promo CODE` applies a promo code before every Avelo search
(`--promo F9=CODE` for Frontier). Avelo says "Applied" to any code at that stage,
so check the fares. `--no-alerts` skips signal detection and pushes, and `-v`
logs more.

### Frontier

Frontier is scraped alongside Avelo by default. Only nonstop flights are kept:
most Frontier results are connections, so before it trusts a new route FareRadar
searches up to one day per weekday and stops at the first nonstop it finds. Frontier's destinations come from the same
fixed airport list in `config.py` as Avelo's; which of them are offered from PHL
and BWI follows Frontier's public route pages once a scrape has read them.

A Frontier calendar price is the day's lowest Discount Den total over any routing,
often a connection, so FareRadar never shows it as a confirmed cost. It's marked
"unverified" on every screen and in alerts until a `detail` or `full` run
confirms the nonstop's fare and times.

## Alerts and the nightly job

Every scrape is saved as a run. Comparing a route's consecutive runs for the same
airline raises signals:

| Signal | Meaning |
|---|---|
| 🏷 sale wave | at least 40% of comparable days (minimum 4) dropped 15% or more |
| ⛔ sold out | a day that had a price last run can no longer be picked |
| 🔥 new low | the route hit its lowest fare ever recorded |
| 🔥 floor | a day sits at the route's lowest-ever fare (in-app only) |
| ⬆ floor gone | a day that was at the floor went up |

Runs are compared like with like. A Frontier calendar price is compared with
earlier calendar prices, a day search with earlier day searches, and a promo-code
or mock run only with a run of the same kind. A route's floor comes only from
plain runs of fares you could book; promo codes, mock data and Frontier calendar
prices never set it.

**Watch this package** (trend and weather tab) alerts when a package drops by $5
or 5%, or loses a leg. **Save filters as an alert rule** (sidebar) stores your
destinations, window, nights, budget and pairing rules, and tells you about
packages that newly match after each run. Watches and booked trips compare fares
only; a carry-on estimate never counts as a price change. A membership change
raises no package-watch or route-signal alert (the baselines reset), but a booked
trip can still alert, and a rule with a price cap can announce packages that the
new pricing brings under the cap.

To get pushes, set `AVELO_NTFY_TOPIC` and subscribe to that topic in the ntfy app
(iOS, Android or desktop). An alert counts as sent only once ntfy accepts it, so
if ntfy is down you hear about the drop on the next run. With no push channel set,
the in-app feed is where alerts land.

The **Nightly scrape** panel in the sidebar installs a launchd job on macOS (on
other systems it prints a cron line) that runs `run_scraper.py --from-rules` at
the time you choose. Its targets come from your saved rules, watches and bookings
each night, limited to the airlines in `AVELO_AIRLINES`. Output goes to
`data/scheduled.log`. A cron entry looks like this:

```
0 3 * * * cd ~/FareRadar && .venv/bin/python run_scraper.py --from-rules >> data/scrape.log 2>&1
```

## Booking, tracking and planning

**Open on airline site** opens a trip for booking. Avelo legs open in a visible
browser that runs each leg's search (with your promo code) and stops on the
results page; Frontier legs open Frontier's results page in your default browser.
It never selects a fare or books. From the command line:

```bash
python handoff.py --legs ILG-TPA:2026-10-09,LAL-ILG:2026-10-12       # airline defaults to XP (Avelo)
python handoff.py --legs PHL-MCO:2026-10-09:F9,MCO-PHL:2026-10-12:F9
```

**Mark as booked** records what you paid. After each scrape FareRadar compares the
trip's current fare with it and pushes an alert when rebooking would save $10 or
more. The header's Plus ROI meter adds up what Avelo PLUS saved you on booked legs,
plus the free carry-on and seat (`AVELO_PLUS_VALUE_PER_LEG`, default $50 a leg),
against the membership fee (`AVELO_PLUS_ANNUAL_COST`, default $99).

The **Season plan** tab picks one getaway per free window under a season budget,
optionally never the same city twice, and can add every planned trip to the
watchlist.

**Place mode** is for when the airport isn't the destination. Type "Siesta Key,
FL" and FareRadar geocodes it (Open-Meteo), gets drive times from each airport
(the public OSRM router, or a straight-line estimate when it's down), drops
airports beyond your maximum drive, and can rank door to door at your $/hour.

## How it works

`Availability.windows()` in `optimizer.py` turns "Wednesday 18:00 to Sunday 23:59"
into every concrete window in your date span. For each window, `build_itineraries`
takes departures inside it and pairs them with returns that leave after the
outbound lands and get home before the window closes. A return may leave from a
different airport than the outbound arrived at when both are in the same region
and within 100 miles: TPA and LAL qualify, TPA and FLL don't. Tune `can_pair` and
`fit_score` to change the rules.

Avelo shows two prices on every fare, the PLUS member fare and the public fare.
FareRadar stores what you'd pay as `price` and the public fare as `base_price`.
Saving your memberships in Settings re-prices everything already stored.

Avelo's booking site is a Blazor WebAssembly app that ignores URL parameters, so
the scraper fills in the search form with real clicks and reads the rendered
page. Frontier embeds its flights as JSON in a server-rendered page. Both
scrapers run headless Chromium through Playwright with one user agent and
viewport per run, cookies kept between runs, images and trackers blocked, and a
few seconds between page loads. Every CSS selector lives in `config.SELECTORS`.
If a scrape suddenly reports "no fares parsed", the airline probably changed its
markup: run with `--headful -v`, compare the page with the saved copies in
`tests/fixtures/`, and update the selectors.

Each search retries with exponential backoff and a fresh browser context, and
saves a screenshot of every failure to `data/screenshots/`. When the dashboard
starts and before every scrape, departed flights are deleted and price history
older than `AVELO_HISTORY_DAYS` (default 120) is pruned.

The database is `data/flights.db`. Fares are in `avelo_flights`, keyed by
`(flight_id, departure_datetime)`; every write also appends to `price_history`,
which feeds the trend chart and the deal score. The other tables hold scrape
runs, what each run saw per route and day, route floors, events and alerts, the
watchlist, bookings, the place-mode cache, and `route_catalog` (which airline flies
which route, and whether nonstop).

## Known limits

- Frontier's public route page lists its 20 cheapest destinations per home
  airport. It flies one to three more, which show up over later weekly refreshes;
  for one that isn't listed yet, pass it with `--routes`.
- Carry-on fees are estimates, not scraped.
- Frontier's bot-challenge page has never been seen live, so a blocked run shows
  up as a plain scrape error.
- A Frontier route floor is set only by `detail` or `full` runs, never by the
  calendar-only nightly job.
- One traveler only.

## Project layout

```
FareRadar/
├── app.py            Streamlit dashboard
├── views.py          dashboard logic without Streamlit (served destinations, scrape plan, airline comparison, map)
├── launch.py         one-click launcher (start, detach, stop)
├── run_scraper.py    command-line scraper; the dashboard builds its command from here
├── scraper.py        Avelo scraper and its HTML parsers
├── frontier.py       Frontier scraper and its parsers
├── browser.py        shared browser setup: stealth, cookies, retries, snapshots
├── airlines.py       airline registry: codes, membership programs, pricing rule
├── optimizer.py      windows, mix-and-match pairing, rankings
├── signals.py        sale, sold-out and floor detection between runs
├── alerts.py         watchlist, rules, ntfy and email delivery
├── bookings.py       booked trips and the Plus ROI meter
├── planner.py        season planner
├── database.py       SQLite schema, migrations and queries
├── persist.py        writes scrape results
├── config.py         home airports, destinations, selectors, settings
├── handoff.py        opens a trip on the airline's site
├── scheduler.py      nightly job (launchd or cron)
├── places.py, weather.py, holidays.py
├── mock_data.py      synthetic schedule for demos and tests
├── data/             database, logs and cookies (git-ignored)
└── tests/            pytest suite, with pages saved from both airlines' sites
```

## Tests

```bash
pytest -q
```

The suite never touches the network, and it refuses to open your real database
(`conftest.py` makes any test that tries fail). Parsers are tested against pages
saved from the live sites in `tests/fixtures/`.

## Disclaimer

FareRadar is a personal project. It is not affiliated with, endorsed by, or
connected to Avelo Airlines or Frontier Airlines. It reads the same public fare
pages a browser does, slowly and only for the routes you ask for, but airline
sites change without notice and their terms may restrict automated access. Use
it for your own trip planning, at your own risk. Fares can go stale fast, so
always check the price on the airline's site before you book.
