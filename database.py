"""SQLite persistence for scraped Avelo flights.

Tables
------
avelo_flights   current fare per flight, keyed on (flight_id, departure_datetime).
                Re-scraping overwrites price and scrape timestamp.
price_history   append-only: every observed price with the time it was seen.
                Feeds the trend chart and the deal score. Seeded from existing
                rows the first time an older database is opened.

`price` is the fare we would pay (Avelo PLUS member fare when available, see
`is_member_fare`); `base_price` keeps the public fare. Datetimes are ISO text.
Older databases are migrated in place: missing columns/tables are added on open.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import config
import trips
from config import DB_PATH, HISTORY_RETENTION_DAYS
from models import Flight

SCHEMA = """
CREATE TABLE IF NOT EXISTS avelo_flights (
    flight_id          TEXT    NOT NULL,
    origin             TEXT    NOT NULL,
    destination        TEXT    NOT NULL,
    departure_datetime TEXT    NOT NULL,
    arrival_datetime   TEXT,
    price              REAL    NOT NULL,
    currency           TEXT    NOT NULL DEFAULT 'USD',
    duration_minutes   INTEGER,
    flight_number      TEXT,
    base_price         REAL,
    is_member_fare     INTEGER NOT NULL DEFAULT 0,
    promo_code         TEXT,
    member_price       REAL,
    airline            TEXT    NOT NULL DEFAULT 'XP',
    timestamp_scraped  TEXT    NOT NULL,
    PRIMARY KEY (flight_id, departure_datetime)
);
CREATE INDEX IF NOT EXISTS idx_avelo_route_date
    ON avelo_flights (origin, destination, departure_datetime);

CREATE TABLE IF NOT EXISTS price_history (
    flight_id          TEXT    NOT NULL,
    origin             TEXT    NOT NULL,
    destination        TEXT    NOT NULL,
    departure_datetime TEXT    NOT NULL,
    price              REAL    NOT NULL,
    base_price         REAL,
    promo_code         TEXT,
    airline            TEXT    NOT NULL DEFAULT 'XP',
    scraped_at         TEXT    NOT NULL,
    PRIMARY KEY (flight_id, departure_datetime, scraped_at)
);
CREATE INDEX IF NOT EXISTS idx_history_flight
    ON price_history (flight_id, departure_datetime, scraped_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    mode         TEXT    NOT NULL,
    routes       TEXT    NOT NULL,   -- JSON list of "ORG-DST"
    promo_code   TEXT,
    airline      TEXT    NOT NULL DEFAULT 'XP',
    summary      TEXT               -- JSON report
);

-- What each run saw per route-day: price NULL = scheduled but not selectable (sold out).
CREATE TABLE IF NOT EXISTS route_day_seen (
    run_id       INTEGER NOT NULL,
    origin       TEXT    NOT NULL,
    destination  TEXT    NOT NULL,
    day          TEXT    NOT NULL,
    price        REAL,
    PRIMARY KEY (run_id, origin, destination, day)
);

CREATE TABLE IF NOT EXISTS route_floor (
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    floor_price  REAL NOT NULL,
    seen_at      TEXT NOT NULL,
    PRIMARY KEY (origin, destination)
);

CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    kind         TEXT    NOT NULL,
    origin       TEXT,
    destination  TEXT,
    day          TEXT,
    message      TEXT    NOT NULL,
    detail       TEXT,               -- JSON
    notify       INTEGER NOT NULL DEFAULT 0,
    read         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at DESC);

