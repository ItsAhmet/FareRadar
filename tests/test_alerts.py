from datetime import date, datetime, time

import pytest

import alerts
import bookings
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


def watched(conn, out_price=100, back_price=80):
    out, back = f("ILG", "TPA", 8, 8, out_price), f("TPA", "ILG", 11, 15, back_price)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["ILG"]))
    wid, _ = alerts.watch_package(it, conn)
    return wid


def reprice(conn, out_price):
    db.upsert_flights([f("ILG", "TPA", 8, 8, out_price)], conn)


def test_gradual_drops_add_up_to_an_alert(conn):
    wid = watched(conn)                                   # total 180, alert_base 180
    seen = []
    for price in (98, 96, 95):                            # totals 178, 176, 175: never $5 in one step
        reprice(conn, price)
        seen.append([e.kind for e in alerts.evaluate_packages(conn)])
    assert seen == [[], [], ["price_drop"]]               # $5 below what the owner last knew: 180 → 175
    state = db.list_watches("package", conn)[0]["state"]
    assert state["alert_base"] == 175 and state["last_total"] == 175
    reprice(conn, 94)
    assert alerts.evaluate_packages(conn) == []           # only $1 below what was announced


def test_legacy_watch_state_gets_a_base_without_alerting(conn):
    wid = watched(conn)
    db.update_watch_state(wid, {"last_total": 180.0}, conn)        # state written before alert_base existed
    assert alerts.evaluate_packages(conn) == []
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 180.0


