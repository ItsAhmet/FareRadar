# Autopilot + weekly digest — design

Date: 2026-10-14 · Status: approved in conversation, awaiting written-spec review

## 1. Goal

Stop the manual scrape-and-browse loop. One click makes the app watch fares on its own, push only real
deals, and send one weekly "best getaways" digest, so the owner checks the dashboard when there is
something to act on instead of to find out whether there is.

### Why this first (evidence from `data/flights.db`, 2026-10-14)

- All 27 scrape runs since 2026-09-07 were launched by hand; the nightly job was never installed. A run takes
  10–60 minutes.
- No rules, watched packages or bookings exist, so the alert machinery has never pushed anything.
- Fares move a lot between runs: of Avelo fares observed at least twice, 63% moved by $20 or more
  (average swing $37, largest $415); about half of Frontier's did. Catching drops needs regular runs.
- Unattended runs also build the price history that later money-saving features (book-now-or-wait,
  post-booking drop claims) depend on.

### Decisions made with the owner

| Topic | Decision |
|---|---|
| Booking horizon | Both near-term and planned trips: the digest has a **this month** (next 30 days) and a **later** (31–90 days) section. |
| The Mac | A laptop, usually asleep overnight. Rely on launchd running a missed job once on wake; no scheduled wake (`pmset`). |
| Digest timing | Due from **Sunday 18:00** local; the first run at or after that sends it (Sunday night if the lid is open, else Monday morning). |
| Pushes between digests | **Only real deals** (section 4), plus the existing booked-trip drop pushes. |
| Preferences | Taken from the **current sidebar** when the owner clicks Turn on / Update from sidebar. |
| Approach | Extend the existing scheduler, rules and alerts (not a separate job, not dashboard-triggered). |

## 2. Turning it on

The sidebar gets a **🛫 Autopilot** expander. Turn on:

1. Saves one watchlist row with `kind = 'autopilot'` (at most one exists). Its payload is
   `alerts.rule_payload(...)` built from the sidebar filters exactly as "Save rule" does today, with
   `span_days = 90`.
2. Installs the nightly job at **19:00 daily** when no job is installed. When one is already installed its
   time is kept, and the panel notes that 19:00 is what makes a Sunday-night digest possible.
3. The job command (`scheduler.job_command`) is wrapped in `caffeinate -i` on macOS, so idle sleep does not
   interrupt a run in progress. Closing the lid still sleeps the Mac; the run resumes or is retried next time.

If the install fails, the autopilot row is still saved (manual scrapes still run the autopilot check) and the
panel shows the `launchctl` error.

## 3. What each run scrapes

`run_scraper.targets_from_rules` reads the autopilot row like a rule (its destinations, origins, airlines),
with a tiered horizon for the autopilot's own contribution to `days`:

| Run | Autopilot horizon |
|---|---|
| Ordinary run | next **30** days |
| Run on which the digest is due (section 5) | next **90** days |

Mode stays `calendar` (what the job runs today). Rules, watched packages and bookings keep their own spans;
the run covers the maximum of all of them, as today. `AVELO_AIRLINES` still caps the airlines, and unknown
stored airport codes are still skipped with a warning.

## 4. Deals

After every **live** run (nightly or launched from the dashboard; mock runs skip it), autopilot builds the
autopilot rule's query with `alerts.rule_query`, **with the budget lifted**, over stored fares, keeps the
cheapest package per package key (`alerts.package_key`, day-level trip keys), and compares each with its
memory.

Memory, in the autopilot row's `state`:

- `lows`: `{package_key: lowest total seen}`.

A package is a **deal** when all hold:

1. its current total is within the rule's `max_budget`;
2. it has a previous low, i.e. it was seen on an earlier run (a first sighting only records its low and goes
   to the digest);
3. `low − total ≥ $30` **or** `(low − total) / low ≥ 20%` (whichever is easier to meet).

After evaluation, every seen package's low becomes `min(low, total)`. Because a pushed deal's price becomes its
new low, the same package pushes again only after a further $30 / 20% drop.

A package's total is `Itinerary.total_price` (Total Getaway Cost: fares plus add-ons, carry-on when the rule
counts it), the same number the budget is checked against.

At most **3 deal pushes per run**, cheapest first; the rest are counted in a single overflow line and appear in
the digest. Packages whose outbound day has passed are dropped from `lows` on every run.

