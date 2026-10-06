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

import config
import database
from optimizer import Availability, GetawayQuery, Itinerary, find_itineraries, rank_by_price
from netutil import open_url
from signals import Event

log = logging.getLogger(__name__)

ROUTE_SIGNALS = {"sale_wave", "sold_out", "floor_gone", "new_floor"}


# --------------------------------------------------------------------------- #
# Keys and payloads
# --------------------------------------------------------------------------- #


def package_key(it: Itinerary) -> dict:
    return {
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
    on the same dates never read alike."""
    return (f"{it.home_label} · {it.destination_city} · {it.route_mix}"
            f" · out {it.outbound.departure_date:%a %b %d}, back {it.inbound.departure_date:%a %b %d}")


def rule_payload(
    destinations: list[str],
    availability: Availability,
    min_nights: int,
    max_budget: float,
    allow_mix: bool,
    max_pair_miles: float,
    span_days: int = 60,
    holiday_extend: bool = False,
) -> dict:
    return {
        "holiday_extend": holiday_extend,
        "destinations": sorted(d.upper() for d in destinations),
        "start_weekday": availability.start_weekday,
        "start_time": availability.start_time.strftime("%H:%M"),
        "end_weekday": availability.end_weekday,
        "end_time": availability.end_time.strftime("%H:%M"),
        "min_nights": min_nights,
        "max_budget": max_budget,
        "allow_mix": allow_mix,
        "max_pair_miles": max_pair_miles,
        "span_days": span_days,
    }


def rule_query(payload: dict, today: date | None = None) -> GetawayQuery | None:
    today = today or date.today()
    av = Availability(
        payload["start_weekday"], time.fromisoformat(payload["start_time"]),
        payload["end_weekday"], time.fromisoformat(payload["end_time"]),
    )
    windows = av.windows(today + timedelta(days=1), today + timedelta(days=int(payload.get("span_days", 60))),
                         holiday_extend=bool(payload.get("holiday_extend", False)))
    if not windows or not payload["destinations"]:
        return None
    return GetawayQuery(
        destinations=set(payload["destinations"]),
        windows=windows,
        min_nights=int(payload["min_nights"]),
        max_budget=float(payload["max_budget"]),
        allow_mix=bool(payload["allow_mix"]),
        max_pair_miles=float(payload["max_pair_miles"]),
    )


def _key_str(key: dict) -> str:
    return "|".join(str(key[k]) for k in ("out_id", "out_dep", "in_id", "in_dep", "window_start"))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate_packages(conn: sqlite3.Connection | None = None) -> list[Event]:
    events: list[Event] = []
    for watch in database.list_watches("package", conn):
        key, state = watch["payload"], dict(watch["state"])
        out = database.get_flight(key["out_id"], datetime.fromisoformat(key["out_dep"]), conn)
        back = database.get_flight(key["in_id"], datetime.fromisoformat(key["in_dep"]), conn)
        if out is None or back is None:
            if not state.get("unavailable"):
                events.append(Event("watch_unavailable", None, key.get("destination"), None,
                                    f"Watched package {watch['label']} is no longer available",
                                    {"watch_id": watch["watch_id"]}))
            state["unavailable"] = True
            database.update_watch_state(watch["watch_id"], state, conn)
            continue
        total = round(out.price + back.price, 2)
        last = state.get("last_total")
        if last is not None:
            drop = last - total
            if drop >= config.ALERT_MIN_DROP_USD or (last and drop / last >= config.ALERT_MIN_DROP_PCT):
                events.append(Event("price_drop", out.origin, out.destination, out.departure_date,
                                    f"Watched: {watch['label']} dropped ${drop:.0f} to ${total:.0f}",
                                    {"watch_id": watch["watch_id"], "from": last, "to": total}))
            elif total - last >= config.ALERT_MIN_DROP_USD:
                events.append(Event("price_rise", out.origin, out.destination, out.departure_date,
                                    f"Watched: {watch['label']} rose ${total - last:.0f} to ${total:.0f}",
                                    {"watch_id": watch["watch_id"], "from": last, "to": total}, notify=False))
        state.update({"last_total": total, "unavailable": False, "checked_at": datetime.now().isoformat(timespec="seconds")})
        database.update_watch_state(watch["watch_id"], state, conn)
    return events


def evaluate_rules(conn: sqlite3.Connection | None = None, today: date | None = None, max_new: int = 3) -> list[Event]:
    events: list[Event] = []
    for watch in database.list_watches("rule", conn):
        query = rule_query(watch["payload"], today)
        state = dict(watch["state"])
        if query is None:
            continue
        matches = rank_by_price(find_itineraries(query, conn))
        keys = {_key_str(package_key(it)): it for it in matches}
        previous = set(state.get("matched", []))
        new_keys = [k for k in keys if k not in previous]
        for k in new_keys[:max_new]:
            it = keys[k]
            events.append(Event("rule_match", it.outbound.origin, it.outbound.destination, it.outbound.departure_date,
                                f"Rule '{watch['label']}': {package_label(it)} for ${it.total_price:.0f}",
                                {"watch_id": watch["watch_id"], "package": package_key(it), "total": it.total_price}))
        if len(new_keys) > max_new:
            events.append(Event("rule_match", None, None, None,
                                f"Rule '{watch['label']}': {len(new_keys) - max_new} more new matches in the app",
                                {"watch_id": watch["watch_id"], "extra": len(new_keys) - max_new}))
        state.update({"matched": sorted(keys), "checked_at": datetime.now().isoformat(timespec="seconds"),
                      "match_count": len(keys)})
        database.update_watch_state(watch["watch_id"], state, conn)
    return events


def watched_routes(conn: sqlite3.Connection | None = None) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for watch in database.list_watches(conn=conn):
        p = watch["payload"]
        if watch["kind"] == "rule":
            for d in p.get("destinations", []):
                routes.add((config.PRIMARY_HOME, d))
                routes.add((d, config.PRIMARY_HOME))
        else:
            out_id, in_id = p.get("out_id", ""), p.get("in_id", "")
            for fid in (out_id, in_id):
                parts = fid.split("-")
                if len(parts) >= 3:
                    routes.add((parts[-2], parts[-1]))
    return routes


def select_pushes(events: list[Event], watched: set[tuple[str, str]]) -> list[Event]:
    """Which events deserve a push: watch-driven ones always, route signals
    only on routes somebody is watching."""
    pushes = []
    for e in events:
        if not e.notify:
            continue
        if e.kind in ROUTE_SIGNALS:
            if (e.origin, e.destination) in watched:
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


def build_message(events: list[Event]) -> tuple[str, str]:
    kinds = {e.kind for e in events}
    if "price_drop" in kinds:
        title = "Avelo fare dropped"
    elif "rule_match" in kinds:
        title = "New Avelo getaway matches"
    elif "sale_wave" in kinds:
        title = "Avelo sale on a watched route"
    else:
        title = "Avelo fare update"
    body = "\n".join(f"• {e.message}" for e in events[:12])
    if len(events) > 12:
        body += f"\n… and {len(events) - 12} more"
    return title, body


def notify_ntfy(title: str, body: str, topic: str | None = None, server: str | None = None) -> bool:
    topic = topic or config.NTFY_TOPIC
    if not topic or config.OFFLINE:
        return False
    url = f"{server or config.NTFY_SERVER}/{topic}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Title": title, "Tags": "airplane", "Priority": "default", "Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with open_url(req, config.NOTIFY_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("ntfy delivery failed: %s", exc)
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
    title, body = build_message(events)
    if config.NTFY_TOPIC:
        result.ntfy_ok = notify_ntfy(title, body)
    if config.SMTP_HOST and config.SMTP_TO:
        result.email_ok = notify_email(title, body)
    return result


def evaluate_and_deliver(run_id: int | None, signal_events: list[Event], conn: sqlite3.Connection | None = None) -> dict:
    """Run after signal detection: evaluate watches, store their events, push."""
    from bookings import evaluate_bookings  # local import avoids a cycle

    watch_events = evaluate_packages(conn) + evaluate_rules(conn) + evaluate_bookings(conn)
    database.add_events(run_id, watch_events, conn)
    pushes = select_pushes(signal_events + watch_events, watched_routes(conn))
    delivery = deliver(pushes)
    summary = {
        "watch_events": len(watch_events),
        "pushed": delivery.pushed,
        "ntfy": delivery.ntfy_ok,
        "email": delivery.email_ok,
    }
    log.info("alerts: %s", json.dumps(summary))
    return summary
