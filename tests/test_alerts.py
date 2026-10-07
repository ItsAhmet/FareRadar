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
    assert alerts.watched_routes(conn) >= {("XP", "ILG", "TPA"), ("XP", "TPA", "ILG"), ("XP", "ILG", "LAL")}


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
    pushes = alerts.select_pushes(events, {("XP", "ILG", "TPA")})
    assert [e.message for e in pushes] == ["sale", "match"]
    title, body = alerts.build_message(pushes)
    assert title == "New getaway matches" and body.count("•") == 2


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
    assert alerts.watched_routes(conn) >= {("XP", "ILG", "TPA"), ("XP", "TPA", "ILG"), ("XP", "PHL", "TPA"), ("XP", "TPA", "PHL")}


def test_rule_match_quotes_the_cheapest_flight_of_the_day(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 100, "AVELO-ILG-TPA-AM"), f("ILG", "TPA", 8, 19, 150, "AVELO-ILG-TPA-PM"),
                       f("TPA", "ILG", 11, 15, 80)], conn)
    payload = alerts.rule_payload(["TPA"], Availability(2, time(6), 6, time(23, 59)), 1, 500, True, 100,
                                  span_days=14, origins=["ILG"])
    db.add_watch("rule", "Tampa", payload, conn)
    events = alerts.evaluate_rules(conn, today=date(2026, 10, 1))
    assert [e.kind for e in events] == ["rule_match"] and events[0].message.endswith("for $180")


def _calendar_watch(conn, out_day=7, out_price=100, back_price=80):
    """Watch a calendar-only package (both legs day-only), then return its id."""
    out, back = cal("ILG", "TPA", out_day, out_price), cal("TPA", "ILG", 11, back_price)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    wid, _ = alerts.watch_package(it, conn)
    return wid


def test_day_only_watch_follows_only_flights_that_fit_its_window(conn):
    # Wed Oct 7 is the window's first day (opens 18:00). The detail run finds a
    # cheaper 07:00 the owner cannot take and a 19:00 that fits.
    _calendar_watch(conn)
    db.delete_day("ILG", "TPA", date(2026, 10, 7), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 7, 7, 60, "AVELO-ILG-TPA-AM"), f("ILG", "TPA", 7, 19, 100, "AVELO-ILG-TPA-PM")], conn)
    assert alerts.evaluate_packages(conn) == []                    # no false $40 drop
    assert db.list_watches("package", conn)[0]["payload"]["out_trip"] == "XP|ILG|TPA|2026-10-07|19:00"
    # the untakeable 07:00 selling out changes nothing
    conn.execute("DELETE FROM avelo_flights WHERE flight_id = 'AVELO-ILG-TPA-AM'")
    conn.commit()
    assert alerts.evaluate_packages(conn) == []


def test_day_only_watch_return_must_be_home_before_the_window_ends(conn):
    _calendar_watch(conn, out_day=8)
    db.delete_day("TPA", "ILG", date(2026, 10, 11), conn=conn)
    # 22:00 + 150 min lands 00:30 Monday, after the window's Sunday 23:59 end
    db.upsert_flights([f("TPA", "ILG", 11, 15, 80, "AVELO-TPA-ILG-PM"), f("TPA", "ILG", 11, 22, 50, "AVELO-TPA-ILG-LATE")], conn)
    assert alerts.evaluate_packages(conn) == []
    assert db.list_watches("package", conn)[0]["payload"]["in_trip"] == "XP|TPA|ILG|2026-10-11|15:00"


def test_day_only_watch_with_no_fitting_flight_is_unavailable_not_pinned(conn):
    wid = _calendar_watch(conn)
    db.update_watch_state(wid, {"last_total": 180.0}, conn)
    db.delete_day("ILG", "TPA", date(2026, 10, 7), conn=conn)
    db.upsert_flights([f("ILG", "TPA", 7, 7, 70)], conn)           # the day's only flight leaves before 18:00
    assert [e.kind for e in alerts.evaluate_packages(conn)] == ["watch_unavailable"]
    assert db.list_watches("package", conn)[0]["payload"]["out_trip"] == "XP|ILG|TPA|2026-10-07|*"
    # still day-only, so a fitting flight added later brings the watch back
    db.upsert_flights([f("ILG", "TPA", 7, 20, 100, "AVELO-ILG-TPA-PM")], conn)
    assert alerts.evaluate_packages(conn) == []
    assert db.list_watches("package", conn)[0]["payload"]["out_trip"] == "XP|ILG|TPA|2026-10-07|20:00"


