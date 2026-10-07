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