def test_a_failed_push_is_retried_on_the_next_run(conn, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    sent = []
    outcome = {"ok": False}

    def fake_ntfy(title, body, topic=None, server=None):
        sent.append(title)
        return outcome["ok"]

    monkeypatch.setattr(alerts, "notify_ntfy", fake_ntfy)
    watched(conn)
    reprice(conn, 80)                                               # 180 → 160
    first = alerts.evaluate_and_deliver(None, [], conn)
    state = db.list_watches("package", conn)[0]["state"]
    assert first["ntfy"] is False and state["last_total"] == 180 and state.get("alert_base", 180) == 180   # nothing recorded
    outcome["ok"] = True
    second = alerts.evaluate_and_deliver(None, [], conn)
    assert second["ntfy"] is True and len(sent) == 2
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 160
    third = alerts.evaluate_and_deliver(None, [], conn)
    assert third["pushed"] == 0 and len(sent) == 2                  # announced once, not again


def test_no_push_channel_still_records_the_alert(conn, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    watched(conn)
    reprice(conn, 80)
    alerts.evaluate_and_deliver(None, [], conn)
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 160    # in-app feed is the delivery


def test_rules_carry_airlines_and_the_partner_window(conn):
    mine, partner = Availability(2, time(18), 6, time(23, 59)), Availability(4, time(17), 6, time(20))
    payload = alerts.rule_payload(["TPA"], mine, 1, 300, True, 100, span_days=14, origins=["ILG"],
                                  airlines=["f9", "XP"], partner=partner, count_carry_on=True)
    assert payload["airlines"] == ["F9", "XP"] and payload["partner"]["start_weekday"] == 4 and payload["count_carry_on"]
    q = alerts.rule_query(payload, today=date(2026, 10, 1))
    assert q.airlines == {"XP", "F9"} and q.count_carry_on
    assert q.windows[0].start == datetime(2026, 10, 9, 17) and q.windows[0].end == datetime(2026, 10, 11, 20)   # both free
    old = {k: v for k, v in payload.items() if k not in ("airlines", "partner", "count_carry_on")}
    legacy = alerts.rule_query(old, today=date(2026, 10, 1))
    assert legacy.airlines == {"XP"} and legacy.windows[0].start == datetime(2026, 10, 7, 18)


def test_old_rule_is_not_flooded_by_frontier_packages(conn):
    db.upsert_flights([f("ILG", "TPA", 8, 8, 100), f("TPA", "ILG", 11, 15, 80)], conn)
    old = {k: v for k, v in alerts.rule_payload(["TPA"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100,
                                                span_days=14).items() if k not in ("airlines", "partner", "count_carry_on")}
    db.add_watch("rule", "Tampa", old, conn)
    today = date(2026, 10, 1)
    assert len(alerts.evaluate_rules(conn, today=today)) == 1
    db.upsert_flights([Flight(airline="F9", origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 9), price=40,
                              duration_minutes=150, flight_id="F9-1-PHL-TPA"),
                       Flight(airline="F9", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 16), price=40,
                              duration_minutes=150, flight_id="F9-2-TPA-PHL")], conn)
    assert alerts.evaluate_rules(conn, today=today) == []           # a pre-phase-3 rule stays Avelo-only


def test_route_signals_are_pushed_only_for_the_watched_airline(conn):
    payload = alerts.rule_payload(["MCO"], Availability(2, time(18), 6, time(23, 59)), 1, 300, True, 100, origins=["PHL"], airlines=["F9"])
    db.add_watch("rule", "Frontier Orlando", payload, conn)
    watched_set = alerts.watched_routes(conn)
    assert ("F9", "PHL", "MCO") in watched_set and ("XP", "PHL", "MCO") not in watched_set
    f9_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Frontier PHL→MCO: sale wave", airline="F9")
    xp_sale = Event("sale_wave", "PHL", "MCO", date(2026, 11, 12), "Avelo PHL→MCO: sale wave", airline="XP")
    assert alerts.select_pushes([f9_sale, xp_sale], watched_set) == [f9_sale]
    assert alerts.build_message([f9_sale])[0] == "Frontier sale on a watched route"
    assert alerts.build_message([f9_sale, xp_sale])[0] == "Avelo & Frontier sale on a watched route"
    assert alerts.build_message([Event("booked_drop", None, "TPA", None, "m")])[0] == "A booked trip got cheaper"


# ---- review fixes: offline runs, feed-only events, membership changes, mixed-airline titles


def _feed(conn, kind=None):
    return [e for e in db.list_events(limit=500, conn=conn) if kind is None or e["kind"] == kind]


def test_offline_with_a_topic_still_records_the_alert(conn, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", True)                    # pushes switched off on purpose
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    called = []
    monkeypatch.setattr(alerts, "notify_ntfy", lambda *a, **k: called.append(a) or False)
    watched(conn)
    reprice(conn, 80)                                               # 180 → 160
    summaries = [alerts.evaluate_and_deliver(None, [], conn) for _ in range(3)]
    assert summaries[0]["ntfy"] is None and summaries[0]["delivered"] is True and not called
    assert len(_feed(conn, "price_drop")) == 1                      # in the feed once, not on every run
    state = db.list_watches("package", conn)[0]["state"]
    assert state["last_total"] == 160 and state["alert_base"] == 160
    assert summaries[1]["watch_events"] == 0 and summaries[2]["watch_events"] == 0


def test_a_failed_push_does_not_drop_another_watch_s_price_rise_from_the_feed(conn, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    outcome = {"ok": False}
    monkeypatch.setattr(alerts, "notify_ntfy", lambda *a, **k: outcome["ok"])
    watched(conn)                                                   # Tampa 100 + 80
    lal_out, lal_back = f("ILG", "LAL", 8, 9, 100), f("LAL", "ILG", 11, 16, 80)
    db.upsert_flights([lal_out, lal_back], conn)
    (it,) = build_itineraries([lal_out], [lal_back], GetawayQuery({"LAL"}, [WINDOW], origins=["ILG"]))
    alerts.watch_package(it, conn)
    reprice(conn, 130)                                              # Tampa rises $30 (feed only)
    db.upsert_flights([f("ILG", "LAL", 8, 9, 60)], conn)            # Lakeland drops $40 (pushed)
    first = alerts.evaluate_and_deliver(None, [], conn)
    assert first["delivered"] is False
    assert [e["kind"] for e in _feed(conn)] == ["price_rise"]       # the rise is recorded with its state write
    outcome["ok"] = True
    alerts.evaluate_and_deliver(None, [], conn)
    assert sorted(e["kind"] for e in _feed(conn)) == ["price_drop", "price_rise"]   # the drop once it was announced
    alerts.evaluate_and_deliver(None, [], conn)
    assert len(_feed(conn)) == 2


def _frontier_pair(out_airline="F9", back_airline="F9"):
    out = Flight(airline=out_airline, origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 8), price=100,
                 base_price=100, member_price=70, duration_minutes=150, flight_id=f"{out_airline}-PHL-TPA")
    back = Flight(airline=back_airline, origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 15), price=80,
                  base_price=80, member_price=60, duration_minutes=150, flight_id=f"{back_airline}-TPA-PHL")
    return out, back


def test_membership_change_does_not_fake_a_price_drop(conn, monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    sent = []
    monkeypatch.setattr(alerts, "notify_ntfy", lambda title, body, **k: sent.append(title) or True)
    out, back = _frontier_pair()
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["PHL"]))
    alerts.watch_package(it, conn)
    assert alerts.evaluate_and_deliver(None, [], conn)["pushed"] == 0
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 180
    db.set_membership("F9", True, conn)                             # fares reprice to 70 + 60
    state = db.list_watches("package", conn)[0]["state"]
    assert "last_total" not in state and "alert_base" not in state
    summary = alerts.evaluate_and_deliver(None, [], conn)
    assert summary["pushed"] == 0 and sent == [] and _feed(conn) == []
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 130    # the new basis is the baseline
    db.set_membership("F9", False, conn)                            # leaving the program is not a price rise either
    assert alerts.evaluate_and_deliver(None, [], conn)["watch_events"] == 0