CREATE TABLE IF NOT EXISTS places (
    query        TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    resolved_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_times (
    place_query  TEXT NOT NULL,
    airport      TEXT NOT NULL,
    minutes      REAL NOT NULL,
    source       TEXT NOT NULL,
    resolved_at  TEXT NOT NULL,
    PRIMARY KEY (place_query, airport)
);

CREATE TABLE IF NOT EXISTS bookings (
    booking_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT    NOT NULL,
    payload      TEXT    NOT NULL,   -- JSON package key + fares at booking time
    price_paid   REAL    NOT NULL,
    booked_at    TEXT    NOT NULL,
    notes        TEXT,
    state        TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS watchlist (
    watch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,   -- 'package' | 'rule'
    label        TEXT    NOT NULL,
    payload      TEXT    NOT NULL,   -- JSON
    state        TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL      -- JSON
);

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
"""

MIGRATIONS: dict[tuple[str, str], str] = {
    ("avelo_flights", "base_price"): "ALTER TABLE avelo_flights ADD COLUMN base_price REAL",
    ("avelo_flights", "is_member_fare"): "ALTER TABLE avelo_flights ADD COLUMN is_member_fare INTEGER NOT NULL DEFAULT 0",
    ("avelo_flights", "promo_code"): "ALTER TABLE avelo_flights ADD COLUMN promo_code TEXT",
    ("scrape_runs", "status"): "ALTER TABLE scrape_runs ADD COLUMN status TEXT",  # ok | partial | failed
    ("avelo_flights", "airline"): "ALTER TABLE avelo_flights ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
    ("avelo_flights", "member_price"): "ALTER TABLE avelo_flights ADD COLUMN member_price REAL",
    ("price_history", "airline"): "ALTER TABLE price_history ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
    ("scrape_runs", "airline"): "ALTER TABLE scrape_runs ADD COLUMN airline TEXT NOT NULL DEFAULT 'XP'",
}

UPSERT_SQL = """
INSERT INTO avelo_flights (
    flight_id, origin, destination, departure_datetime, arrival_datetime,
    price, currency, duration_minutes, flight_number, base_price, is_member_fare, promo_code, member_price, airline, timestamp_scraped
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (flight_id, departure_datetime) DO UPDATE SET
    price             = excluded.price,
    currency          = excluded.currency,
    arrival_datetime  = COALESCE(excluded.arrival_datetime, avelo_flights.arrival_datetime),
    duration_minutes  = COALESCE(excluded.duration_minutes, avelo_flights.duration_minutes),
    flight_number     = COALESCE(excluded.flight_number, avelo_flights.flight_number),
    base_price        = COALESCE(excluded.base_price, avelo_flights.base_price),
    is_member_fare    = excluded.is_member_fare,
    promo_code        = excluded.promo_code,
    member_price      = CASE WHEN excluded.member_price IS NOT NULL THEN excluded.member_price
                             WHEN excluded.is_member_fare = 1 THEN excluded.price
                             ELSE NULL END,
    airline           = excluded.airline,
    timestamp_scraped = excluded.timestamp_scraped;
"""

HISTORY_SQL = """
INSERT OR IGNORE INTO price_history
    (flight_id, origin, destination, departure_datetime, price, base_price, promo_code, airline, scraped_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #


def _resolve(db_path: Path | str | None) -> Path | str:
    return DB_PATH if db_path is None else db_path


def _migrate(conn: sqlite3.Connection) -> None:
    columns: dict[str, set[str]] = {}
    for (table, column), ddl in MIGRATIONS.items():
        if table not in columns:
            columns[table] = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns[table]:
            conn.execute(ddl)
            columns[table].add(column)
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
    conn.commit()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and initialise/migrate) the database. Callers own closing it."""
    path = Path(_resolve(db_path))
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with connection(db_path):
        pass


def _run(sql: str, params: Iterable, conn: sqlite3.Connection | None, many: bool = False) -> int:
    """Execute a write against `conn` or a temporary connection; returns rowcount."""
    if conn is not None:
        cur = conn.executemany(sql, params) if many else conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    with connection() as own:
        cur = own.executemany(sql, params) if many else own.execute(sql, params)
        return cur.rowcount


def _fetch(sql: str, params: Iterable, conn: sqlite3.Connection | None) -> list[sqlite3.Row]:
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    with connection() as own:
        return own.execute(sql, params).fetchall()


def _placeholders(values: Iterable) -> str:
    return ",".join("?" for _ in values)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def _to_row(f: Flight) -> tuple:
    return (
        f.flight_id,
        f.origin,
        f.destination,
        f.departure_datetime.isoformat(timespec="minutes"),
        f.arrival_datetime.isoformat(timespec="minutes") if f.arrival_datetime else None,
        float(f.price),
        f.currency,
        f.duration_minutes,
        f.flight_number,
        float(f.base_price) if f.base_price is not None else None,
        1 if f.is_member_fare else 0,
        f.promo_code,
        float(f.member_price) if f.member_price is not None else None,
        f.airline,
        f.timestamp_scraped.isoformat(timespec="seconds"),
    )


def _to_history_row(f: Flight) -> tuple:
    return (
        f.flight_id,
        f.origin,
        f.destination,
        f.departure_datetime.isoformat(timespec="minutes"),
        float(f.price),
        float(f.base_price) if f.base_price is not None else None,
        f.promo_code,
        f.airline,
        f.timestamp_scraped.isoformat(timespec="seconds"),
    )


def upsert_flights(flights: Iterable[Flight], conn: sqlite3.Connection | None = None) -> int:
    """Insert or refresh flights and append each observation to price_history."""
    flights = list(flights)
    if not flights:
        return 0
    rows = [_to_row(f) for f in flights]
    history = [_to_history_row(f) for f in flights]
    if conn is not None:
        conn.executemany(UPSERT_SQL, rows)
        conn.executemany(HISTORY_SQL, history)
        conn.commit()
    else:
        with connection() as own:
            own.executemany(UPSERT_SQL, rows)
            own.executemany(HISTORY_SQL, history)
    return len(rows)


def purge_departed(conn: sqlite3.Connection | None = None, now: datetime | None = None) -> int:
    """Delete flights whose departure is already in the past."""
    cutoff = (now or datetime.now()).isoformat(timespec="minutes")
    return _run("DELETE FROM avelo_flights WHERE departure_datetime < ?", (cutoff,), conn)


def prune_history(conn: sqlite3.Connection | None = None, now: datetime | None = None, keep_days: int | None = None) -> int:
    """Drop history rows for departed flights older than the retention window."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=keep_days if keep_days is not None else HISTORY_RETENTION_DAYS)).isoformat(timespec="minutes")
    return _run("DELETE FROM price_history WHERE departure_datetime < ?", (cutoff,), conn)


def drop_shadowed_placeholders(origin: str, destination: str, conn: sqlite3.Connection | None = None,
                               airline: str = "XP") -> int:
    """Remove an airline's calendar placeholders on days where the same airline
    already has a real (timed) row, so a calendar run after a detail run never
    duplicates a flight.

    A placeholder is a row with no arrival time stored at 00:00 (calendar rows have
    no clock time). A detail row that merely lacks an arrival (a card showing one
    time and no duration) departs at its real time, so it is neither deleted nor
    ignored as a shadow.
    """
    sql = """
        DELETE FROM avelo_flights
        WHERE airline = ? AND origin = ? AND destination = ?
          AND arrival_datetime IS NULL AND substr(departure_datetime, 12, 5) = '00:00'
          AND substr(departure_datetime, 1, 10) IN (
              SELECT substr(departure_datetime, 1, 10) FROM avelo_flights
              WHERE airline = ? AND origin = ? AND destination = ?
                AND (arrival_datetime IS NOT NULL OR substr(departure_datetime, 12, 5) <> '00:00')
          )
    """
    a, o, d = airline.upper(), origin.upper(), destination.upper()
    return _run(sql, (a, o, d, a, o, d), conn)


def maintenance(conn: sqlite3.Connection | None = None, now: datetime | None = None) -> dict[str, int]:
    """Housekeeping run at app start and before every scrape."""
    return {
        "departed_flights_removed": purge_departed(conn, now),
        "history_rows_pruned": prune_history(conn, now),
    }


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


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _from_row(row: sqlite3.Row) -> Flight:
    return Flight(
        flight_id=row["flight_id"],
        origin=row["origin"],
        destination=row["destination"],
        departure_datetime=datetime.fromisoformat(row["departure_datetime"]),
        arrival_datetime=datetime.fromisoformat(row["arrival_datetime"]) if row["arrival_datetime"] else None,
        price=row["price"],
        currency=row["currency"],
        duration_minutes=row["duration_minutes"],
        flight_number=row["flight_number"],
        base_price=row["base_price"],
        is_member_fare=bool(row["is_member_fare"]),
        promo_code=row["promo_code"],
        member_price=row["member_price"],
        airline=row["airline"],
        timestamp_scraped=datetime.fromisoformat(row["timestamp_scraped"]),
    )


def query_flights(
    origins: Iterable[str],
    destinations: Iterable[str],
    start_date: date,
    end_date: date,
    max_price: float | None = None,
    conn: sqlite3.Connection | None = None,
    airlines: Iterable[str] | None = None,
) -> list[Flight]:
    """All flights from any `origins` to any `destinations` departing within
    [start_date, end_date] inclusive, cheapest first."""
    origins = [o.upper() for o in origins]
    destinations = [d.upper() for d in destinations]
    if not origins or not destinations:
        return []
    params: list = [*origins, *destinations, start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    sql = f"""
        SELECT * FROM avelo_flights
        WHERE origin IN ({_placeholders(origins)})
          AND destination IN ({_placeholders(destinations)})
          AND departure_datetime >= ? AND departure_datetime < ?
    """
    if airlines is not None:
        codes = [a.upper() for a in airlines]
        if not codes:
            return []
        sql += f" AND airline IN ({_placeholders(codes)})"
        params.extend(codes)
    if max_price is not None:
        sql += " AND price <= ?"
        params.append(max_price)
    sql += " ORDER BY price ASC, departure_datetime ASC"
    return [_from_row(r) for r in _fetch(sql, params, conn)]


def route_calendar(
    start_date: date,
    end_date: date,
    conn: sqlite3.Connection | None = None,
    airlines: Iterable[str] | None = None,
) -> list[dict]:
    """Lowest price per (origin, destination, day). Feeds the calendar view.
    `airlines` restricts to those carriers (None = all)."""
    params: list = [start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    airline_sql = ""
    if airlines is not None:
        codes = [a.upper() for a in airlines]
        if not codes:
            return []
        airline_sql = f" AND airline IN ({_placeholders(codes)})"
        params.extend(codes)
    sql = f"""
        SELECT origin, destination, substr(departure_datetime, 1, 10) AS day,
               MIN(price) AS min_price, COUNT(*) AS flights
        FROM avelo_flights
        WHERE departure_datetime >= ? AND departure_datetime < ?{airline_sql}
        GROUP BY origin, destination, day
        ORDER BY day
    """
    return [dict(r) for r in _fetch(sql, params, conn)]


def stats(conn: sqlite3.Connection | None = None) -> dict:
    sql = """
        SELECT COUNT(*) AS flights,
               COUNT(DISTINCT origin || '-' || destination) AS routes,
               MAX(timestamp_scraped) AS last_scraped,
               MIN(substr(departure_datetime,1,10)) AS first_day,
               MAX(substr(departure_datetime,1,10)) AS last_day
        FROM avelo_flights
    """
    return dict(_fetch(sql, (), conn)[0])


def _codes(values: str | Iterable[str]) -> list[str]:
    """A code or an iterable of codes, upper-cased, order kept."""
    return [values.upper()] if isinstance(values, str) else [v.upper() for v in values]


def selection_stats(
    origins: str | Iterable[str],
    destinations: Iterable[str],
    start_date: date,
    end_date: date,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Coverage of the user's current selection (both directions)."""
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return {"flights": 0, "member_fares": 0, "last_scraped": None, "routes": 0}
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    sql = f"""
        SELECT COUNT(*) AS flights,
               SUM(is_member_fare) AS member_fares,
               MAX(timestamp_scraped) AS last_scraped,
               COUNT(DISTINCT origin || '-' || destination) AS routes
        FROM avelo_flights
        WHERE ((origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_})))
          AND departure_datetime >= ? AND departure_datetime < ?
    """
    params = [*origins, *destinations, *origins, *destinations,
              start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    row = dict(_fetch(sql, params, conn)[0])
    row["member_fares"] = row["member_fares"] or 0
    return row


def route_freshness(
    origins: str | Iterable[str], destinations: Iterable[str], conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Per directional route: when it was last scraped and how many fares it holds."""
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return []
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    sql = f"""
        SELECT origin, destination, MAX(timestamp_scraped) AS last_scraped, COUNT(*) AS fares,
               MIN(substr(departure_datetime,1,10)) AS first_day, MAX(substr(departure_datetime,1,10)) AS last_day
        FROM avelo_flights
        WHERE (origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_}))
        GROUP BY origin, destination
        ORDER BY origin NOT IN ({po}), destination, origin
    """
    return [dict(r) for r in _fetch(sql, [*origins, *destinations, *origins, *destinations, *origins], conn)]


def route_medians(
    origins: str | Iterable[str],
    destinations: Iterable[str],
    start_date: date,
    end_date: date,
    conn: sqlite3.Connection | None = None,
    airlines: Iterable[str] | None = None,
) -> dict[tuple[str, str, str], float]:
    """Median current fare per airline and directional route in the span, keyed
    (airline, origin, destination): the 'typical' price a package leg is compared
    against for its deal score. `airlines` restricts to those carriers (None = all)."""
    origins, destinations = _codes(origins), [d.upper() for d in destinations]
    if not destinations or not origins:
        return {}
    po, pd_ = _placeholders(origins), _placeholders(destinations)
    params = [*origins, *destinations, *origins, *destinations,
              start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    airline_sql = ""
    if airlines is not None:
        codes = [a.upper() for a in airlines]
        if not codes:
            return {}
        airline_sql = f" AND airline IN ({_placeholders(codes)})"
        params.extend(codes)
    sql = f"""
        SELECT airline, origin, destination, price FROM avelo_flights
        WHERE ((origin IN ({po}) AND destination IN ({pd_})) OR (destination IN ({po}) AND origin IN ({pd_})))
          AND departure_datetime >= ? AND departure_datetime < ?{airline_sql}
        ORDER BY airline, origin, destination, price
    """
    prices: dict[tuple[str, str, str], list[float]] = {}
    for r in _fetch(sql, params, conn):
        prices.setdefault((r["airline"], r["origin"], r["destination"]), []).append(r["price"])
    medians = {}
    for key, values in prices.items():
        n = len(values)
        medians[key] = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return medians


def price_history(flight_id: str, departure_datetime: datetime, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every observed price for one flight, oldest first."""
    sql = """
        SELECT scraped_at, price, base_price, promo_code FROM price_history
        WHERE flight_id = ? AND departure_datetime = ?
        ORDER BY scraped_at
    """
    return [dict(r) for r in _fetch(sql, (flight_id, departure_datetime.isoformat(timespec="minutes")), conn)]


def previous_prices(
    keys: Iterable[tuple[str, datetime]], conn: sqlite3.Connection | None = None
) -> dict[tuple[str, str], float]:
    """For each (flight_id, departure) the price seen on the scrape *before*
    the latest one, keyed by (flight_id, iso departure). Missing = only one observation."""
    result: dict[tuple[str, str], float] = {}
    sql = """
        SELECT price FROM price_history
        WHERE flight_id = ? AND departure_datetime = ?
        ORDER BY scraped_at DESC LIMIT 2
    """
    for flight_id, dep in set(keys):
        iso = dep.isoformat(timespec="minutes")
        rows = _fetch(sql, (flight_id, iso), conn)
        if len(rows) == 2:
            result[(flight_id, iso)] = rows[1]["price"]
    return result


# --------------------------------------------------------------------------- #
# Scrape runs and sightings (feed the signal detectors)
# --------------------------------------------------------------------------- #


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def start_run(mode: str, routes: Iterable[tuple[str, str]], promo_code: str | None = None,
              conn: sqlite3.Connection | None = None, now: datetime | None = None, airline: str = "XP") -> int:
    routes_json = json.dumps([f"{o}-{d}" for o, d in routes])
    sql = "INSERT INTO scrape_runs (started_at, mode, routes, promo_code, airline) VALUES (?, ?, ?, ?, ?)"
    if conn is not None:
        cur = conn.execute(sql, (_now(now), mode, routes_json, promo_code, airline.upper()))
        conn.commit()
        return int(cur.lastrowid)
    with connection() as own:
        return int(own.execute(sql, (_now(now), mode, routes_json, promo_code, airline.upper())).lastrowid)


def finish_run(run_id: int, summary: dict | None = None, conn: sqlite3.Connection | None = None,
               now: datetime | None = None, status: str = "ok") -> None:
    _run("UPDATE scrape_runs SET finished_at = ?, summary = ?, status = ? WHERE run_id = ?",
         (_now(now), json.dumps(summary or {}), status, run_id), conn)


def annotate_run(run_id: int, extra: dict, conn: sqlite3.Connection | None = None) -> None:
    """Merge `extra` into a run's stored summary without touching its status or finish time."""
    rows = _fetch("SELECT summary FROM scrape_runs WHERE run_id = ?", (run_id,), conn)
    if rows:
        summary = {**json.loads(rows[0]["summary"] or "{}"), **extra}
        _run("UPDATE scrape_runs SET summary = ? WHERE run_id = ?", (json.dumps(summary), run_id), conn)


def latest_run_ids(conn: sqlite3.Connection | None = None) -> list[int]:
    """The newest run of each airline, newest first. A scrape writes one run row per airline, so "the
    latest run" as the dashboard means it is this set, not the single newest row."""
    rows = _fetch("SELECT MAX(run_id) AS run_id FROM scrape_runs GROUP BY airline ORDER BY run_id DESC", (), conn)
    return [int(r["run_id"]) for r in rows]


def run_airline(run_id: int, conn: sqlite3.Connection | None = None) -> str | None:
    """The airline code a run belongs to, or None when the run does not exist."""
    rows = _fetch("SELECT airline FROM scrape_runs WHERE run_id = ?", (run_id,), conn)
    return rows[0]["airline"] if rows else None


def last_runs(limit: int = 10, conn: sqlite3.Connection | None = None) -> list[dict]:
    rows = _fetch("SELECT * FROM scrape_runs ORDER BY run_id DESC LIMIT ?", (limit,), conn)
    out = []
    for r in rows:
        d = dict(r)
        d["routes"] = json.loads(d["routes"] or "[]")
        d["summary"] = json.loads(d["summary"] or "{}")
        out.append(d)
    return out


def record_seen(run_id: int, origin: str, destination: str, days: Iterable[date], flights: Iterable[Flight],
                conn: sqlite3.Connection | None = None) -> int:
    """One row per scheduled day for this route in this run; price is the
    lowest fare seen that day, or NULL when the day was not selectable."""
    o, d = origin.upper(), destination.upper()
    lowest: dict[date, float] = {}
    for f in flights:
        if f.origin == o and f.destination == d:
            lowest[f.departure_date] = min(lowest.get(f.departure_date, float("inf")), f.price)
    rows = [(run_id, o, d, day.isoformat(), lowest.get(day)) for day in sorted(set(days))]
    if not rows:
        return 0
    _run("INSERT OR REPLACE INTO route_day_seen (run_id, origin, destination, day, price) VALUES (?, ?, ?, ?, ?)",
         rows, conn, many=True)
    return len(rows)


def seen_in_run(run_id: int, origin: str, destination: str, conn: sqlite3.Connection | None = None) -> dict[date, float | None]:
    rows = _fetch("SELECT day, price FROM route_day_seen WHERE run_id = ? AND origin = ? AND destination = ?",
                  (run_id, origin.upper(), destination.upper()), conn)
    return {date.fromisoformat(r["day"]): r["price"] for r in rows}


def routes_in_run(run_id: int, conn: sqlite3.Connection | None = None) -> list[tuple[str, str]]:
    rows = _fetch("SELECT DISTINCT origin, destination FROM route_day_seen WHERE run_id = ? ORDER BY origin, destination",
                  (run_id,), conn)
    return [(r["origin"], r["destination"]) for r in rows]


def previous_run_for_route(run_id: int, origin: str, destination: str, conn: sqlite3.Connection | None = None) -> int | None:
    """Latest earlier run that finished cleanly and saw this route (legacy runs have NULL status)."""
    rows = _fetch(
        """SELECT MAX(s.run_id) AS prev FROM route_day_seen s
           JOIN scrape_runs r ON r.run_id = s.run_id
           WHERE s.run_id < ? AND s.origin = ? AND s.destination = ?
             AND r.finished_at IS NOT NULL AND COALESCE(r.status, 'ok') = 'ok'
             AND r.airline = (SELECT airline FROM scrape_runs WHERE run_id = ?)""",
        (run_id, origin.upper(), destination.upper(), run_id), conn)
    return rows[0]["prev"] if rows and rows[0]["prev"] is not None else None


def get_floor(origin: str, destination: str, conn: sqlite3.Connection | None = None) -> float | None:
    rows = _fetch("SELECT floor_price FROM route_floor WHERE origin = ? AND destination = ?",
                  (origin.upper(), destination.upper()), conn)
    return rows[0]["floor_price"] if rows else None


def set_floor(origin: str, destination: str, price: float, conn: sqlite3.Connection | None = None,
              now: datetime | None = None) -> None:
    _run("""INSERT INTO route_floor (origin, destination, floor_price, seen_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (origin, destination) DO UPDATE SET floor_price = excluded.floor_price, seen_at = excluded.seen_at""",
         (origin.upper(), destination.upper(), float(price), _now(now)), conn)


def get_flight(flight_id: str, departure_datetime: datetime, conn: sqlite3.Connection | None = None) -> Flight | None:
    rows = _fetch("SELECT * FROM avelo_flights WHERE flight_id = ? AND departure_datetime = ?",
                  (flight_id, departure_datetime.isoformat(timespec="minutes")), conn)
    return _from_row(rows[0]) if rows else None


def trip_candidates(key: str, conn: sqlite3.Connection | None = None) -> list[Flight]:
    """Every flight a trip key may refer to right now, cheapest first.

    Timed key → the flight departing at exactly that time. Day-only key → that
    day's timed flights if a detail run has seen the day, else the calendar row.
    A caller that knows more (a package's window) picks among them; see
    `alerts.package_legs`.
    """
    k = trips.parse(key)
    same_day = [f for f in query_flights([k.origin], [k.destination], k.day, k.day, conn=conn) if f.airline == k.airline]
    if k.time != trips.ANY_TIME:
        return [f for f in same_day if f.time_known and f.departure_datetime.strftime("%H:%M") == k.time]
    return [f for f in same_day if f.time_known] or same_day


def find_trip(key: str, conn: sqlite3.Connection | None = None) -> Flight | None:
    """The flight a trip key refers to right now: the cheapest of `trip_candidates`.

    Knows nothing about windows, so for a day-only key it may return a flight a
    package cannot use; package watches and bookings resolve through
    `alerts.package_legs` instead.
    """
    found = trip_candidates(key, conn)
    return found[0] if found else None


# --------------------------------------------------------------------------- #
# Events (signals + alerts feed)
# --------------------------------------------------------------------------- #


def add_events(run_id: int | None, events: Iterable, conn: sqlite3.Connection | None = None,
               now: datetime | None = None) -> int:
    """Store Event-like objects (attributes: kind, origin, destination, day, message, detail, notify)."""
    rows = [
        (run_id, e.kind, e.origin, e.destination, e.day.isoformat() if e.day else None, e.message,
         json.dumps(e.detail or {}), 1 if e.notify else 0, _now(now))
        for e in events
    ]
    if not rows:
        return 0
    _run("""INSERT INTO events (run_id, kind, origin, destination, day, message, detail, notify, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows, conn, many=True)
    return len(rows)


def list_events(limit: int = 100, notify_only: bool = False, unread_only: bool = False,
                run_id: int | None = None, conn: sqlite3.Connection | None = None,
                run_ids: Iterable[int] | None = None) -> list[dict]:
    """`run_ids` keeps events of any of those runs (an empty list matches nothing)."""
    where, params = [], []
    if run_ids is not None:
        run_ids = list(run_ids)
        if not run_ids:
            return []
        where.append(f"run_id IN ({_placeholders(run_ids)})")
        params.extend(run_ids)
    if notify_only:
        where.append("notify = 1")
    if unread_only:
        where.append("read = 0")
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY event_id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in _fetch(sql, params, conn):
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        out.append(d)
    return out


def unread_event_count(conn: sqlite3.Connection | None = None) -> int:
    return int(_fetch("SELECT COUNT(*) AS n FROM events WHERE notify = 1 AND read = 0", (), conn)[0]["n"])


def mark_events_read(conn: sqlite3.Connection | None = None) -> int:
    return _run("UPDATE events SET read = 1 WHERE read = 0", (), conn)


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


def add_watch(kind: str, label: str, payload: dict, conn: sqlite3.Connection | None = None,
              now: datetime | None = None) -> int:
    sql = "INSERT INTO watchlist (kind, label, payload, state, created_at) VALUES (?, ?, ?, '{}', ?)"
    params = (kind, label, json.dumps(payload, default=str), _now(now))
    if conn is not None:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid)
    with connection() as own:
        return int(own.execute(sql, params).lastrowid)


def list_watches(kind: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    sql, params = "SELECT * FROM watchlist", []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY watch_id"
    out = []
    for r in _fetch(sql, params, conn):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["state"] = json.loads(d["state"] or "{}")
        out.append(d)
    return out


def update_watch_state(watch_id: int, state: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE watchlist SET state = ? WHERE watch_id = ?", (json.dumps(state, default=str), watch_id), conn)


def update_watch_payload(watch_id: int, payload: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE watchlist SET payload = ? WHERE watch_id = ?", (json.dumps(payload, default=str), watch_id), conn)


def remove_watch(watch_id: int, conn: sqlite3.Connection | None = None) -> int:
    return _run("DELETE FROM watchlist WHERE watch_id = ?", (watch_id,), conn)


# --------------------------------------------------------------------------- #
# Places (place-based mode cache)
# --------------------------------------------------------------------------- #


def get_place(query: str, conn: sqlite3.Connection | None = None) -> dict | None:
    rows = _fetch("SELECT * FROM places WHERE query = ?", (query,), conn)
    return dict(rows[0]) if rows else None


def set_place(query: str, name: str, lat: float, lon: float, conn: sqlite3.Connection | None = None) -> None:
    _run("INSERT OR REPLACE INTO places (query, name, lat, lon, resolved_at) VALUES (?, ?, ?, ?, ?)",
         (query, name, float(lat), float(lon), _now()), conn)


def get_drive(place_query: str, airport: str, conn: sqlite3.Connection | None = None) -> dict | None:
    rows = _fetch("SELECT * FROM drive_times WHERE place_query = ? AND airport = ?", (place_query, airport.upper()), conn)
    return dict(rows[0]) if rows else None


def set_drive(place_query: str, airport: str, minutes: float, source: str, conn: sqlite3.Connection | None = None) -> None:
    _run("INSERT OR REPLACE INTO drive_times (place_query, airport, minutes, source, resolved_at) VALUES (?, ?, ?, ?, ?)",
         (place_query, airport.upper(), float(minutes), source, _now()), conn)


# --------------------------------------------------------------------------- #
# Bookings
# --------------------------------------------------------------------------- #


def add_booking(label: str, payload: dict, price_paid: float, notes: str | None = None,
                conn: sqlite3.Connection | None = None, now: datetime | None = None) -> int:
    sql = "INSERT INTO bookings (label, payload, price_paid, booked_at, notes, state) VALUES (?, ?, ?, ?, ?, '{}')"
    params = (label, json.dumps(payload, default=str), float(price_paid), _now(now), notes)
    if conn is not None:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid)
    with connection() as own:
        return int(own.execute(sql, params).lastrowid)


def list_bookings(conn: sqlite3.Connection | None = None) -> list[dict]:
    out = []
    for r in _fetch("SELECT * FROM bookings ORDER BY booking_id", (), conn):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["state"] = json.loads(d["state"] or "{}")
        out.append(d)
    return out


def update_booking_state(booking_id: int, state: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE bookings SET state = ? WHERE booking_id = ?", (json.dumps(state, default=str), booking_id), conn)


def update_booking_payload(booking_id: int, payload: dict, conn: sqlite3.Connection | None = None) -> None:
    _run("UPDATE bookings SET payload = ? WHERE booking_id = ?", (json.dumps(payload, default=str), booking_id), conn)


def remove_booking(booking_id: int, conn: sqlite3.Connection | None = None) -> int:
    return _run("DELETE FROM bookings WHERE booking_id = ?", (booking_id,), conn)


# --------------------------------------------------------------------------- #
# Settings (owner preferences: home-airport costs, later memberships)
# --------------------------------------------------------------------------- #


def get_setting(key: str, default=None, conn: sqlite3.Connection | None = None):
    rows = _fetch("SELECT value FROM settings WHERE key = ?", (key,), conn)
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key: str, value, conn: sqlite3.Connection | None = None) -> None:
    _run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
         (key, json.dumps(value)), conn)


def memberships(conn: sqlite3.Connection | None = None) -> dict[str, bool]:
    """Which airline programs the owner belongs to (settings over config defaults)."""
    stored = get_setting("memberships", {}, conn) or {}
    return {code: bool(stored.get(code, default)) for code, default in config.DEFAULT_MEMBERSHIPS.items()}


def reprice(airline: str, is_member: bool, conn: sqlite3.Connection | None = None) -> int:
    """Recompute `price` (what the owner pays) for one airline from the two stored fares.

    Mirrors airlines.owner_price: a member takes the program fare, anyone else the public fare, and a row that
    knows only one fare uses it either way. `is_member_fare` is set only when the price is the program fare.
    """
    if is_member:
        sql = """UPDATE avelo_flights
                 SET price = COALESCE(member_price, base_price), is_member_fare = (member_price IS NOT NULL)
                 WHERE airline = ? AND COALESCE(member_price, base_price) IS NOT NULL"""
    else:
        sql = """UPDATE avelo_flights
                 SET price = COALESCE(base_price, member_price), is_member_fare = 0
                 WHERE airline = ? AND COALESCE(base_price, member_price) IS NOT NULL"""
    return _run(sql, (airline.upper(),), conn)


def _trip_airline(key: str) -> str | None:
    try:
        return trips.parse(key).airline
    except ValueError:
        return None


def reset_baselines(airline: str, conn: sqlite3.Connection | None = None) -> None:
    """Forget what earlier runs saw of one airline, so a change of price basis raises no false signal.

    Signals compare a run with the run before it, and a route floor or a watched package total is a fare seen
    earlier; all of them were priced under the old membership. Drops the airline's route_day_seen rows and the
    floors of the routes they covered (route_floor has no airline column, so a route two airlines fly loses its
    floor too, and the next run sets it again), and the stored total of package watches with a leg on this airline.
    """
    code = airline.upper()
    routes = _fetch("""SELECT DISTINCT s.origin, s.destination FROM route_day_seen s
                       JOIN scrape_runs r ON r.run_id = s.run_id WHERE r.airline = ?""", (code,), conn)
    _run("DELETE FROM route_floor WHERE origin = ? AND destination = ?",
         [(r["origin"], r["destination"]) for r in routes], conn, many=True)
    _run("DELETE FROM route_day_seen WHERE run_id IN (SELECT run_id FROM scrape_runs WHERE airline = ?)", (code,), conn)
    for watch in list_watches("package", conn):
        legs = [trips.payload_trip(watch["payload"], leg) for leg in ("out", "in")]
        if "last_total" in watch["state"] and any(k and _trip_airline(k) == code for k in legs):
            state = {k: v for k, v in watch["state"].items() if k != "last_total"}
            update_watch_state(watch["watch_id"], state, conn)


def set_membership(airline: str, is_member: bool, conn: sqlite3.Connection | None = None) -> None:
    """Record whether the owner is in an airline's program and reprice its stored fares.

    A change of basis makes the airline's earlier sightings (route_day_seen) incomparable with the next scrape,
    so they are dropped when the value actually changes: the next run then has no baseline and signals nothing.
    The route floors are cleared too, as is the stored package total of watches, see `reset_baselines`.
    """
    code = airline.upper()
    stored = get_setting("memberships", {}, conn) or {}
    changed = memberships(conn).get(code) != bool(is_member)
    stored[code] = bool(is_member)
    set_setting("memberships", stored, conn)
    reprice(code, is_member, conn)
    if changed:
        reset_baselines(code, conn)


def home_costs(conn: sqlite3.Connection | None = None) -> dict[str, config.HomeCost]:
    """Per home airport: owner-set drive minutes and parking $/day over the defaults."""
    stored = get_setting("home_costs", {}, conn) or {}
    out: dict[str, config.HomeCost] = {}
    for code in config.HOME_AIRPORT_INFO:
        default = config.DEFAULT_HOME_COSTS.get(code, config.HomeCost())
        mine = stored.get(code, {})
        drive = mine.get("drive_min", default.drive_min)
        out[code] = config.HomeCost(
            float(drive) if drive is not None else None,
            float(mine.get("parking_per_day", default.parking_per_day)),
        )
    return out


def set_home_cost(code: str, drive_min: float | None, parking_per_day: float,
                  conn: sqlite3.Connection | None = None) -> None:
    stored = get_setting("home_costs", {}, conn) or {}
    stored[code.upper()] = {"drive_min": drive_min, "parking_per_day": float(parking_per_day)}
    set_setting("home_costs", stored, conn)


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
                     WHERE airline = ? AND last_seen >= ? AND source <> 'search'
                     ORDER BY origin, destination""", (airline.upper(), cutoff), conn)
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
    """When this airline's routes from `origin` were last confirmed by a listing (None = never).
    Rows created by a nonstop probe (source 'search') are not listings and are ignored."""
    rows = _fetch("SELECT MAX(last_seen) AS seen FROM route_catalog WHERE airline = ? AND origin = ? AND source <> 'search'",
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
    """Record a nonstop probe result. A route not yet catalogued is created with source 'search',
    which `catalog_routes` and `catalog_checked` ignore until a route-page listing upserts it."""
    stamp = _now(now)
    _run("""INSERT INTO route_catalog (airline, origin, destination, first_seen, last_seen, source, nonstop, nonstop_checked)
            VALUES (?, ?, ?, ?, ?, 'search', ?, ?)
            ON CONFLICT (airline, origin, destination) DO UPDATE SET nonstop = excluded.nonstop, nonstop_checked = excluded.nonstop_checked""",
         (airline.upper(), origin.upper(), destination.upper(), stamp, stamp, 1 if nonstop else 0, stamp), conn)