A change of price basis must not look like a deal. `database.reset_baselines(airline)` (called when a
membership changes) also drops the `lows` and `digest_prices` entries of packages with a leg on that airline,
exactly as it drops package watches' stored totals today. Update from sidebar clears all `lows` when the rule's
`count_carry_on` changes (turning carry-on off would otherwise lower every Frontier total by the fee).

Deal events have kind `autopilot_deal`. The message names the package (`alerts.package_label`), the total, and
the drop ("$128, down $41 from $169"). A Frontier calendar placeholder leg adds `alerts.unverified_note`
("from $X (unverified Frontier calendar price …)"); Frontier calendar prices are only ever compared with
earlier Frontier calendar prices because the package key carries the airline. A leg priced under a promo
code says "with code X".

Autopilot routes are excluded from `alerts.watched_routes`, so route signals (`sale_wave`, `sold_out`,
`floor_gone`, `new_floor`) on routes only autopilot watches are not pushed; they are summarised in the digest.
`alerts.pending_rules` reads `kind = 'rule'` only, so autopilot never raises `rule_match` pushes.
Booked-trip drop pushes (`booked_drop`, ≥ $10) are unchanged.

## 5. The weekly digest

**Due** when `now ≥ S` and `last_digest_at < S`, where `S` is the most recent Sunday 18:00 local at or before
`now`. A manual "Send digest now" sets `last_digest_at` too, so a manual send on Monday morning counts as that
week's digest; one sent on a Wednesday does not cancel the following Sunday's.

**Sent** at the end of a run on which it is due (any live run, so an overdue digest is not lost when the
nightly job finds the lock held by a manual scrape), or from the panel button (stored fares, no scrape).

