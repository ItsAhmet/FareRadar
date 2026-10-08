"""Watchlist evaluation and notification delivery.

Two kinds of watch live in the `watchlist` table:

package  a specific outbound+inbound pair. Alerts when its total drops by
         ALERT_MIN_DROP_USD or ALERT_MIN_DROP_PCT, or when a leg disappears.
rule     a saved set of sidebar filters. Alerts when a package matches that
         did not match at the previous evaluation.

Route-level signals (sale waves, sold-out days, floor changes) are pushed
only for routes that some watch cares about; everything still lands in the
in-app feed. Delivery: ntfy.sh (topic in config) and/or SMTP email.
"""
from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage

import airlines as airline_registry
import config
import database
import trips
from models import Flight
from optimizer import (Availability, GetawayQuery, Itinerary, Window, find_itineraries, inbound_fits,
                       intersect_windows, outbound_fits, rank_by_price)
from netutil import open_url
from signals import Event

log = logging.getLogger(__name__)

ROUTE_SIGNALS = {"sale_wave", "sold_out", "floor_gone", "new_floor"}


@dataclass(frozen=True)
class StateWrite:
    """A watch/booking state update waiting for its alert to be delivered."""

    kind: str        # "watch" | "booking"
    row_id: int
    state: dict
    alerting: bool   # True when this state records an alert the owner must actually receive first
    events: tuple[Event, ...] = ()   # the events this state accounts for; held back together with an alerting write


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


# --------------------------------------------------------------------------- #
# Keys and payloads
# --------------------------------------------------------------------------- #


def package_key(it: Itinerary) -> dict:
    return {
        "out_trip": trips.trip_key(it.outbound),
        "in_trip": trips.trip_key(it.inbound),
        "out_day": trips.day_key(it.outbound),
        "in_day": trips.day_key(it.inbound),
        # legacy fields, still read by the dashboard and by old payload readers
        "out_id": it.outbound.flight_id,
        "out_dep": it.outbound.departure_datetime.isoformat(timespec="minutes"),
        "in_id": it.inbound.flight_id,
        "in_dep": it.inbound.departure_datetime.isoformat(timespec="minutes"),
        "window_start": it.window.start.isoformat(timespec="minutes"),
        "window_end": it.window.end.isoformat(timespec="minutes"),
        "destination": it.destination,
        "route_mix": it.route_mix,
    }


def package_label(it: Itinerary) -> str:
    """Human label for watches, pushes and bookings. Leads with the home airport
    ('ILG', or 'ILG→PHL' for an open-jaw return) so trips from different homes
    on the same dates never read alike, and ends with the airline(s) flown."""
    return (f"{it.home_label} · {it.destination_city} · {it.route_mix}"
            f" · out {it.outbound.departure_date:%a %b %d}, back {it.inbound.departure_date:%a %b %d}"
            f" · {it.airline_label}")


def unverified_note(out: Flight, back: Flight) -> str:
    """A marker for a package total that is not a price the owner can count on: a leg is a calendar placeholder
    of an airline whose calendar price is not a bookable fare (Frontier: the lowest Discount Den total of any
    routing, so the nonstop's standard fare can be much higher). '' when both legs are bookable fares."""
    names = sorted({airline_registry.get(leg.airline or "XP").name for leg in (out, back)
                    if not leg.time_known and not airline_registry.get(leg.airline or "XP").calendar_bookable})
    return f" (unverified {' & '.join(names)} calendar price: times and fare need a detail scrape)" if names else ""


