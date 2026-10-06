# Avelo Finder: getaway feature expansion — design

Date: 2026-09-07 · Status: all three phases built (2026-09-08)

## Goal

Turn the ILG getaway finder from "search what's in the database" into a tool
that watches fares for you, understands your real calendar and destination,
and gets you to checkout. Twelve features, built in three phases so each phase
ships something usable on its own.

## Constraints and principles

* No paid services. Notifications via ntfy.sh (topic only) with SMTP as an
  option; geocoding via Open-Meteo's keyless geocoder; drive times via the
  public OSRM demo server with a haversine fallback when it is unreachable.
* The scraper stays filter-first: no feature adds untargeted requests.
* Every feature has a pure core (testable without browser or network) and a
  thin Streamlit layer.
* The database keeps migrating in place; nothing requires a rebuild.

---

## Phase 1 — Signals and alerts (the tool starts watching for you)

### 1a. Availability snapshots and event detection
**Why:** sold-out and sale detection need to know what each scrape *saw*, not
just the latest price.

* New table `scrape_runs(run_id, started_at, finished_at, mode, routes, promo)`.
* New table `route_day_seen(run_id, origin, destination, day, price, bucket)`:
  one row per priced day per run for calendar scrapes.
* `events` table: `(event_id, run_id, kind, origin, destination, day, detail, created_at)`.
  Kinds: `price_drop`, `sale_wave`, `sold_out`, `floor_fare`, `floor_gone`, `rule_match`.
* Detection (pure functions in `signals.py`, run at the end of every scrape):
  * `sale_wave`: on a route, ≥ 40% of scraped days (min 4) dropped ≥ 15% vs the
    previous run.
  * `sold_out`: a route-day priced in the previous run is absent (not selectable)
    in this run while still in the requested range and in the schedule feed.
  * `floor_fare` / `floor_gone`: per route, the lowest bucket ever seen
    (`route_floor` table, updated each run). A day at the floor is flagged;
    a day leaving the floor bucket raises `floor_gone`.

### 1b. Watchlist, rules and notifications
* `watchlist` table: saved packages `(outbound key, inbound key, window)` and
  rules `(destinations, max_total, min_nights, regions, label)`.
* After detection, `alerts.py` evaluates: watched package total fell ≥ $5 or
  ≥ 5%; a rule matched a package that didn't match last run; any
  `sale_wave`/`sold_out`/`floor_gone` on a watched route.
* Delivery: `ntfy.sh/<topic>` POST (config `AVELO_NTFY_TOPIC`), optional SMTP
  (`AVELO_SMTP_*`), always the in-app **Alerts** feed (unread badge in the header).
* UI: "★ Watch" button on any package; "Save as rule" from the current
  sidebar filters; Alerts tab with mark-read.

### 1c. Nightly scheduler
* `scheduler.py`: writes/removes a launchd plist on macOS
  (`~/Library/LaunchAgents/com.avelo-finder.scrape.plist`) or prints the cron
  line elsewhere. The job runs `run_scraper.py` with the *saved rules'* union
  of destinations and span, so unattended runs stay targeted.
* UI: sidebar expander with time picker, install/remove, last-run status read
  from `scrape_runs`.

**Phase 1 tests:** detection functions on synthetic run pairs; rule matching;
ntfy payload builder (no network); plist rendering.

---

## Phase 2 — Smarter windows and ranking

### 2a. Holiday long-weekend finder
* `holidays.py`: US federal holidays computed (no dependency): New Year's,
  MLK, Presidents', Memorial, Juneteenth, Independence, Labor, Columbus,
  Veterans, Thanksgiving, Christmas, with observed-day rules.
* `Availability.windows()` gains `holiday_extend=True`: a Monday holiday
  extends the preceding window's end to Monday 23:59; a Friday holiday pulls
  the start to Thursday evening. Windows carry a `holiday` label.
* UI: checkbox "Extend around holidays"; window label shows "🎉 Columbus Day".

