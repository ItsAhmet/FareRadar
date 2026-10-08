from datetime import date, datetime

import database as db
from models import Flight
from signals import detect_route, run_detection

D = lambda n: date(2026, 10, n)  # noqa: E731


def kinds(events):
    return sorted(e.kind for e in events)


def test_first_run_sets_floor_and_flags_floor_days_quietly():
    cur = {D(2): 74.0, D(4): 53.0, D(6): 53.0, D(9): 114.0}
    events, floor = detect_route("ILG", "TPA", None, cur, None)
    assert floor == 53.0
    assert kinds(events) == ["floor_fare", "floor_fare"]
    assert all(not e.notify for e in events)


def test_sale_wave_when_enough_days_drop():
    prev = {D(2): 100.0, D(4): 100.0, D(6): 100.0, D(9): 100.0, D(11): 100.0}
    cur = {D(2): 80.0, D(4): 60.0, D(6): 100.0, D(9): 79.0, D(11): 100.0}  # 3 of 5 dropped ≥15%
    events, _ = detect_route("ILG", "TPA", prev, cur, 100.0)
    wave = [e for e in events if e.kind == "sale_wave"]
    assert len(wave) == 1 and wave[0].notify
    assert wave[0].detail["days"] == ["2026-10-02", "2026-10-04", "2026-10-09"]
    assert "new_floor" in kinds(events)  # 60 < previous floor 100


def test_no_sale_wave_below_thresholds():
    prev = {D(2): 100.0, D(4): 100.0, D(6): 100.0, D(9): 100.0}
    cur = {D(2): 80.0, D(4): 100.0, D(6): 100.0, D(9): 100.0}  # only 1 of 4
    events, _ = detect_route("ILG", "TPA", prev, cur, 80.0)
    assert "sale_wave" not in kinds(events)


def test_sold_out_and_floor_gone():
    prev = {D(2): 53.0, D(4): 74.0, D(6): 53.0}
    cur = {D(2): None, D(4): 74.0, D(6): 74.0}  # Oct 2 vanished; Oct 6 left the floor
    events, floor = detect_route("ILG", "TPA", prev, cur, 53.0)
    assert floor == 53.0  # floor is all-time, not per-run
    by_kind = {e.kind: e for e in events}
    assert by_kind["sold_out"].day == D(2) and "was $53" in by_kind["sold_out"].message
    assert by_kind["floor_gone"].day == D(6) and by_kind["floor_gone"].detail["now"] == 74.0


def test_run_detection_end_to_end_with_two_runs():
    conn = db.get_connection(":memory:")

    def flight(day, price):
        return Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, day), price=price, flight_id="LOWFARE-ILG-TPA")

    days = [D(2), D(4), D(6), D(9), D(11)]
    r1 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(r1, "ILG", "TPA", days, [flight(d, 100) for d in (2, 4, 6, 9, 11)], conn)
    first = run_detection(r1, conn)
    db.finish_run(r1, {}, conn)
    assert kinds(first) == ["floor_fare"] * 5 and db.get_floor("ILG", "TPA", conn) == 100

    r2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    # Oct 6 sold out; 3 of the 4 comparable days dropped 30%
    db.record_seen(r2, "ILG", "TPA", days, [flight(2, 70), flight(4, 70), flight(9, 100), flight(11, 70)], conn)
    second = run_detection(r2, conn)
    assert {"sale_wave", "sold_out", "new_floor"} <= set(kinds(second))
    assert db.get_floor("ILG", "TPA", conn) == 70
    stored = db.list_events(run_id=r2, conn=conn)
    assert len(stored) == len(second)
    assert db.unread_event_count(conn) == len([e for e in second if e.notify])
    conn.close()


def test_partial_runs_raise_no_sold_out():
    prev = {date(2026, 10, 9): 79.0, date(2026, 10, 12): 89.0}
    cur = {date(2026, 10, 9): 79.0, date(2026, 10, 12): None}
    events, _ = detect_route("ILG", "TPA", prev, cur, None)
    assert [e.kind for e in events if e.kind == "sold_out"] == ["sold_out"]
    events, _ = detect_route("ILG", "TPA", prev, cur, None, allow_sold_out=False)
    assert not [e for e in events if e.kind == "sold_out"]


