from datetime import date, datetime, time

import pytest

import alerts
import config
import database as db
from models import Flight
from optimizer import Availability, GetawayQuery, Window, build_itineraries
from signals import Event


def f(origin, dest, day, hour, price, fid=None):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour), price=price,
                  duration_minutes=150, flight_id=fid or f"AVELO-{origin}-{dest}")


@pytest.fixture
def conn():
    c = db.get_connection(":memory:")
    yield c
    c.close()


def test_rule_payload_round_trips_into_a_query():
    av = Availability(2, time(18, 0), 6, time(23, 59))
    payload = alerts.rule_payload(["tpa", "LAL"], av, 2, 300, True, 100, span_days=30)
    q = alerts.rule_query(payload, today=date(2026, 10, 1))
    assert q is not None
    assert q.destinations == {"TPA", "LAL"} and q.max_budget == 300 and q.min_nights == 2
    assert q.windows[0].start == datetime(2026, 10, 7, 18, 0)
    assert alerts.rule_query({**payload, "destinations": []}, today=date(2026, 10, 1)) is None


def test_watched_package_alerts_on_drop_and_on_disappearance(conn):
    out, back = f("ILG", "TPA", 8, 8, 100), f("TPA", "ILG", 11, 15, 80)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))]))
    wid = db.add_watch("package", alerts.package_label(it), alerts.package_key(it), conn)
    db.update_watch_state(wid, {"last_total": 180.0}, conn)

    assert alerts.evaluate_packages(conn) == []  # unchanged

    db.upsert_flights([f("ILG", "TPA", 8, 8, 60)], conn)  # outbound fell $40
    events = alerts.evaluate_packages(conn)
    assert [e.kind for e in events] == ["price_drop"] and "dropped $40 to $140" in events[0].message

    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    events = alerts.evaluate_packages(conn)
    assert [e.kind for e in events] == ["watch_unavailable"]
    assert alerts.evaluate_packages(conn) == []  # only reported once


def test_rule_alerts_only_on_new_matches(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 100), f("TPA", "ILG", 11, 15, 80)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100, span_days=14)
    db.add_watch("rule", "Tampa cheap", payload, conn)
    today = date(2026, 10, 1)

    first = alerts.evaluate_rules(conn, today=today)
    assert [e.kind for e in first] == ["rule_match"] and "Tampa cheap" in first[0].message
    assert alerts.evaluate_rules(conn, today=today) == []  # same match, no repeat

    db.upsert_flights([f("ILG", "LAL", 9, 8, 50, "AVELO-ILG-LAL"), f("LAL", "ILG", 11, 12, 40, "AVELO-LAL-ILG")], conn)
    db.add_watch("rule", "Anywhere", alerts.rule_payload(["TPA", "LAL"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100, 14), conn)
    events = alerts.evaluate_rules(conn, today=today)
    assert all(e.kind == "rule_match" for e in events) and any("Anywhere" in e.message for e in events)
    assert alerts.watched_routes(conn) >= {("ILG", "TPA"), ("TPA", "ILG"), ("ILG", "LAL")}


def test_select_pushes_filters_route_signals_to_watched_routes():
    events = [
        Event("sale_wave", "ILG", "TPA", None, "sale"),
        Event("sale_wave", "ILG", "SJU", None, "sale"),
        Event("rule_match", None, None, None, "match"),
        Event("floor_fare", "ILG", "TPA", None, "quiet", notify=False),
    ]
    pushes = alerts.select_pushes(events, {("ILG", "TPA")})
    assert [e.message for e in pushes] == ["sale", "match"]
    title, body = alerts.build_message(pushes)
    assert title == "New Avelo getaway matches" and body.count("•") == 2


def test_delivery_is_noop_when_unconfigured_or_offline(monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    assert alerts.deliver([Event("price_drop", "ILG", "TPA", None, "x")]).ntfy_ok is None
    monkeypatch.setattr(config, "NTFY_TOPIC", "some-topic")
    monkeypatch.setattr(config, "OFFLINE", True)
    assert alerts.notify_ntfy("t", "b") is False