def test_a_base_without_a_last_total_is_reseeded(conn):
    wid = watched(conn)
    db.update_watch_state(wid, {"alert_base": 180.0}, conn)         # last_total dropped by an older reset_baselines
    reprice(conn, 60)
    assert alerts.evaluate_packages(conn) == []
    assert db.list_watches("package", conn)[0]["state"]["alert_base"] == 140


def test_mixed_airline_package_title_names_both_airlines(conn):
    out, back = _frontier_pair(out_airline="XP")
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["PHL"]))
    alerts.watch_package(it, conn)
    alerts.evaluate_packages(conn)
    db.upsert_flights([Flight(airline="F9", origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 15),
                              price=50, base_price=50, duration_minutes=150, flight_id="F9-TPA-PHL")], conn)
    (drop,) = alerts.evaluate_packages(conn)
    assert drop.kind == "price_drop" and drop.airline == "XP" and drop.detail["airlines"] == ["F9", "XP"]
    assert alerts.build_message([drop])[0] == "Avelo & Frontier fare dropped"


def test_membership_change_can_still_alert_a_booked_trip(conn, monkeypatch):
    """Documented behaviour (CLAUDE.md/README): package watches and route signals reset on a membership change, but a
    booked trip compares price_paid with the repriced fares, so it can alert that rebooking is now cheaper."""
    monkeypatch.setattr(config, "OFFLINE", True)
    out, back = _frontier_pair()
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"TPA"}, [WINDOW], origins=["PHL"]))
    db.add_booking("trip", bookings.booking_payload(it), it.total_price, conn=conn)
    assert alerts.evaluate_and_deliver(None, [], conn)["watch_events"] == 0
    db.set_membership("F9", True, conn)
    assert alerts.evaluate_and_deliver(None, [], conn)["watch_events"] == 1
    assert [e["kind"] for e in _feed(conn)] == ["booked_drop"]


# --- a Frontier calendar price is not a fare the owner can buy: pushes built on one say so ---