def test_run_detection_passes_allow_sold_out_through():
    conn = db.get_connection(":memory:")
    d1, d2 = date(2026, 10, 9), date(2026, 10, 12)
    fare = lambda d, p: Flight(origin="ILG", destination="TPA", departure_datetime=datetime(d.year, d.month, d.day),  # noqa: E731
                               price=p, flight_id="LOWFARE-ILG-TPA")
    r1 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(r1, "ILG", "TPA", [d1, d2], [fare(d1, 79), fare(d2, 89)], conn)
    db.finish_run(r1, {}, conn=conn)
    r2 = db.start_run("calendar", [("ILG", "TPA")], conn=conn)
    db.record_seen(r2, "ILG", "TPA", [d1, d2], [fare(d1, 79)], conn)  # Oct 12 seen without a fare

    assert not [e for e in run_detection(r2, conn, allow_sold_out=False) if e.kind == "sold_out"]
    assert [e.day for e in run_detection(r2, conn) if e.kind == "sold_out"] == [d2]  # the default still reports it
    conn.close()


def _fare_run(conn, airline, price, fid, origin="BWI", dest="TPA"):
    days = [date(2026, 11, 10 + i) for i in range(4)]
    run = db.start_run("calendar", [(origin, dest)], airline=airline, conn=conn)
    fares = [Flight(flight_id=fid, origin=origin, destination=dest, departure_datetime=datetime(2026, 11, 10 + i),
                    price=price, airline=airline) for i in range(4)]
    db.record_seen(run, origin, dest, days, fares, conn)
    db.finish_run(run, {}, status="ok", conn=conn)
    return run


def test_frontier_runs_raise_frontier_signals_and_leave_the_avelo_floor_alone():
    # on a route both airlines fly, a Frontier fare must not overwrite Avelo's floor or be reported as Avelo's
    # (and the other way round)
    conn = db.get_connection(":memory:")
    floors = lambda: (db.get_floor("BWI", "TPA", conn), db.get_floor("BWI", "TPA", conn, airline="F9"))  # noqa: E731
    run_detection(_fare_run(conn, "XP", 80, "LOWFARE-BWI-TPA"), conn)
    assert floors() == (80, None)
    f9 = _fare_run(conn, "F9", 40, "F9CAL-BWI-TPA")
    first = run_detection(f9, conn)
    assert kinds(first) == ["floor_fare"] * 4 and floors() == (80, 40)   # Frontier's first sighting: quiet, its own floor
    assert {e["airline"] for e in db.list_events(run_id=f9, conn=conn)} == {"F9"}
    events = run_detection(_fare_run(conn, "XP", 70, "LOWFARE-BWI-TPA"), conn)
    assert "new_floor" in kinds(events) and floors() == (70, 40)         # Avelo's own new low; 70 is no low for Frontier
    assert all(e.airline == "XP" and e.message.startswith("Avelo BWI→TPA") for e in events)
    assert "sale_wave" not in kinds(events)                              # 80 → 70 is compared with Avelo's 80, not Frontier's 40
    sale = run_detection(_fare_run(conn, "F9", 20, "F9CAL-BWI-TPA"), conn)
    assert {"new_floor", "sale_wave"} <= set(kinds(sale)) and floors() == (70, 20)
    assert all(e.airline == "F9" and e.message.startswith("Frontier BWI→TPA") for e in sale)
    conn.close()


def test_detect_route_labels_default_to_avelo():
    events, _ = detect_route("ILG", "TPA", None, {D(2): 53.0}, None)
    assert [(e.airline, e.message) for e in events] == [("XP", "Avelo ILG→TPA (Tampa): Fri Oct 02 at the $53 floor fare")]


def test_run_airline_and_unknown_runs():
    conn = db.get_connection(":memory:")
    run = db.start_run("calendar", [("BWI", "TPA")], airline="F9", conn=conn)
    assert db.run_airline(run, conn) == "F9" and db.run_airline(999, conn) is None
    assert run_detection(999, conn) == []
    conn.close()