def test_frontier_fare_drop_is_raised_as_a_frontier_sale():
    # a Frontier-only price drop on PHL-MCO is Frontier's signal: it names Frontier and carries its airline
    import signals
    from datetime import timedelta
    conn = db.get_connection(":memory:")
    db.add_watch("rule", "Orlando weekends", {"origins": ["ILG", "PHL", "BWI"], "destinations": ["MCO"]}, conn=conn)
    days = [date.today() + timedelta(days=20 + i) for i in range(6)]

    def f9_run(price):
        run = db.start_run("full", [("PHL", "MCO")], None, airline="F9", conn=conn)
        fares = [Flight(flight_id="F9-2415-PHL-MCO", origin="PHL", destination="MCO",
                        departure_datetime=datetime.combine(d, time(7)), price=price, base_price=price,
                        member_price=price - 10, airline="F9") for d in days]
        db.upsert_flights(fares, conn=conn)
        db.record_seen(run, "PHL", "MCO", days, fares, conn=conn)
        db.finish_run(run, {}, status="ok", conn=conn)
        return run

    f9_run(100.0)
    run2 = f9_run(60.0)
    events = signals.run_detection(run2, conn)
    assert "sale_wave" in {e.kind for e in events}
    assert all(e.airline == "F9" and e.message.startswith("Frontier PHL→MCO") for e in events)
    assert db.get_floor("PHL", "MCO", conn) is None and db.get_floor("PHL", "MCO", conn, airline="F9") == 60.0
    # the rule above was saved before rules carried airlines, so it is Avelo-only: Frontier's sale is not pushed
    assert alerts.select_pushes(events, alerts.watched_routes(conn)) == []
    conn.close()


def test_rule_saved_before_airlines_does_not_push_a_frontier_sale(conn):
    db.add_watch("rule", "Orlando weekends", {"origins": ["PHL"], "destinations": ["MCO"]}, conn=conn)   # no "airlines" key
    watched = alerts.watched_routes(conn)
    assert {("XP", "PHL", "MCO"), ("XP", "MCO", "PHL")} <= watched
    assert not {w for w in watched if w[0] == "F9"}
    f9_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Frontier PHL→MCO: sale wave", airline="F9")
    xp_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Avelo PHL→MCO: sale wave", airline="XP")
    assert alerts.select_pushes([f9_sale, xp_sale], watched) == [xp_sale]
    assert alerts.build_message([xp_sale])[0] == "Avelo sale on a watched route"


def test_route_signal_without_an_airline_counts_as_avelo(conn):
    db.add_watch("rule", "Orlando weekends", {"origins": ["PHL"], "destinations": ["MCO"]}, conn=conn)
    old = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "PHL→MCO: sale wave")                    # airline=None
    assert alerts.select_pushes([old], alerts.watched_routes(conn)) == [old]
    assert alerts.select_pushes([old], {("F9", "PHL", "MCO")}) == []


def test_route_signals_are_pushed_only_for_the_watched_airline(conn):
    db.add_watch("rule", "Frontier Orlando", {"origins": ["PHL"], "destinations": ["MCO"], "airlines": ["F9"]}, conn=conn)
    watched = alerts.watched_routes(conn)
    assert ("F9", "PHL", "MCO") in watched and ("XP", "PHL", "MCO") not in watched
    f9_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Frontier PHL→MCO: sale wave", airline="F9")
    xp_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Avelo PHL→MCO: sale wave", airline="XP")
    assert alerts.select_pushes([f9_sale, xp_sale], watched) == [f9_sale]
    assert alerts.build_message([f9_sale])[0] == "Frontier sale on a watched route"
    assert alerts.build_message([f9_sale, xp_sale])[0] == "Avelo & Frontier sale on a watched route"
    assert alerts.build_message([Event("booked_drop", None, "TPA", None, "m")])[0] == "A booked trip got cheaper"


def test_package_watch_watches_its_legs_airline(conn):
    db.add_watch("package", "p", {"out_trip": "F9|PHL|MCO|2026-11-12|07:00", "in_trip": "XP|MCO|ILG|2026-11-15|*"}, conn=conn)
    assert alerts.watched_routes(conn) == {("F9", "PHL", "MCO"), ("XP", "MCO", "ILG")}


def test_watch_from_carry_on_counted_search_does_not_fake_a_price_drop(conn, monkeypatch):
    monkeypatch.setattr(config, "CARRY_ON_FEE", {"XP": 0.0, "F9": 55.0})
    out = Flight(airline="F9", origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 8), price=100,
                 base_price=100, duration_minutes=150, flight_id="F9-PHL-TPA")
    back = Flight(airline="F9", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 15), price=80,
                  base_price=80, duration_minutes=150, flight_id="F9-TPA-PHL")
    db.upsert_flights([out, back], conn)
    q = GetawayQuery({"TPA"}, [WINDOW], origins={"PHL"}, count_carry_on=True)
    (it,) = build_itineraries([out], [back], q)
    assert it.total_price == 290.0 and it.fare_total == 180
    wid, _ = alerts.watch_package(it, conn)
    assert db.list_watches("package", conn)[0]["state"]["last_total"] == 180
    assert alerts.evaluate_packages(conn) == []  # fares unchanged -> no event
