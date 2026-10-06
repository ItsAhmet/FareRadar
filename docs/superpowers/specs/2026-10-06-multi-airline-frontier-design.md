# Multi-airline + multi-home-airport (Frontier) — design

Date: 2026-10-06 · Status: approved in conversation, awaiting written-spec review

## 1. Goal

Answer "where can I get away this weekend, and from which airport, on which
airline?" across **Avelo from ILG** and **Frontier from PHL and BWI**, ranked
door-to-door, with the same scrape → package → alert → hand-off loop the app
already has for Avelo.

Success looks like:

- One package list mixing Avelo and Frontier nonstops from ILG, PHL and BWI.
- An "airline vs airline" comparison for the same weekend and destination.
- Frontier fares stored as **both** public and Discount Den, with a meter showing
  what Discount Den would actually save.
- Nightly alerts that cover both airlines and never report a trip as new or sold
  out merely because a calendar row was replaced by a detail row.

### Decisions made with the owner

| Topic | Decision |
|---|---|
| Home airports | Configurable list; start with **ILG, PHL, BWI**. The single `ORIGIN` constant goes away. |
| Discount Den | Owner is **not** a member. Store both fares; rank on the public fare; show savings. |
| Return airport | Same home airport by default; **open-jaw toggle** (off by default) allows a different one. |
| Data source | **Scrape Frontier's booking site with Playwright** (spike confirmed); public fare pages for the route catalog. Duffel API is the fallback if scraping is ever blocked. |
| Mixed-airline packages | Allowed by default (each one-way is a separate booking anyway). |
| Bag fees | Fare-only totals; optional "Count a carry-on" toggle, off by default. |
| Table name | Keep `avelo_flights` (no risky rename migration). |

### Non-goals