**Content** (plain text, under 1,500 characters; ntfy's limit is 4 KB):

```
Getaways this week: from $128
THIS MONTH
• Fri Oct 23–Sun 25 · ILG⇄TPA · Avelo · $128 (↓$22 vs last week)
• Thu Oct 29–Sun Nov 1 · ILG⇄MCO · Avelo · $164
• Fri Nov 6–Mon 9 · PHL⇄MIA · Frontier · from $141 (unverified)
LATER (31–90 days)
• Fri Dec 4–Sun 6 · ILG⇄SJU · Avelo · $212 (new)
THIS WEEK: 2 deals pushed · sale on ILG–RSW · booked ILG⇄TPA now $18 cheaper
Data: fares refreshed on 6 of the last 7 days · last scrape Sun 19:12
```

- Two sections split at 30 days from today by outbound date; each lists the **3 cheapest in-budget packages,
  at most one per destination**, ranked by total price (`optimizer.rank_by_price`).
- "↓$X vs last week" when the same package key was in the previous digest (`state.digest_prices`), "(new)" when
  the package key has no low recorded before this week.
- "This week" line: deals pushed, route signals on autopilot routes, booked-trip changes, all since
  `last_digest_at`, read from the `events` table.
- "Data" line: on how many of the last 7 calendar days at least one run finished with status `ok`
  (`scrape_runs`, any airline; runs are not tagged nightly vs manual, and either refreshes fares), and when the
  last run finished. When the last `ok` run is more than 2 days old the line starts with "⚠".
- A section with nothing in budget says so ("Nothing under $400 this month").

The digest is its own push (title "Getaways this week: from $X"), separate from the deal/alert push of the same
run, and is also stored as a `digest` event so it appears in the in-app feed.

## 6. Delivery and state

Uses the existing contract: state is written only after delivery (`alerts.StateWrite` / `apply_writes`);
with no push channel or `AVELO_OFFLINE` the in-app feed is the delivery.

- Deal pushes go out with the run's other pushes (`alerts.deliver`). `lows` are applied only when that delivery
  succeeded, so an undelivered deal is still a deal next run if the price holds.
- The digest is delivered separately; `last_digest_at` and `digest_prices` are written only when it was
  delivered, so a failed digest is retried by the next run.
- `run_scraper` tells `alerts.evaluate_and_deliver` whether the run was a mock (`autopilot=not args.mock`); a
  `--no-alerts` run skips autopilot like every other alert.

## 7. The panel

**Off:** "Autopilot will watch: 14 destinations · ILG, PHL, BWI · Avelo, Frontier · ≤ $400 ·
Fri 17:00 → Sun 23:00 · 2+ nights" and **[Turn on]**. When no push channel is configured: "Push alerts are
off: the digest and deals will appear only in the Watchlist & alerts tab." The ntfy topic is never rendered.

**On:** what it watches, "Last run Tue 19:04 ✓" (or the failure), "Next digest: Sun after 6 pm" (or "due now"),
and **[Update from sidebar]**, **[Send digest now]**, **[Turn off]**.

Turn off removes the autopilot row. It removes the nightly job too unless rules, watched packages or bookings
remain, in which case the panel says the job stays for them.

The Watchlist tab lists the autopilot row ("Autopilot — 14 destinations · ≤ $400 · N packages tracked"); its
Remove button behaves like Turn off.

## 8. Code changes

| File | Change |
|---|---|
| `autopilot.py` (new) | Pure: `autopilot_payload`, `find_deals(lows, packages, budget, now)`, `digest_due(last_digest_at, now)`, `digest_horizon_days(state, now)`, `build_digest(...)`. One DB entry point `pending_autopilot(conn, now)` returning events, `StateWrite`s and an optional digest. |
| `run_scraper.py` | `targets_from_rules` reads the autopilot row with the 30/90-day horizon; passes `autopilot=not args.mock` to `evaluate_and_deliver`. |
| `alerts.py` | `evaluate_and_deliver` runs `pending_autopilot` and delivers the digest separately; `watched_routes` excludes autopilot; `build_message` titles deal pushes. |
| `scheduler.py` | `job_command` prefixes `caffeinate -i` on macOS. |
| `database.py` | No schema change: the row lives in `watchlist`, state in its `state` JSON, digests in `events`. `reset_baselines` also drops the airline's autopilot `lows` / `digest_prices` entries. |
| `app.py` / `views.py` | Autopilot panel (pure summary text in `views.py`), Watchlist tab entry. |
| `CLAUDE.md`, `.env.example` | Document autopilot, the 19:00 default and the digest. |

## 9. Error handling

| Situation | Behaviour |
|---|---|
| Push fails | State not written; deals re-evaluated and digest retried next run. |
| Nightly job exits because a manual scrape holds the lock | The manual run's end runs the autopilot check, including an overdue digest. |
| Scrape fails or is partial | Autopilot works from stored fares; the digest's data line reports it. |
| Laptop shut for days | One catch-up run on wake; an overdue digest goes out then, with run health showing the gap. |
| Job install fails | Autopilot row kept; panel shows the error. |
| Stored autopilot destination no longer known | Skipped with a warning by `targets_from_rules` (existing behaviour). |

## 10. Testing

No network and never the owner's database (the root `conftest.py` guard). Pure-function tests in
`tests/test_autopilot.py`:

- `find_deals`: $30 and 20% boundaries (whichever is easier), first sighting records but does not push,
  over-budget packages tracked but not pushed, cap of 3 with overflow line, no repeat until a further drop,
  departed packages pruned, unverified Frontier and promo wording.
- Price-basis changes: `reset_baselines` drops only that airline's autopilot lows; a membership change then
  raises no deal; changing `count_carry_on` on Update clears lows.
- `digest_due`: before/after Sunday 18:00, multi-week gaps, manual send on Monday vs Wednesday.
- `build_digest`: 30-day split, one package per destination, ↓ vs last week and (new), empty section text,
  "this week" and "data" lines, ⚠ on stale data, length under 1,500 characters.
- `targets_from_rules`: 30 vs 90 days, union with rules/watches/bookings, `AVELO_AIRLINES` cap.
- `watched_routes` excludes autopilot; `pending_rules` ignores it.
- `evaluate_and_deliver`: state held back on a failed push, digest retried, mock runs skipped.
- `scheduler.job_command`: `caffeinate -i` on macOS only.
- `tests/test_app.py` (AppTest): panel off → on → update → off with `scheduler.install` / `remove` faked;
  topic never rendered.

## 11. Out of scope

Scheduled wake (`pmset`); more than one autopilot; phone deep links (the dashboard is `localhost`); scrape-speed
work beyond the 30/90 split (staleness skipping is a later idea); detail scrapes to verify Frontier calendar
prices on deals. Email delivery comes for free through the existing SMTP channel when `AVELO_SMTP_*` is set.