def rule_payload(
    destinations: list[str],
    availability: Availability,
    min_nights: int,
    max_budget: float,
    allow_mix: bool,
    max_pair_miles: float,
    span_days: int = 60,
    holiday_extend: bool = False,
    origins: list[str] | None = None,
    allow_open_jaw: bool = False,
    airlines: list[str] | None = None,
    partner: Availability | None = None,
    count_carry_on: bool = False,
) -> dict:
    return {
        "holiday_extend": holiday_extend,
        "destinations": sorted(d.upper() for d in destinations),
        "origins": list(dict.fromkeys(o.upper() for o in (origins or config.HOME_CODES))),
        "allow_open_jaw": bool(allow_open_jaw),
        "start_weekday": availability.start_weekday,
        "start_time": availability.start_time.strftime("%H:%M"),
        "end_weekday": availability.end_weekday,
        "end_time": availability.end_time.strftime("%H:%M"),
        "min_nights": min_nights,
        "max_budget": max_budget,
        "allow_mix": allow_mix,
        "max_pair_miles": max_pair_miles,
        "span_days": span_days,
        "airlines": sorted({a.strip().upper() for a in (airlines or config.KNOWN_AIRLINES)}),
        "count_carry_on": bool(count_carry_on),
        "partner": None if partner is None else {
            "start_weekday": partner.start_weekday, "start_time": partner.start_time.strftime("%H:%M"),
            "end_weekday": partner.end_weekday, "end_time": partner.end_time.strftime("%H:%M"),
        },
    }


def rule_query(payload: dict, today: date | None = None) -> GetawayQuery | None:
    today = today or date.today()
    av = Availability(
        payload["start_weekday"], time.fromisoformat(payload["start_time"]),
        payload["end_weekday"], time.fromisoformat(payload["end_time"]),
    )
    windows = av.windows(today + timedelta(days=1), today + timedelta(days=int(payload.get("span_days", 60))),
                         holiday_extend=bool(payload.get("holiday_extend", False)))
    p = payload.get("partner")
    if p:
        theirs = Availability(p["start_weekday"], time.fromisoformat(p["start_time"]),
                              p["end_weekday"], time.fromisoformat(p["end_time"]))
        windows = intersect_windows(windows, theirs.windows(today + timedelta(days=1),
                                                            today + timedelta(days=int(payload.get("span_days", 60))),
                                                            holiday_extend=bool(payload.get("holiday_extend", False))))
    if not windows or not payload["destinations"]:
        return None
    return GetawayQuery(
        destinations=set(payload["destinations"]),
        windows=windows,
        min_nights=int(payload["min_nights"]),
        max_budget=float(payload["max_budget"]),
        allow_mix=bool(payload["allow_mix"]),
        max_pair_miles=float(payload["max_pair_miles"]),
        origins=list(payload.get("origins") or [config.PRIMARY_HOME]),
        allow_open_jaw=bool(payload.get("allow_open_jaw", False)),
        airlines=set(payload.get("airlines") or ["XP"]),   # rules saved before airlines existed meant Avelo
        count_carry_on=bool(payload.get("count_carry_on", False)),
    )


def _key_str(key: dict) -> str:
    """Rule-match identity: day-level so a calendar→detail upgrade is not a new match."""
    return "|".join(str(key[k]) for k in ("out_day", "in_day", "window_start"))


def watch_package(it: Itinerary, conn: sqlite3.Connection | None = None) -> tuple[int, bool]:
    """Watch a package unless the same trip is already watched. Returns (watch_id, created)."""
    key = package_key(it)
    for w in database.list_watches("package", conn):
        p = w["payload"]
        out_k, in_k = trips.payload_trip(p, "out"), trips.payload_trip(p, "in")
        if (out_k and in_k and p.get("window_start") == key["window_start"]
                and trips.matches(out_k, key["out_trip"]) and trips.matches(in_k, key["in_trip"])):
            return w["watch_id"], False
    watch_id = database.add_watch("package", package_label(it), key, conn)
    # Fares only: evaluate_packages compares against out.price + back.price, so seeding with
    # total_price would read the carry-on fees as a price drop when carry-on counting is on.
    database.update_watch_state(watch_id, {"last_total": it.fare_total}, conn)
    return watch_id, True


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _day_only(key: str | None) -> bool:
    return key is not None and trips.parse(key).time == trips.ANY_TIME