def test_each_airline_has_its_own_floor_and_labels():
    conn = db.get_connection(":memory:")
    days = [D(2), D(4), D(6), D(9), D(11)]

    def priced(airline, price):
        return [Flight(airline=airline, origin="PHL", destination="MCO", departure_datetime=datetime(2026, 10, d.day),
                       price=price, flight_id=f"{airline}CAL") for d in days]

    xp = db.start_run("calendar", [("PHL", "MCO")], conn=conn)
    db.record_seen(xp, "PHL", "MCO", days, priced("XP", 100), conn)
    run_detection(xp, conn)
    db.finish_run(xp, {}, conn)
    f9_run = db.start_run("calendar", [("PHL", "MCO")], conn=conn, airline="F9")
    db.record_seen(f9_run, "PHL", "MCO", days, priced("F9", 40), conn)
    events = run_detection(f9_run, conn)
    assert events and all(e.airline == "F9" and e.message.startswith("Frontier ") for e in events)
    assert "new_floor" not in kinds(events)                          # 40 is Frontier's first floor, not a new low for Avelo
    assert (db.get_floor("PHL", "MCO", conn), db.get_floor("PHL", "MCO", conn, airline="F9")) == (100, 40)
    db.finish_run(f9_run, {}, conn)
    xp2 = db.start_run("calendar", [("PHL", "MCO")], conn=conn)
    db.record_seen(xp2, "PHL", "MCO", days, priced("XP", 100), conn)
    assert "sale_wave" not in kinds(run_detection(xp2, conn))        # Avelo compares with Avelo's run, not Frontier's
    conn.close()


# --- only like is compared with like: price basis (Frontier calendar vs day search), promo runs, mock runs ---

import persist  # noqa: E402

F9_DAYS = [date(2026, 11, 10 + i) for i in range(6)]


def _f9_calendar(day, den):
    """What frontier.parse_calendar yields: the day's lowest Discount Den total, any routing."""
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(day.year, day.month, day.day),
                  price=den, member_price=den, flight_id="F9CAL-PHL-MCO")


def _f9_nonstop(day, standard, den):
    """What frontier.parse_select yields for a nonstop."""
    return Flight(airline="F9", origin="PHL", destination="MCO", departure_datetime=datetime(day.year, day.month, day.day, 7),
                  arrival_datetime=datetime(day.year, day.month, day.day, 9, 40), price=standard, base_price=standard,
                  member_price=den, flight_number="2415", flight_id="F9-2415-PHL-MCO", duration_minutes=160)


def _f9_run(conn, mode, standard=204.0, den=139.0, nonstop_den=199.0):
    """One Frontier run: 'detail' searches every day, 'calendar' reads the calendar only."""
    run = db.start_run(mode, [("PHL", "MCO")], conn=conn, airline="F9")
    if mode == "detail":
        for d in F9_DAYS:
            persist.save_day(run, "PHL", "MCO", d, [_f9_nonstop(d, standard, nonstop_den)], conn, airline="F9")
    else:
        persist.save_calendar(run, "PHL", "MCO", F9_DAYS, [_f9_calendar(d, den) for d in F9_DAYS], conn, airline="F9")
    events = run_detection(run, conn)
    db.finish_run(run, {}, conn, status="ok")
    return events


def _loud(events):
    return sorted(e.kind for e in events if e.notify)


def test_frontier_detail_run_then_calendar_run_is_not_a_sale():
    # nonstops cost $204 on every day and nothing changed; the calendar shows $139 (a connection's Discount Den fare)
    conn = db.get_connection(":memory:")
    _f9_run(conn, "detail")
    events = _f9_run(conn, "calendar")
    assert events == [] and db.get_floor("PHL", "MCO", conn, airline="F9") == 204.0     # the floor is a fare the owner can buy
    assert _f9_run(conn, "detail") == []                                               # and back again: nothing left the floor
    assert _f9_run(conn, "calendar") == []
    conn.close()


def test_frontier_calendar_run_then_detail_run_is_not_floor_gone():
    conn = db.get_connection(":memory:")
    assert _f9_run(conn, "calendar") == [] and db.get_floor("PHL", "MCO", conn, airline="F9") is None
    events = _f9_run(conn, "detail")
    assert _loud(events) == [] and kinds(events) == ["floor_fare"] * 6                   # the first bookable fares: quiet
    assert db.get_floor("PHL", "MCO", conn, airline="F9") == 204.0
    conn.close()


