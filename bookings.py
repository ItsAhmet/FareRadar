"""Booked-trip tracker and the Avelo PLUS ROI meter.

A booking stores the package key plus the fares (member and public) at the
moment you booked. After every scrape the current total is compared with what
you paid: "beat the market" if fares rose, an alert if they fell enough that
rebooking might be worth it. The ROI meter totals member savings across
booked legs against the annual membership cost.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import config
import database
from optimizer import Itinerary
from signals import Event


def booking_payload(it: Itinerary) -> dict:
    from alerts import package_key  # local import: alerts imports optimizer, not us

    key = package_key(it)
    key.update(
        {
            "out_price": it.outbound.price,
            "out_base": it.outbound.base_price,
            "in_price": it.inbound.price,
            "in_base": it.inbound.base_price,
            "member_fare": it.member_fare,
            "destination_city": it.destination_city,
            "out_date": it.outbound.departure_date.isoformat(),
            "in_date": it.inbound.departure_date.isoformat(),
        }
    )
    return key


def fare_savings(payload: dict) -> float:
    """Member fare savings for the two legs at booking time (0 if unknown)."""
    total = 0.0
    for leg in ("out", "in"):
        base, price = payload.get(f"{leg}_base"), payload.get(f"{leg}_price")
        if base is not None and price is not None:
            total += max(float(base) - float(price), 0.0)
    return round(total, 2)


def current_total(payload: dict, conn: sqlite3.Connection | None = None) -> float | None:
    out = database.get_flight(payload["out_id"], datetime.fromisoformat(payload["out_dep"]), conn)
    back = database.get_flight(payload["in_id"], datetime.fromisoformat(payload["in_dep"]), conn)
    if out is None or back is None:
        return None
    return round(out.price + back.price, 2)


def evaluate_bookings(conn: sqlite3.Connection | None = None) -> list[Event]:
    """Compare each booking with today's fares; alert on meaningful drops once."""
    events: list[Event] = []
    for b in database.list_bookings(conn):
        state = dict(b["state"])
        now_total = current_total(b["payload"], conn)
        if now_total is None:
            state["current_total"] = None
            database.update_booking_state(b["booking_id"], state, conn)
            continue
        paid = float(b["price_paid"])
        drop = round(paid - now_total, 2)
        alerted_at = state.get("alerted_total")
        if drop >= config.BOOKED_DROP_ALERT_USD and (alerted_at is None or now_total < alerted_at - 0.5):
            events.append(Event("booked_drop", None, b["payload"].get("destination"), None,
                                f"Booked {b['label']} is now ${now_total:.0f}, you paid ${paid:.0f} — ${drop:.0f} cheaper; consider rebooking",
                                {"booking_id": b["booking_id"], "paid": paid, "now": now_total}))
            state["alerted_total"] = now_total
        state.update({
            "current_total": now_total,
            "delta": round(now_total - paid, 2),  # positive = you beat the market
            "lowest_seen": min(now_total, state.get("lowest_seen", now_total)),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        database.update_booking_state(b["booking_id"], state, conn)
    return events


def plus_roi(conn: sqlite3.Connection | None = None) -> dict:
    """Membership value from booked trips: fare savings plus the value of the
    free carry-on and seat selection on every booked leg."""
    bookings = database.list_bookings(conn)
    fare = round(sum(fare_savings(b["payload"]) for b in bookings), 2)
    perks = round(len(bookings) * 2 * config.PLUS_VALUE_PER_LEG, 2)
    total = round(fare + perks, 2)
    cost = config.PLUS_ANNUAL_COST
    return {
        "bookings": len(bookings),
        "fare_savings": fare,
        "perk_value": perks,
        "total_value": total,
        "annual_cost": cost,
        "paid_off": cost > 0 and total >= cost,
        "ratio": round(total / cost, 2) if cost > 0 else None,
    }