- Connecting flights (nonstops only, as today).
- Booking anything automatically (hand-off stops at the airline's results page).
- Airlines other than Avelo and Frontier — but the registry makes a third one a
  data-only change plus a scraper.
- Travelers > 1, hotel costs (separate ideas on the backlog).

## 2. Background

### Frontier API research (2026-10-06)

- **No public fare API.** Frontier distributes via NDC to travel agencies
  (Fareportal, Apr 2025) only.
- **Duffel** aggregates Frontier: searches are free up to a 1500:1
  search-to-book ratio, then $0.005/search; likely public fares only (no
  Discount Den). Kept as fallback.
- Frontier **does not serve ILG** (left June 2022, third exit). Nearest bases:
  PHL (~20 nonstops: MCO, TPA, RSW, MIA, SJU, ATL, DEN, LAS, CUN, PUJ, …) and BWI.

### Spike findings (throwaway code; captures in `frontier-spike/`)

- `https://booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov%2012%2C%202026&ADT=1&mon=true&promo=`
  loads **headless with the existing stealth setup** — no challenge on 5 page
  loads (PHL→MCO, MCO→PHL, BWI→TPA). Visit `www.flyfrontier.com` first.
- The select page embeds all data as JSON: `window.FlightDataJson` (parsed by the
  page from `window.FlightData`). Read it with
  `page.evaluate("JSON.parse(JSON.stringify(window.FlightDataJson))")` after
  `wait_for_function("window.FlightDataJson && window.FlightDataJson.journeys")`.
  - `journeys[0].flights[]`: `discountDenFare`, `standardFare`, `stopCount`,
    `legs[].{departureStation, arrivalStation, departureDate, arrivalDate,
    departureDateUtc, arrivalDateUtc, duration}`, `isNextDayArrival`,
    `duration`, and fare keys containing the flight number (`F9~2415~`).
  - Top level: `lowestDDAmount`, `lowestStandardAmount`, `hasActiveDDSubscription`.
  - Most flights are connections (MCO→PHL 25 flights / 5 nonstop; BWI→TPA 7 / 1).
  - Discount Den is often only **$1** below standard on Basic fares.
- Adding `&c=true` gives the calendar view, which POSTs
  `/Flight/GetLowFareCalendarAvailability` and receives
  `{"calendarSections":[{"departingStation","arrivalStation","weeks":[{"days":[{"dateFormatted","amount","taxesAndFees","amountString","isUnavailable","isDiscountDen",…}]}]}]}`
  — a month of **lowest Discount Den** fares per day. No standard fare, and the
  lowest fare may be a connection.
- Timing: 2–3.5 s per route-day page.

## 3. Data model

### Home airports

- `config.HOME_AIRPORTS: dict[str, Airport]` replaces `ORIGIN`/`ORIGIN_AIRPORT`;
  env `AVELO_HOME_AIRPORTS=ILG,PHL,BWI` selects which are active (default all
  three). `Airport` gains `parking_per_day: float` (owner-set estimate,
  editable in the dashboard; stored in a `settings` table).
- `config.ALL_AIRPORTS` = home airports ∪ destinations (BWI/PHL lat/lon added).
- Every former `config.ORIGIN` use (~20 sites in app.py, alerts.py, optimizer.py,
  signals.py, mock_data.py, config.default_routes) takes a list of origins.

### Airlines

New `airlines.py`:

```python
@dataclass(frozen=True)
class Airline:
    code: str              # "XP" (Avelo), "F9" (Frontier)
    name: str
    member_program: str    # "Avelo PLUS", "Discount Den"
    color: tuple[int, int, int]
    def booking_url(self, origin, dest, day) -> str | None: ...  # None = needs assisted handoff
AIRLINES: dict[str, Airline]
```

Which airline flies which pair comes from the route catalog (below), not
hard-coded lists. Memberships (`XP: True, F9: False`) live in `settings` and are
editable in the dashboard.

### Database (all via `MIGRATIONS` / `SCHEMA`; existing data kept)

- `avelo_flights.airline TEXT NOT NULL DEFAULT 'XP'`; same column on
  `price_history`, `route_day_seen`, `route_floor`, `events`. Existing rows
  backfill to `'XP'` through the default.
- Fare semantics: `price` = **the fare the owner would pay** (member fare when
  the owner belongs to that airline's program, else public); `base_price` =
  public fare; new `member_price REAL` = program fare. For Avelo today
  `price == member_price`; for Frontier (not a member) `price == base_price` and
  `member_price` holds the Discount Den fare. When membership changes, a
  one-line SQL recomputes `price` from the two stored fares.
- Flight ids: Avelo unchanged (`LOWFARE-ILG-TPA`, `AVELO-ILG-TPA`); Frontier
  `F9CAL-PHL-MCO` (calendar placeholder, 00:00, `time_known=False`) and
  `F9-2415` (detail, real time, `flight_number='2415'`).
- New `route_catalog(airline, origin, destination, first_seen, last_seen,
  source)` — PK (airline, origin, destination).
- New `settings(key PRIMARY KEY, value)` for memberships and parking.
- **Trip identity** (new helper `trip_key(flight)`):
  `airline|origin|destination|YYYY-MM-DD|HH:MM-or-*`. Watches, rules, bookings
  and package keys use it. Matching rule: equal when airline, route and date
  match and either side's time is `*` or the times are equal. A stored
  `*` key is upgraded in place to the timed key when a detail row appears.

## 4. Scrapers

- **`browser.py`** (extracted from `AveloScraper`, behavior-preserving): context
  creation with stealth/UA/viewport, per-airline cookie file
  `data/browser_state_<code>.json` (atomic write; a corrupt file is ignored),
  human pauses, polite delay, `_retry`, failure snapshot (screenshot **and**
  HTML), `ScrapeReport` (+ `status: ok|partial|failed`).
- **`scraper.py`**: `AveloScraper(BaseScraper)`; logic unchanged except shared
  plumbing and the stale-row fixes below. Fills `route_catalog` from
  `routes/all`.
- **`frontier.py`**: `FrontierScraper(BaseScraper)` plus pure parsers:
  - `parse_frontier_select(payload, scraped_at, member) -> list[Flight]` —
    nonstops only; local departure/arrival times; both fares; flight number from
    the fare key; next-day arrivals handled.
  - `parse_frontier_calendar(payload, scraped_at, member) -> list[Flight]` —
    one placeholder per available day (`amount + taxesAndFees` = DD total), skip
    `isUnavailable`/`isEmpty`; standard fare unknown (`base_price=None`).
  - `parse_frontier_routes(html, origin) -> set[str]` — destinations from the
    public "flights from <city>" page (`flights.flyfrontier.com/en/flights-from-<slug>`),
    plain HTTP via `netutil`, refreshed when the catalog is >7 days old.
  - `is_challenge_page(html) -> bool`.
- **Modes**: `calendar` = one calendar request per route per month;
  `detail` = one select page per route per day; `full` = calendar, then detail
  on the cheapest days (as Avelo).
- **CLI** (`run_scraper.py`): `--airlines XP,F9` (default all),
  `--origins ILG,PHL,BWI` (default active home airports); routes = catalog pairs
  in (origins × destinations, both directions) for each airline. `--promo`
  becomes `--promo F9=CODE` (bare `--promo CODE` still means Avelo).
- **Reliability (folded in from the 2026-09-29 audit)**:
  - After a successful parse, delete that airline's rows for the searched
    route-days that returned no fare (calendar: whole parsed month; detail:
    all rows for that day, not only placeholders).
  - Days in months that failed to parse are **not** recorded as sold out.
  - `DateUnavailable` is not retried; it records the day as no-fare.
  - `data/scrape.lock` (pid + start time; stale if pid is dead) — one scrape at
    a time across dashboard, nightly job and CLI. `run_scraper.main` wraps the
    run in try/finally so `finish_run` always records a status.
  - Frontier block detection: 2 consecutive challenge pages → stop Frontier for
    this run, status `partial`, continue other airlines. Partial or failed runs
    never produce `sold_out` events and are skipped as "previous run" baselines.

## 5. Packages, alerts, hand-off

- `GetawayQuery`: `origins: list[str]`, `airlines: set[str]`,
  `allow_open_jaw: bool = False`, `count_carry_on: bool = False`.
- `build_itineraries`: outbound from any selected origin on any selected
  airline; inbound must land at the same origin unless `allow_open_jaw`.
  Destination mix-and-match keeps working across airlines.
- `Itinerary` gains `airlines` label (`XP ILG→TPA · F9 TPA→PHL`),
  `member_total` (Discount Den / PLUS total), `member_savings`.
- Door-to-door: drive time to the departure home airport and from the return
  one (`places` cache per (place, airport) already exists), + parking
  `parking_per_day × nights`, + inter-airport drive for open-jaw. Carry-on fee per
  airline from `config.CARRY_ON_FEE` when the toggle is on.
- Alerts: rules persist `origins`, `airlines`, `allow_open_jaw`, and the partner
  window. Matching uses trip identity. `price_drop` compares against the
  price at the last alert (not the last observation). Watch state is saved only
  after delivery succeeds. Duplicate watches (same trip key) are refused.
- Signals (`sale_wave`, `sold_out`, `new_floor`, `floor_gone`) keyed per airline
  and route; floors exclude promo and mock rows.
- Push titles include the airline name.
- Nightly targets are computed at run time (`run_scraper --from-rules`) from
  rules, watches and bookings, so the plist never goes stale.
- `handoff.py`: per leg by airline — Avelo uses the existing assisted flow;
  Frontier opens `Airline.booking_url(...)` in the default browser. Never books.

## 6. Dashboard

- Sidebar: Home airports multiselect, Airlines checkboxes, open-jaw toggle,
  carry-on toggle; destination list = catalog union with airline tags
  (`TPA · XP F9`).
- Settings expander: memberships, parking $/day per home airport.
- Packages table: airline and home-airport columns, airline color tags,
  "Discount Den would save" column when non-zero.
- New tab **Airline vs airline**: per weekend window and destination, best Avelo
  vs best Frontier option, door-to-door.
- Discount Den meter beside the Plus ROI badge.
- Map arcs from every selected home airport, colored by airline; fare calendar
  heatmap faceted by airline; scrape panel shows routes × days × airlines; the
  "This weekend" button respects airline and airport selections.
- The private ntfy topic is no longer shown in the UI (audit item).

## 7. Testing

Offline, TDD, fixtures under `tests/fixtures/frontier/` (copied from
`docs/superpowers/specs/frontier-spike/`, plus a challenge page and a public
"flights from Philadelphia" page captured in phase 2).

- Parsers: nonstop filter, fares, local times, flight number, next-day arrival,
  calendar unavailable days, challenge detection, route-page parsing.
- Database: migration on a DB built with the current schema (rows → `XP`),
  trip-key matching and upgrade, vanished-fare deletion, scrape lock.
- Optimizer: multi-origin pairing, open-jaw on/off, mixed airlines, parking and
  drive in door-to-door, member savings.
- Alerts: calendar→detail upgrade produces no false new/sold-out; gradual
  price drop fires; per-airline signals; duplicate watch refused.
- App (`AppTest`): mixed-airline DB renders filters and the comparison tab.
- One manual live check: `run_scraper.py --airlines F9 --origins PHL
  --destinations MCO --start <weekend> --end <weekend+3>` before enabling
  Frontier in the nightly job.

## 8. Phases (each leaves the app working)

1. **Multi-home-airport + trip identity + reliability fixes** (Avelo only).
2. **Airline column, registry, route catalog, `browser.py` extraction,
   Frontier scraper** (CLI can scrape Frontier).
3. **Optimizer, alerts, signals, hand-off across airlines.**
4. **Dashboard.**

## 9. Risks

| Risk | Mitigation |
|---|---|
| Frontier adds bot protection later | Block detection + partial runs; Duffel fallback (public fares only). |
| `FlightDataJson` shape changes | Pure parser + fixtures; failure snapshot saves HTML for a new fixture. |
| Calendar lowest fare is a connection | Calendar rows are radar only (`time_verified=False`); packages shown as unverified until a detail run. |
| Scraping volume doubles nightly time | Calendar mode nightly (1 request per route-month); detail only for rule/watch targets. |
| Terms of service | Personal use, low rate, human pauses; same posture as Avelo today. |
