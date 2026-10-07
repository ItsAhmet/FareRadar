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
from typing import TYPE_CHECKING

import airlines as airline_registry
import config
import database
import trips
from models import Flight
from optimizer import Itinerary
from signals import Event

if TYPE_CHECKING:
    from alerts import StateWrite


def booking_payload(it: Itinerary) -> dict:
    from alerts import package_key  # local import: alerts imports optimizer, not us

    key = package_key(it)
    key.update(
        {
            "out_price": it.outbound.price,
            "out_base": it.outbound.base_price,
            "in_price": it.inbound.price,
            "in_base": it.inbound.base_price,
            "out_airline": it.outbound.airline,
            "in_airline": it.inbound.airline,
            "out_member": it.outbound.member_price,
            "in_member": it.inbound.member_price,
            "member_fare": it.member_fare,
            "destination_city": it.destination_city,
            "out_date": it.outbound.departure_date.isoformat(),
            "in_date": it.inbound.departure_date.isoformat(),
        }
    )
    return key


def _leg_airline(payload: dict, leg: str) -> str:
    """Bookings saved before airlines existed were Avelo."""
    return (payload.get(f"{leg}_airline") or "XP").upper()


def fare_savings(payload: dict, airline: str | None = None) -> float:
    """Member-fare savings obtained on the two legs at booking time (0 if unknown), optionally one airline's legs."""
    total = 0.0
    for leg in ("out", "in"):
        if airline is not None and _leg_airline(payload, leg) != airline.upper():
            continue
        base, price = payload.get(f"{leg}_base"), payload.get(f"{leg}_price")
        if base is not None and price is not None:
            total += max(float(base) - float(price), 0.0)
    return round(total, 2)


def legs(payload: dict, conn: sqlite3.Connection | None = None) -> tuple[Flight | None, Flight | None]:
    """(outbound, inbound) flights the booking refers to now; None for a leg with
    no flight that fits the booked window (see `alerts.package_legs`)."""
    from alerts import package_legs  # local import, as in booking_payload

    return package_legs(payload, conn)


def current_total(payload: dict, conn: sqlite3.Connection | None = None) -> float | None:
    out, back = legs(payload, conn)
    if out is None or back is None:
        return None
    return round(out.price + back.price, 2)


def pending_bookings(conn: sqlite3.Connection | None = None) -> tuple[list[Event], list[StateWrite]]:
    """Compare each booking with today's fares; alert on meaningful drops once.

    Returns the events and the state each booking should get. The state that records a drop alert is
    marked alerting, so `alerts.apply_writes` stores it only once the push was delivered."""
    from alerts import StateWrite  # local import, as in booking_payload

    events: list[Event] = []
    writes: list[StateWrite] = []
    for b in database.list_bookings(conn):
        state = dict(b["state"])
        payload = b["payload"]
        out, back = legs(payload, conn)
        now_total = round(out.price + back.price, 2) if out is not None and back is not None else None
        if out is not None and back is not None:
            upgraded = {**payload, "out_trip": trips.upgrade(trips.payload_trip(payload, "out"), out),
                        "in_trip": trips.upgrade(trips.payload_trip(payload, "in"), back)}
            if upgraded != payload:
                database.update_booking_payload(b["booking_id"], upgraded, conn)
        if now_total is None:
            writes.append(StateWrite("booking", b["booking_id"], {**state, "current_total": None}, False))
            continue
        paid = float(b["price_paid"])
        drop = round(paid - now_total, 2)
        alerted_at = state.get("alerted_total")
        alerting = drop >= config.BOOKED_DROP_ALERT_USD and (alerted_at is None or now_total < alerted_at - 0.5)
        mine: list[Event] = []
        if alerting:
            mine.append(Event("booked_drop", None, b["payload"].get("destination"), None,
                              f"Booked {b['label']} is now ${now_total:.0f}, you paid ${paid:.0f} — ${drop:.0f} cheaper; consider rebooking",
                              {"booking_id": b["booking_id"], "paid": paid, "now": now_total}))
            state["alerted_total"] = now_total
        events += mine
        state.update({
            "current_total": now_total,
            "delta": round(now_total - paid, 2),  # positive = you beat the market
            "lowest_seen": min(now_total, state.get("lowest_seen", now_total)),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        writes.append(StateWrite("booking", b["booking_id"], state, alerting, tuple(mine)))
    return events, writes


def evaluate_bookings(conn: sqlite3.Connection | None = None) -> list[Event]:
    from alerts import apply_writes  # local import, as in booking_payload

    events, writes = pending_bookings(conn)
    apply_writes(writes, True, conn)
    return events


def plus_roi(conn: sqlite3.Connection | None = None) -> dict:
    """Avelo PLUS value from booked Avelo legs: fare savings plus the value of the free carry-on and seat."""
    booked = database.list_bookings(conn)
    legs = sum(1 for b in booked for leg in ("out", "in") if _leg_airline(b["payload"], leg) == "XP")
    fare = round(sum(fare_savings(b["payload"], "XP") for b in booked), 2)
    perks = round(legs * config.PLUS_VALUE_PER_LEG, 2)
    total = round(fare + perks, 2)
    cost = config.PLUS_ANNUAL_COST
    return {
        "bookings": len(booked),
        "legs": legs,
        "fare_savings": fare,
        "perk_value": perks,
        "total_value": total,
        "annual_cost": cost,
        "paid_off": cost > 0 and total >= cost,
        "ratio": round(total / cost, 2) if cost > 0 else None,
    }


def program_savings(airline: str, conn: sqlite3.Connection | None = None) -> dict:
    """What an airline's membership program would have saved on booked legs paid at the public fare."""
    code = airline.strip().upper()
    legs, saved = 0, 0.0
    for b in database.list_bookings(conn):
        p = b["payload"]
        for leg in ("out", "in"):
            if _leg_airline(p, leg) != code or f"{leg}_airline" not in p:
                continue
            legs += 1
            price, member = p.get(f"{leg}_price"), p.get(f"{leg}_member")
            if price is not None and member is not None:
                saved += max(float(price) - float(member), 0.0)
    return {"airline": code, "program": airline_registry.get(code).member_program, "legs": legs, "would_have_saved": round(saved, 2)}
