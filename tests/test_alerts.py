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


WINDOW = Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))


def test_package_label_names_the_home_airport():
    outs = [f("ILG", "TPA", 8, 8, 60), f("PHL", "TPA", 8, 8, 60)]
    backs = [f("TPA", "ILG", 11, 15, 50), f("TPA", "PHL", 11, 15, 50)]
    its = build_itineraries(outs, backs, GetawayQuery({"TPA"}, [WINDOW], origins=["ILG", "PHL"]))
    labels = {it.home_label: alerts.package_label(it) for it in its}
    assert set(labels) == {"ILG", "PHL"}
    assert labels["ILG"] != labels["PHL"]
    assert all(home in label for home, label in labels.items())


def test_open_jaw_package_label_shows_both_homes():
    outs = [f("ILG", "TPA", 8, 8, 60)]
    backs = [f("TPA", "ILG", 11, 15, 50), f("TPA", "PHL", 11, 15, 50)]
    its = build_itineraries(outs, backs, GetawayQuery({"TPA"}, [WINDOW], origins=["ILG", "PHL"], allow_open_jaw=True))
    labels = {it.home_label: alerts.package_label(it) for it in its}
    assert set(labels) == {"ILG", "ILG\u2192PHL"}
    assert "ILG\u2192PHL" in labels["ILG\u2192PHL"]
    assert labels["ILG"] != labels["ILG\u2192PHL"]


def test_rule_push_messages_tell_home_airports_apart(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 60), f("TPA", "ILG", 11, 15, 50),
                       f("PHL", "TPA", 8, 8, 60), f("TPA", "PHL", 11, 15, 50)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 500, True, 100, span_days=14)
    db.add_watch("rule", "Tampa cheap", payload, conn)
    events = [e for e in alerts.evaluate_rules(conn, today=date(2026, 10, 1)) if e.kind == "rule_match"]
    assert {e.origin for e in events} == {"ILG", "PHL"}
    assert len({e.message for e in events}) == 2
    for e in events:
        assert e.origin in e.message


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


def cal(origin, dest, day, price):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day), price=price,
                  flight_id=f"LOWFARE-{origin}-{dest}")


def test_watch_survives_calendar_to_detail_upgrade(conn):
    out, back = cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    wid, created = alerts.watch_package(it, conn)
    assert created
    db.update_watch_state(wid, {"last_total": 180.0}, conn)
    # detail run: placeholders replaced by timed rows at the same price
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.delete_day("TPA", "ILG", date(2026, 10, 11), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 8, 19, 100), f("TPA", "ILG", 11, 15, 80)], conn)
    assert alerts.evaluate_packages(conn) == []
    payload = db.list_watches("package", conn)[0]["payload"]
    assert payload["out_trip"] == "XP|ILG|TPA|2026-10-08|19:00" and payload["in_trip"] == "XP|TPA|ILG|2026-10-11|15:00"


def test_watch_package_refuses_duplicates(conn):
    out, back = f("ILG", "TPA", 8, 19, 100), f("TPA", "ILG", 11, 15, 80)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    first, created1 = alerts.watch_package(it, conn)
    second, created2 = alerts.watch_package(it, conn)
    assert created1 and not created2 and first == second
    assert len(db.list_watches("package", conn)) == 1


def test_legacy_package_watch_still_resolves(conn):
    db.upsert_flights([cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)], conn)
    legacy = {"out_id": "LOWFARE-ILG-TPA", "out_dep": "2026-10-08T00:00", "in_id": "LOWFARE-TPA-ILG",
              "in_dep": "2026-10-11T00:00", "window_start": "2026-10-07T18:00", "destination": "TPA"}
    wid = db.add_watch("package", "old", legacy, conn)
    db.update_watch_state(wid, {"last_total": 200.0}, conn)
    events = alerts.evaluate_packages(conn)
    assert [e.kind for e in events] == ["price_drop"]


def test_rule_does_not_rematch_after_upgrade_or_on_legacy_state(conn):
    db.upsert_flights([cal("ILG", "TPA", 8, 100), cal("TPA", "ILG", 11, 80)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                  span_days=14, origins=["ILG"])
    assert payload["origins"] == ["ILG"] and payload["allow_open_jaw"] is False
    db.add_watch("rule", "Tampa", payload, conn)
    today = date(2026, 10, 1)
    assert [e.kind for e in alerts.evaluate_rules(conn, today=today)] == ["rule_match"]
    db.delete_day("ILG", "TPA", date(2026, 10, 8), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 8, 19, 100)], conn)
    assert alerts.evaluate_rules(conn, today=today) == []          # same trip, now with a time

    legacy_id = db.add_watch("rule", "Legacy", payload, conn)
    db.update_watch_state(legacy_id, {"matched": ["LOWFARE-ILG-TPA|2026-10-08T00:00|x|y|z"]}, conn)
    assert alerts.evaluate_rules(conn, today=today) == []          # legacy state: baseline silently
    assert db.list_watches("rule", conn)[1]["state"]["key_version"] == 2


def test_rule_origins_reach_the_query_and_watched_routes(conn):
    payload = alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                  span_days=14, origins=["ILG", "PHL"], allow_open_jaw=True)
    q = alerts.rule_query(payload, today=date(2026, 10, 1))
    assert q.origins == ["ILG", "PHL"] and q.allow_open_jaw
    old = {k: v for k, v in payload.items() if k not in ("origins", "allow_open_jaw")}
    assert alerts.rule_query(old, today=date(2026, 10, 1)).origins == [config.PRIMARY_HOME]
    db.add_watch("rule", "r", payload, conn)
    assert alerts.watched_routes(conn) >= {("ILG", "TPA"), ("TPA", "ILG"), ("PHL", "TPA"), ("TPA", "PHL")}


def test_rule_match_quotes_the_cheapest_flight_of_the_day(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 100, "AVELO-ILG-TPA-AM"), f("ILG", "TPA", 8, 19, 150, "AVELO-ILG-TPA-PM"),
                       f("TPA", "ILG", 11, 15, 80)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(6), 6, time(23, 59)), 1, 500, True, 100,
                                  span_days=14, origins=["ILG"])
    db.add_watch("rule", "Tampa", payload, conn)
    events = alerts.evaluate_rules(conn, today=date(2026, 10, 1))
    assert [e.kind for e in events] == ["rule_match"] and events[0].message.endswith("for $180")