def _f9_placeholder(origin, dest, day, den):
    """A Frontier calendar row as stored for a non-member: the day's lowest Discount Den total, any routing."""
    return Flight(airline="F9", origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day), price=den,
                  member_price=den, flight_id=f"F9CAL-{origin}-{dest}")


def _f9_nonstop(origin, dest, day, hour, price):
    return Flight(airline="F9", origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour),
                  arrival_datetime=datetime(2026, 10, day, hour + 2, 40), price=price, base_price=price, member_price=price - 10,
                  duration_minutes=160, flight_number="2415", flight_id=f"F9-2415-{origin}-{dest}")


def _orlando_rule(conn, airlines):
    payload = alerts.rule_payload(["MCO"], Availability(2, time(18), 6, time(23, 59)), 1, 900, True, 100, span_days=14,
                                  origins=["PHL"], airlines=airlines)
    db.add_watch("rule", "Orlando", payload, conn)


def test_rule_match_on_frontier_calendar_prices_is_marked_unverified(conn):
    db.upsert_flights([_f9_placeholder("PHL", "MCO", 8, 139.0), _f9_placeholder("MCO", "PHL", 11, 139.0)], conn)
    _orlando_rule(conn, ["F9"])
    (event,), _writes = alerts.pending_rules(conn, today=date(2026, 10, 1))
    assert event.kind == "rule_match" and event.detail["unverified"] is True and event.detail["total"] == 278.0
    assert event.message.endswith("· Frontier from $278 (unverified Frontier calendar price: times and fare need a detail scrape)")
    assert "for $278" not in event.message


def test_rule_match_on_searched_frontier_nonstops_is_a_plain_price(conn):
    db.upsert_flights([_f9_nonstop("PHL", "MCO", 8, 19, 204.0), _f9_nonstop("MCO", "PHL", 11, 15, 204.0)], conn)
    _orlando_rule(conn, ["F9"])
    (event,), _writes = alerts.pending_rules(conn, today=date(2026, 10, 1))
    assert event.message.endswith("· Frontier for $408") and "unverified" not in event.detail


def test_one_frontier_calendar_leg_marks_a_mixed_package(conn):
    db.upsert_flights([f("PHL", "MCO", 8, 19, 100), _f9_placeholder("MCO", "PHL", 11, 139.0)], conn)
    _orlando_rule(conn, ["XP", "F9"])
    (event,), _writes = alerts.pending_rules(conn, today=date(2026, 10, 1))
    assert "from $239 (unverified Frontier calendar price" in event.message and event.detail["unverified"] is True


def test_avelo_calendar_fares_are_bookable_and_not_marked(conn):
    # Avelo's calendar fare is the nonstop member fare itself
    db.upsert_flights([Flight(origin=o, destination=d, departure_datetime=datetime(2026, 10, day), price=price,
                              flight_id=f"LOWFARE-{o}-{d}") for o, d, day, price in (("PHL", "MCO", 8, 60), ("MCO", "PHL", 11, 70))], conn)
    _orlando_rule(conn, ["XP"])
    (event,), _writes = alerts.pending_rules(conn, today=date(2026, 10, 1))
    assert event.message.endswith("· Avelo for $130") and "unverified" not in event.detail
    span = (date(2026, 10, 1), date(2026, 10, 31))
    out, back = db.query_flights(["PHL"], ["MCO"], *span, conn=conn)[0], db.query_flights(["MCO"], ["PHL"], *span, conn=conn)[0]
    assert not out.time_known and alerts.unverified_note(out, back) == ""