def test_one_dollar_discount_den_gap_raises_nothing_in_either_order():
    conn = db.get_connection(":memory:")
    for mode in ("detail", "calendar", "detail", "calendar"):
        assert _loud(_f9_run(conn, mode, standard=60.0, den=59.0, nonstop_den=59.0)) == []
    assert db.get_floor("PHL", "MCO", conn, airline="F9") == 60.0
    conn.close()


def test_frontier_runs_of_one_basis_still_raise_their_signals():
    conn = db.get_connection(":memory:")
    _f9_run(conn, "calendar", den=139.0)
    _f9_run(conn, "detail", standard=204.0)
    wave = _f9_run(conn, "calendar", den=89.0)                       # calendar against the earlier calendar run
    assert kinds(wave) == ["sale_wave"]                              # a calendar price never sets or tests the floor
    assert wave[0].message.startswith("Frontier PHL→MCO (Orlando, unverified calendar price): sale wave")
    assert wave[0].detail["basis"] == "calendar" and wave[0].airline == "F9"
    assert db.get_floor("PHL", "MCO", conn, airline="F9") == 204.0
    real = _f9_run(conn, "detail", standard=150.0)                   # day searches against the earlier day searches
    assert _loud(real) == ["new_floor", "sale_wave"] and db.get_floor("PHL", "MCO", conn, airline="F9") == 150.0
    assert all(e.message.startswith("Frontier PHL→MCO (Orlando):") and "basis" not in e.detail for e in real)
    gone = _f9_run(conn, "detail", standard=204.0)
    assert _loud(gone) == ["floor_gone"] * 6
    conn.close()


def _plain_run(conn, price, mode="calendar", promo=None, airline="XP", fid="LOWFARE-ILG-TPA"):
    days = [D(2), D(4), D(6), D(9), D(11)]
    run = db.start_run(mode, [("ILG", "TPA")], promo, conn=conn, airline=airline)
    fares = [Flight(airline=airline, origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, d.day),
                    price=price, promo_code=promo, flight_id=fid) for d in days]
    db.record_seen(run, "ILG", "TPA", days, fares, conn)
    events = run_detection(run, conn)
    db.finish_run(run, {}, conn, status="ok")
    return events


def test_promo_run_never_moves_the_floor_or_becomes_a_plain_runs_baseline():
    for airline in ("XP", "F9"):
        conn = db.get_connection(":memory:")
        floor = lambda: db.get_floor("ILG", "TPA", conn, airline=airline)  # noqa: E731
        _plain_run(conn, 60, airline=airline)
        assert _plain_run(conn, 45, promo="SAVE25", airline=airline) == [] and floor() == 60   # no floor, no wave, no floor_fare
        assert _plain_run(conn, 60, airline=airline) == [] and floor() == 60                   # no floor_gone: nothing moved
        low = _plain_run(conn, 50, airline=airline)                  # a real low above the promo fare is still announced
        assert _loud(low) == ["new_floor", "sale_wave"] and floor() == 50
        again = _plain_run(conn, 30, promo="SAVE25", airline=airline)   # compared with the earlier run under the same code
        assert kinds(again) == ["sale_wave"] and floor() == 50
        assert _plain_run(conn, 30, promo="OTHER", airline=airline) == []
        conn.close()


def test_mock_run_never_moves_the_floor_and_is_compared_with_mock_runs_only():
    conn = db.get_connection(":memory:")
    _plain_run(conn, 100)
    assert _plain_run(conn, 50, mode="mock") == [] and db.get_floor("ILG", "TPA", conn) == 100
    assert _plain_run(conn, 100) == [] and db.get_floor("ILG", "TPA", conn) == 100
    assert kinds(_plain_run(conn, 30, mode="mock")) == ["sale_wave"]      # the --mock-sale demo still shows its wave
    assert db.get_floor("ILG", "TPA", conn) == 100
    conn.close()


def test_detect_route_without_floor_tracking_keeps_the_floor_it_was_given():
    prev = {D(2): 53.0, D(4): 53.0, D(6): 53.0, D(9): 53.0, D(11): 53.0}
    cur = {D(2): 30.0, D(4): 74.0, D(6): 30.0, D(9): None, D(11): 53.0}
    events, floor = detect_route("ILG", "TPA", prev, cur, 53.0, track_floor=False)
    assert floor == 53.0 and kinds(events) == ["sale_wave", "sold_out"]