def _payload_window(payload: dict) -> Window:
    """The free window a package was built for; a bound the payload lacks stays open."""
    start, end = payload.get("window_start"), payload.get("window_end")
    return Window(datetime.fromisoformat(start) if start else datetime.min,
                  datetime.fromisoformat(end) if end else datetime.max)


def package_legs(payload: dict, conn: sqlite3.Connection | None = None) -> tuple[Flight | None, Flight | None]:
    """(outbound, inbound) a watched or booked package refers to now.

    A timed leg is that exact flight. A day-only leg (saved from a calendar
    run) only resolves to a flight that still fits the package's window by the
    optimizer's own test: the outbound leaves once the window opens, the return
    leaves after the outbound lands and is home before the window ends. The
    cheapest fitting pair wins. With no fitting pair the inbound is None (and
    the outbound too if none of its flights fits), so the package reads as
    unavailable instead of following a flight the owner cannot take.
    """
    out_k, in_k = trips.payload_trip(payload, "out"), trips.payload_trip(payload, "in")
    outs = database.trip_candidates(out_k, conn) if out_k else []
    backs = database.trip_candidates(in_k, conn) if in_k else []
    window = _payload_window(payload)
    if _day_only(out_k):
        outs = [o for o in outs if outbound_fits(o, window)]
    pairs = [(o, b) for o in outs for b in backs if not _day_only(in_k) or inbound_fits(b, o, window)]
    if pairs:
        return min(pairs, key=lambda pair: pair[0].price + pair[1].price)
    return (outs[0] if outs else None), None


def _leg_airlines(out: Flight, back: Flight) -> list[str]:
    """Airline codes of a package's two legs, each once: a mixed package's alert names both."""
    return sorted({(leg.airline or "XP").upper() for leg in (out, back)})


def pending_packages(conn: sqlite3.Connection | None = None) -> tuple[list[Event], list[StateWrite]]:
    events: list[Event] = []
    writes: list[StateWrite] = []
    for watch in database.list_watches("package", conn):
        key, state = watch["payload"], dict(watch["state"])
        out_k, in_k = trips.payload_trip(key, "out"), trips.payload_trip(key, "in")
        out, back = package_legs(key, conn)
        if out is None or back is None:
            alerting = not state.get("unavailable")
            mine: list[Event] = []
            if alerting:
                mine.append(Event("watch_unavailable", None, key.get("destination"), None,
                                  f"Watched package {watch['label']} is no longer available",
                                  {"watch_id": watch["watch_id"]}))
            events += mine
            writes.append(StateWrite("watch", watch["watch_id"], {**state, "unavailable": True}, alerting, tuple(mine)))
            continue
        total = round(out.price + back.price, 2)
        base = state.get("alert_base", state.get("last_total"))   # what the owner was last told (or first saw)
        alerting = False
        mine = []
        flown = _leg_airlines(out, back)
        if base is None or "last_total" not in state:
            # Never priced, or the stored total was dropped because the price basis changed
            # (database.reset_baselines): start again from today's total without alerting.
            base = total
        else:
            drop = base - total
            if drop >= config.ALERT_MIN_DROP_USD or (base and drop / base >= config.ALERT_MIN_DROP_PCT):
                mine.append(Event("price_drop", out.origin, out.destination, out.departure_date,
                                  f"Watched: {watch['label']} dropped ${drop:.0f} to ${total:.0f}{unverified_note(out, back)}",
                                  {"watch_id": watch["watch_id"], "from": base, "to": total, "airlines": flown},
                                  airline=out.airline))
                base, alerting = total, True
            elif total - base >= config.ALERT_MIN_DROP_USD:
                mine.append(Event("price_rise", out.origin, out.destination, out.departure_date,
                                  f"Watched: {watch['label']} rose ${total - base:.0f} to ${total:.0f}",
                                  {"watch_id": watch["watch_id"], "from": base, "to": total, "airlines": flown},
                                  notify=False, airline=out.airline))
                base = total                                         # later drops are measured from the new level
        events += mine
        upgraded = {**key, "out_trip": trips.upgrade(out_k, out), "in_trip": trips.upgrade(in_k, back)}
        if upgraded != key:
            database.update_watch_payload(watch["watch_id"], upgraded, conn)
        writes.append(StateWrite("watch", watch["watch_id"],
                                 {**state, "last_total": total, "alert_base": base, "unavailable": False,
                                  "checked_at": datetime.now().isoformat(timespec="seconds")}, alerting, tuple(mine)))
    return events, writes