def test_watched_frontier_calendar_package_drop_is_marked_unverified(conn):
    out, back = _f9_placeholder("PHL", "MCO", 8, 139.0), _f9_placeholder("MCO", "PHL", 11, 139.0)
    db.upsert_flights([out, back], conn)
    (it,) = build_itineraries([out], [back], GetawayQuery({"MCO"}, [Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))],
                                                           origins=["PHL"]))
    alerts.watch_package(it, conn)
    assert alerts.evaluate_packages(conn) == []
    db.upsert_flights([_f9_placeholder("PHL", "MCO", 8, 99.0)], conn)
    (event,) = alerts.evaluate_packages(conn)
    assert event.kind == "price_drop" and event.message.endswith("dropped $40 to $238 (unverified Frontier calendar price: "
                                                                   "times and fare need a detail scrape)")


# --- a push that fails does not consume the alert: rule matches and vanished watches are retried, once ---


def _failing_channel(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    sent, outcome = [], {"ok": False}

    def fake_ntfy(title, body, topic=None, server=None):
        sent.append((title, body))
        return outcome["ok"]

    monkeypatch.setattr(alerts, "notify_ntfy", fake_ntfy)
    return sent, outcome


def _coming_weekend():
    from datetime import timedelta

    fri = date.today() + timedelta(days=10)
    while fri.weekday() != 4:
        fri += timedelta(days=1)
    return fri, fri + timedelta(days=2)


def _xp(origin, dest, day, hour, price):
    return Flight(origin=origin, destination=dest, departure_datetime=datetime.combine(day, time(hour)), price=price,
                  duration_minutes=150, flight_id=f"AVELO-{origin}-{dest}")


def test_a_failed_rule_match_push_is_retried_once(conn, monkeypatch):
    sent, outcome = _failing_channel(monkeypatch)
    fri, sun = _coming_weekend()
    payload = alerts.rule_payload(["TPA"], Availability(4, time(0, 0), 6, time(23, 59)), 1, 900, True, 100,
                                  origins=["ILG"], airlines=["XP"])
    db.add_watch("rule", "r", payload, conn)
    db.upsert_flights([_xp("ILG", "TPA", fri, 9, 50), _xp("TPA", "ILG", sun, 15, 60)], conn)
    first = alerts.evaluate_and_deliver(None, [], conn)
    assert first["delivered"] is False and db.list_events(conn=conn) == []
    assert "matched" not in db.list_watches("rule", conn)[0]["state"]            # the match is not recorded as announced
    outcome["ok"] = True
    second = alerts.evaluate_and_deliver(None, [], conn)
    assert second["delivered"] and [e["kind"] for e in db.list_events(conn=conn)] == ["rule_match"]
    third = alerts.evaluate_and_deliver(None, [], conn)
    assert third["pushed"] == 0 and len(sent) == 2                               # one failed try, one delivery, no repeat


def test_a_failed_watch_unavailable_push_is_retried_once(conn, monkeypatch):
    sent, outcome = _failing_channel(monkeypatch)
    fri, sun = _coming_weekend()
    out, back = _xp("ILG", "TPA", fri, 9, 50), _xp("TPA", "ILG", sun, 15, 60)
    db.upsert_flights([out, back], conn)
    window = Window(datetime.combine(fri, time(0)), datetime.combine(sun, time(23, 59)))
    (it,) = build_itineraries([out], [back], GetawayQuery(destinations={"TPA"}, windows=[window], origins=["ILG"]))
    alerts.watch_package(it, conn)
    conn.execute("DELETE FROM avelo_flights WHERE origin = 'TPA'")
    conn.commit()
    first = alerts.evaluate_and_deliver(None, [], conn)
    assert first["delivered"] is False and not db.list_watches("package", conn)[0]["state"].get("unavailable")
    assert db.list_events(conn=conn) == []
    outcome["ok"] = True
    alerts.evaluate_and_deliver(None, [], conn)
    alerts.evaluate_and_deliver(None, [], conn)
    assert len(sent) == 2 and [e["kind"] for e in db.list_events(conn=conn)] == ["watch_unavailable"]
    assert db.list_watches("package", conn)[0]["state"]["unavailable"] is True