### 2b. This-weekend mode
* Sidebar button that sets span = today … today+5, all destinations,
  calendar depth, and triggers the scrape; results view sorted by price.

### 2c. Co-scheduling
* Second `Availability` ("Travel partner") in the sidebar, off by default.
  `Availability.intersect(other)` yields the overlap per week; windows are the
  intersection. Empty intersection shows a clear message.

### 2d. Sun-chaser ranking
* `weather.score(rows)`: mean of (high °F normalised 60–90) × (1 − rain%),
  only inside the 16-day horizon; otherwise None.
* New "Rank by" option **Sunniest under budget**: packages with a score sort
  by score then price; unscored packages fall to the bottom with a note.
  Table gains "Sun score" and "Δ°F vs ILG".

### 2e. Place-based mode
* `places.py`: geocode free text via Open-Meteo geocoding
  (`geocoding-api.open-meteo.com/v1/search`), cached; drive time from each
  candidate airport via OSRM demo (`router.project-osrm.org/route/v1/driving`)
  with haversine × 1.25 / 45 mph fallback. Results cached in a `places` table.
* Sidebar: "Where are you actually going?" text input. When set, the optimizer
  adds `drive_minutes_out` (arrival airport → place) and `drive_minutes_back`
  (place → departure airport); new ranking **Door-to-door** = total price +
  (drive hours × a user-set $/hour, default $15) and a "Drive" column.
  Airports farther than the pairing limit from the place are excluded.

**Phase 2 tests:** holiday dates for 2026/2027 incl. observed rules; window
extension; intersection edge cases; sun score bounds; geocode/OSRM parsers on
fixtures; door-to-door ordering with fallback drive times.

---

## Phase 3 — Booking, tracking and planning

### 3a. Assisted booking handoff
* `handoff.py`: launches a **headful** Playwright session (same stealth
  profile, cookies), fills origin/destination/date (and promo) for a leg, and
  stops on the results page. One tab per leg. The process is detached from
  Streamlit like the scraper (Popen), so closing the dashboard doesn't close
  the browser.
* UI: "Open on Avelo" button per package (in the inspector), with a note that
  it stops at results and never books.

### 3b. Booked-trip tracker and Plus ROI
* `bookings` table: package keys, price paid, booked_at, notes.
* After each scrape, compare current total to price paid → "beat the market by
  $X" / "dropped by $Y since booking". Alerts on drops ≥ $10.
* ROI meter: Σ(base_price − price) over booked legs + $ value of free
  carry-on/seat per leg (config `PLUS_VALUE_PER_LEG`, default $50) vs
  membership cost (`PLUS_ANNUAL_COST`, user-set). Shown in the header next to
  the badge.

### 3c. Season planner
* `planner.py`: given packages across windows, a season budget, and
  preferences (max trips, no destination repeats, weight for sun/fit), choose
  one package per window maximising trips then the chosen score under budget.
  Greedy by value density with a small exhaustive search when ≤ 8 windows.
* UI: "Plan my season" tab with budget slider, resulting itinerary list,
  totals and a timeline; "Watch all" button to add each to the watchlist.

**Phase 3 tests:** handoff command builder; booking comparison math; ROI
arithmetic; planner on synthetic packages (budget binding, no repeats,
exhaustive vs greedy agreement on small cases).

---

## Data model summary (new tables)

`scrape_runs`, `route_day_seen`, `route_floor`, `events`, `watchlist`,
`alerts_read`, `places`, `bookings`. All created by `SCHEMA` and safe to add
to existing files.

## Out of scope (for now)

Natural-language query, promo sniffer, shareable PNG cards, event feeds,
real-time push from the dashboard itself. Revisit after Phase 3.

## Sequencing rationale

Phase 1 first because alerts and the scheduler make every later feature
compound: history grows nightly, and the fare-floor and sale detectors need
consecutive runs to say anything. Phase 2 changes what a "good weekend" means
and is pure logic over data Phase 1 accumulates. Phase 3 touches the booking
flow and needs the most live verification, so it goes last.