def evaluate_packages(conn: sqlite3.Connection | None = None) -> list[Event]:
    events, writes = pending_packages(conn)
    apply_writes(writes, True, conn)
    return events


def pending_rules(conn: sqlite3.Connection | None = None, today: date | None = None,
                  max_new: int = 3) -> tuple[list[Event], list[StateWrite]]:
    events: list[Event] = []
    writes: list[StateWrite] = []
    for watch in database.list_watches("rule", conn):
        query = rule_query(watch["payload"], today)
        state = dict(watch["state"])
        if query is None:
            continue
        matches = rank_by_price(find_itineraries(query, conn))
        keys: dict[str, Itinerary] = {}
        for it in matches:  # cheapest first; keys are day-level, so keep the cheapest per day pair
            keys.setdefault(_key_str(package_key(it)), it)
        legacy_state = "matched" in state and state.get("key_version") != 2
        previous = set(state.get("matched", []))
        # Rules saved before trip keys stored a different key format: adopt the
        # current matches silently instead of announcing them all as new.
        new_keys = [] if legacy_state else [k for k in keys if k not in previous]
        mine: list[Event] = []
        for k in new_keys[:max_new]:
            it = keys[k]
            note = unverified_note(it.outbound, it.inbound)
            detail = {"watch_id": watch["watch_id"], "package": package_key(it), "total": it.total_price,
                      "airlines": _leg_airlines(it.outbound, it.inbound)}
            if note:
                detail["unverified"] = True
            mine.append(Event("rule_match", it.outbound.origin, it.outbound.destination, it.outbound.departure_date,
                                f"Rule '{watch['label']}': {package_label(it)} {'from' if note else 'for'} ${it.total_price:.0f}{note}",
                                detail, airline=it.outbound.airline))
        if len(new_keys) > max_new:
            mine.append(Event("rule_match", None, None, None,
                                f"Rule '{watch['label']}': {len(new_keys) - max_new} more new matches in the app",
                                {"watch_id": watch["watch_id"], "extra": len(new_keys) - max_new}))
        events += mine
        writes.append(StateWrite("watch", watch["watch_id"],
                                 {**state, "matched": sorted(keys), "checked_at": datetime.now().isoformat(timespec="seconds"),
                                  "match_count": len(keys), "key_version": 2}, alerting=bool(new_keys), events=tuple(mine)))
    return events, writes


def evaluate_rules(conn: sqlite3.Connection | None = None, today: date | None = None, max_new: int = 3) -> list[Event]:
    events, writes = pending_rules(conn, today, max_new)
    apply_writes(writes, True, conn)
    return events


def watched_routes(conn: sqlite3.Connection | None = None) -> set[tuple[str, str, str]]:
    """Every (airline, origin, destination) some watch cares about. A rule saved
    before rules carried airlines (no "airlines" key) means Avelo only."""
    routes: set[tuple[str, str, str]] = set()
    for watch in database.list_watches(conn=conn):
        p = watch["payload"]
        if watch["kind"] == "rule":
            homes = p.get("origins") or [config.PRIMARY_HOME]
            for code in p.get("airlines") or ["XP"]:
                for d in p.get("destinations", []):
                    for home in homes:
                        routes.add((code.upper(), home, d))
                        routes.add((code.upper(), d, home))
        else:
            for leg in ("out", "in"):
                k = trips.payload_trip(p, leg)
                if k:
                    tk = trips.parse(k)
                    routes.add((tk.airline.upper(), tk.origin, tk.destination))
    return routes


def select_pushes(events: list[Event], watched: set[tuple[str, str, str]]) -> list[Event]:
    """Which events deserve a push: watch-driven ones always, route signals
    only on routes somebody is watching with that airline. A route signal
    without an airline (stored before signals carried one) is Avelo's."""
    pushes = []
    for e in events:
        if not e.notify:
            continue
        if e.kind in ROUTE_SIGNALS:
            if ((e.airline or "XP").upper(), e.origin, e.destination) in watched:
                pushes.append(e)
        else:
            pushes.append(e)
    return pushes


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


@dataclass
class Delivery:
    pushed: int = 0
    ntfy_ok: bool | None = None
    email_ok: bool | None = None

    @property
    def delivered(self) -> bool:
        """Nothing to send, no channel configured (the in-app feed is the delivery), or a channel succeeded."""
        outcomes = [o for o in (self.ntfy_ok, self.email_ok) if o is not None]
        return self.pushed == 0 or not outcomes or any(outcomes)


def build_message(events: list[Event]) -> tuple[str, str]:
    kinds = {e.kind for e in events}
    # A package event lists both legs' airlines, so a mixed package is not titled by its outbound leg alone.
    codes = sorted({c for e in events for c in ((e.detail or {}).get("airlines") or ([e.airline] if e.airline else []))})
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


def notify_ntfy(title: str, body: str, topic: str | None = None, server: str | None = None) -> bool:
    topic = topic or config.NTFY_TOPIC
    if not topic or config.OFFLINE:
        return False
    url = f"{server or config.NTFY_SERVER}/{topic}"
    try:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Title": title, "Tags": "airplane", "Priority": "default", "Content-Type": "text/plain; charset=utf-8"},
        )
        with open_url(req, config.NOTIFY_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        # the URL carries the private topic and some exception texts quote it: log the type only
        code = getattr(exc, "code", None)
        log.warning("ntfy delivery failed: %s%s", type(exc).__name__, f" (HTTP {code})" if isinstance(code, int) else "")
        return False


def notify_email(title: str, body: str) -> bool:
    if not (config.SMTP_HOST and config.SMTP_TO) or config.OFFLINE:
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = config.SMTP_USER or config.SMTP_TO
    msg["To"] = config.SMTP_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.NOTIFY_TIMEOUT_S) as smtp:
            smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASS)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("email delivery failed: %s", exc)
        return False


def deliver(events: list[Event]) -> Delivery:
    result = Delivery(pushed=len(events))
    if not events:
        return result
    if config.OFFLINE:
        return result       # pushes are switched off: no channel is in use, so the in-app feed is the delivery
    title, body = build_message(events)
    if config.NTFY_TOPIC:
        result.ntfy_ok = notify_ntfy(title, body)
    if config.SMTP_HOST and config.SMTP_TO:
        result.email_ok = notify_email(title, body)
    return result


def deliver_signals(signal_events: list[Event], conn: sqlite3.Connection | None = None) -> dict:
    """Push one finished run's route signals now, without evaluating watches.

    For a scrape of several airlines: an airline's run is finished (and becomes the next run's baseline)
    before the next airline starts, so its signals would never be detected again. Pushing them here, not
    after the last airline, keeps them when the rest of the scrape is cancelled, times out or is killed.
    """
    delivery = deliver(select_pushes(signal_events, watched_routes(conn)))
    summary = {"pushed": delivery.pushed, "ntfy": delivery.ntfy_ok, "email": delivery.email_ok}
    log.info("signal alerts: %s", json.dumps(summary))
    return summary


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
    # An event whose state write waits for a retry waits with it, so the feed shows it once, when it was
    # announced. Every other event belongs to a write that is applied now and would never be raised again.
    held = {id(e) for w in writes if w.alerting and not delivery.delivered for e in w.events}
    database.add_events(run_id, [e for e in watch_events if id(e) not in held], conn)
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
