from datetime import date, datetime, time

import pytest

import database as db
from config import HomeCost
from models import Flight
from optimizer import (
    Availability,
    GetawayQuery,
    Window,
    build_itineraries,
    can_pair,
    enrich_drives,
    enrich_home,
    find_itineraries,
    inter_home_minutes,
    rank_by_price,
    rank_by_schedule,
    to_dataframe,
)


def f(origin, dest, day, hour=None, price=100.0, duration=150, base=None):
    """hour=None → calendar-style row: day known, clock unknown."""
    dep = datetime(2026, 10, day, hour or 0, 0)
    return Flight(
        origin=origin,
        destination=dest,
        departure_datetime=dep,
        price=price,
        base_price=base,
        is_member_fare=base is not None,
        duration_minutes=duration if hour is not None else None,
        flight_id=f"T-{origin}-{dest}-{hour}",
    )


# October 2026: the 7th is a Wednesday, the 11th a Sunday.
WINDOW = Window(datetime(2026, 10, 7, 18, 0), datetime(2026, 10, 11, 23, 59))


def q(**overrides):
    kwargs = dict(destinations={"TPA", "LAL", "SRQ", "FLL", "CHS", "SAV", "GSP"}, windows=[WINDOW], min_nights=1)
    kwargs.update(overrides)
    return GetawayQuery(**kwargs)


def test_availability_expands_to_windows_inside_span():
    av = Availability(start_weekday=2, start_time=time(18, 0), end_weekday=6, end_time=time(23, 59))
    windows = av.windows(date(2026, 10, 1), date(2026, 10, 31))
    assert [w.start.date() for w in windows] == [date(2026, 10, 7), date(2026, 10, 14), date(2026, 10, 21)]
    assert windows[0].start == datetime(2026, 10, 7, 18, 0)
    assert windows[0].end == datetime(2026, 10, 11, 23, 59)
    # Oct 28 → Nov 1 spills past the span and is dropped.


def test_availability_same_weekday_wraps_a_full_week():
    av = Availability(start_weekday=4, start_time=time(17, 0), end_weekday=4, end_time=time(9, 0))
    w = av.windows(date(2026, 10, 2), date(2026, 10, 9))[0]
    assert (w.end - w.start).days == 6


def test_outbound_must_depart_inside_window_when_time_known():
    early = f("ILG", "TPA", 7, hour=9)  # Wed 09:00, before 18:00 free time
    ok = f("ILG", "TPA", 7, hour=19)
    back = f("TPA", "ILG", 10, hour=12)
    its = build_itineraries([early, ok], [back], q())
    assert [i.outbound.departure_datetime.hour for i in its] == [19]


def test_return_must_land_before_window_ends():
    out = f("ILG", "TPA", 8, hour=8)
    late = f("TPA", "ILG", 11, hour=22, duration=150)  # lands 00:30 Monday
    fine = f("TPA", "ILG", 11, hour=18)
    its = build_itineraries([out], [late, fine], q())
    assert [i.inbound.departure_datetime.hour for i in its] == [18]


def test_calendar_rows_match_by_date_and_are_flagged_unverified():
    out = f("ILG", "TPA", 7)  # time unknown, but Wednesday is inside the window by date
    back = f("TPA", "ILG", 11)
    its = build_itineraries([out], [back], q())
    assert len(its) == 1
    assert its[0].time_verified is False
    assert its[0].nights == 4


def test_mix_and_match_within_region_and_distance():
    query = q(max_pair_miles=100)
    assert can_pair("TPA", "LAL", query)  # ~33 mi
    assert can_pair("CHS", "SAV", query)  # ~90 mi
    assert not can_pair("TPA", "FLL", query)  # ~190 mi, too far to drive
    assert not can_pair("GSP", "CHS", query)  # same region, ~180 mi
    assert not can_pair("TPA", "CHS", query)  # different regions
    assert can_pair("TPA", "TPA", q(allow_mix=False))
    assert not can_pair("TPA", "LAL", q(allow_mix=False))


def test_mixed_itinerary_aggregates_both_tickets():
    out = f("ILG", "TPA", 8, hour=8, price=79, base=119)
    back = f("LAL", "ILG", 11, hour=15, price=61, base=101)
    its = build_itineraries([out], [back], q())
    assert len(its) == 1
    it = its[0]
    assert it.is_mixed
    assert it.destination_city == "Tampa"
    assert it.route_mix == "TPA in · LAL out"
    assert it.total_price == 140
    assert it.savings == 80
    assert it.member_fare


def test_min_nights_and_budget_filters():
    out = f("ILG", "TPA", 8, hour=8, price=100)
    backs = [f("TPA", "ILG", 9, hour=15, price=50), f("TPA", "ILG", 11, hour=15, price=400)]
    assert [i.nights for i in build_itineraries([out], backs, q(min_nights=2))] == [3]
    assert [i.total_price for i in build_itineraries([out], backs, q(max_budget=200))] == [150]


def test_fit_rewards_using_the_whole_window():
    out_early = f("ILG", "TPA", 7, hour=19)
    out_late = f("ILG", "TPA", 9, hour=8)
    back = f("TPA", "ILG", 11, hour=20)
    its = build_itineraries([out_early, out_late], [back], q())
    by_out = {i.outbound.departure_date: i.fit for i in its}
    assert by_out[date(2026, 10, 7)] > by_out[date(2026, 10, 9)]
    assert 0.95 < by_out[date(2026, 10, 7)] <= 1.0


def test_rankings_differ_by_objective():
    outs = [f("ILG", "TPA", 7, hour=19, price=150), f("ILG", "TPA", 9, hour=8, price=60)]
    backs = [f("TPA", "ILG", 11, hour=20, price=100), f("TPA", "ILG", 10, hour=12, price=40)]
    its = build_itineraries(outs, backs, q())
    cheapest = rank_by_price(its)[0]
    assert (cheapest.outbound.price, cheapest.inbound.price) == (60, 40)
    best = rank_by_schedule(its)[0]
    assert (best.outbound.price, best.inbound.price) == (150, 100)


def test_multiple_windows_each_yield_packages():
    w2 = Window(datetime(2026, 10, 14, 18, 0), datetime(2026, 10, 18, 23, 59))
    outs = [f("ILG", "TPA", 8, hour=8), f("ILG", "TPA", 15, hour=8)]
    backs = [f("TPA", "ILG", 11, hour=12), f("TPA", "ILG", 18, hour=12)]
    its = build_itineraries(outs, backs, q(windows=[WINDOW, w2]))
    assert sorted(i.window.start.day for i in its) == [7, 14]


def test_find_itineraries_reads_from_db_and_ignores_other_origins():
    conn = db.get_connection(":memory:")
    db.upsert_flights([f("ILG", "CHS", 8, hour=8), f("SAV", "ILG", 11, hour=12), f("HVN", "TPA", 8, hour=8), f("TPA", "HVN", 11, hour=12)], conn)
    its = find_itineraries(q(), conn=conn)
    assert len(its) == 1
    assert its[0].route_mix == "CHS in · SAV out"
    conn.close()


def test_dataframe_columns():
    df = to_dataframe(build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "ILG", 11, hour=12)], q()))
    assert list(df.columns[:6]) == ["Rank", "Window", "Destination", "Home", "Route Mix", "Out Route"]
    assert df.loc[0, "Total Getaway Cost"] == 200


def test_dataframe_weather_column_is_not_named_after_one_home_airport():
    df = to_dataframe(build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "ILG", 11, hour=12)], q()))
    assert "Δ°F vs home" in df.columns
    assert not [c for c in df.columns if "ILG" in c]


def test_query_validation():
    with pytest.raises(ValueError):
        q(windows=[])
    with pytest.raises(ValueError):
        Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 7, 18))


def test_member_benefits_lock_addons_to_zero_and_total_is_fares_only():
    from optimizer import BENEFITS

    assert BENEFITS.carry_on_bag == 0.0 and BENEFITS.seat_selection == 0.0 and BENEFITS.total == 0.0
    its = build_itineraries([f("ILG", "TPA", 8, hour=8, price=79)], [f("TPA", "ILG", 11, hour=15, price=61)], q())
    assert its[0].addon_fees == 0.0
    assert its[0].total_price == its[0].fare_total == 140


def test_enrich_computes_deal_and_trend():
    from optimizer import enrich

    out, back = f("ILG", "TPA", 8, hour=8, price=80), f("TPA", "ILG", 11, hour=15, price=60)
    (it,) = build_itineraries([out], [back], q())
    enrich([it], {("ILG", "TPA"): 100, ("TPA", "ILG"): 100}, {(out.flight_id, "2026-10-08T08:00"): 95})
    assert it.typical_total == 200
    assert it.deal_pct == 0.3  # $140 vs $200 typical
    assert it.previous_total == 155  # 95 + current 60
    assert it.trend.startswith("▼ falling $15")

    (fresh,) = build_itineraries([out], [back], q())
    enrich([fresh], {}, {})
    assert fresh.deal_pct is None and fresh.trend == "new"


def test_wildcard_picks_a_valid_package_deterministically():
    import random

    from optimizer import wildcard

    outs = [f("ILG", "TPA", 8, hour=8, price=100), f("ILG", "CHS", 8, hour=8, price=90)]
    backs = [f("TPA", "ILG", 11, hour=12, price=100), f("CHS", "ILG", 11, hour=12, price=90)]
    its = build_itineraries(outs, backs, q())
    pick = wildcard(its, random.Random(1))
    assert pick in its
    assert wildcard([], random.Random(1)) is None


def test_holiday_extension_monday_and_friday():
    av = Availability(2, time(18, 0), 6, time(23, 59))
    plain = av.windows(date(2026, 10, 1), date(2026, 10, 15))
    extended = av.windows(date(2026, 10, 1), date(2026, 10, 15), holiday_extend=True)
    assert plain[0].end == datetime(2026, 10, 11, 23, 59) and plain[0].holiday is None
    assert extended[0].end == datetime(2026, 10, 12, 23, 59)  # Columbus Day Monday
    assert extended[0].holiday == "Columbus Day" and "🎉" in extended[0].label

    fri = Availability(4, time(18, 0), 6, time(23, 59))  # free Fri evening → Sun
    (w,) = fri.windows(date(2026, 6, 29), date(2026, 7, 6), holiday_extend=True)
    assert w.start == datetime(2026, 7, 2, 18, 0)  # Independence Day observed Fri Jul 3 → start Thu evening
    assert w.holiday == "Independence Day"


def test_intersect_windows_with_partner():
    from optimizer import intersect_windows

    mine = Availability(2, time(18), 6, time(23, 59)).windows(date(2026, 10, 1), date(2026, 10, 15))
    theirs = Availability(4, time(12), 0, time(9)).windows(date(2026, 10, 1), date(2026, 10, 15))  # Fri noon → Mon 9am
    both = intersect_windows(mine, theirs)
    assert [(w.start, w.end) for w in both] == [
        (datetime(2026, 10, 9, 12), datetime(2026, 10, 11, 23, 59)),
    ]
    assert intersect_windows(mine, []) == []


def test_sun_and_door_to_door_enrichment_and_ranking():
    from optimizer import enrich_drives, enrich_weather, rank_by_door_to_door, rank_by_sun

    outs = [f("ILG", "TPA", 8, hour=8, price=100), f("ILG", "LAL", 8, hour=9, price=60)]
    backs = [f("TPA", "ILG", 11, hour=12, price=100), f("LAL", "ILG", 11, hour=12, price=60)]
    its = build_itineraries(outs, backs, q(allow_mix=False))
    sunny = [{"day": date(2026, 10, d), "high_f": 88, "rain_pct": 5} for d in (8, 9, 10, 11)]
    wet = [{"day": date(2026, 10, d), "high_f": 70, "rain_pct": 80} for d in (8, 9, 10, 11)]
    hub = [{"day": date(2026, 10, d), "high_f": 60, "rain_pct": 20} for d in (8, 9, 10, 11)]
    enrich_weather(its, {"TPA": sunny, "LAL": wet}, hub)
    by_dest = {it.destination: it for it in its}
    assert by_dest["TPA"].sun_score > by_dest["LAL"].sun_score
    assert by_dest["TPA"].temp_delta_f == 28.0
    assert rank_by_sun(its)[0].destination == "TPA"  # sunnier wins despite costing more

    enrich_drives(its, {"TPA": 90.0, "LAL": 20.0}, value_per_hour=30.0)
    assert by_dest["TPA"].door_to_door_cost == 200 + 3 * 30  # 180 min round trip at $30/h
    assert by_dest["LAL"].door_to_door_cost == 120 + (40 / 60) * 30
    assert rank_by_door_to_door(its)[0].destination == "LAL"
    unscored = build_itineraries(outs, backs, q(allow_mix=False))
    assert rank_by_sun(unscored + its)[-1].sun_score is None  # unscored sink to the bottom


def test_packages_from_each_home_airport_return_to_the_same_home():
    flights_out = [f("ILG", "TPA", 8, hour=8), f("PHL", "TPA", 8, hour=9, price=60)]
    flights_back = [f("TPA", "ILG", 11, hour=15), f("TPA", "PHL", 11, hour=16, price=60)]
    its = build_itineraries(flights_out, flights_back, q(origins=["ILG", "PHL"]))
    assert {(i.home_out, i.home_back) for i in its} == {("ILG", "ILG"), ("PHL", "PHL")}
    assert {i.home_label for i in its} == {"ILG", "PHL"}
    assert not any(i.is_open_jaw for i in its)


def test_open_jaw_toggle_allows_returning_to_another_home():
    flights_out = [f("ILG", "TPA", 8, hour=8), f("PHL", "TPA", 8, hour=9, price=60)]
    flights_back = [f("TPA", "ILG", 11, hour=15), f("TPA", "PHL", 11, hour=16, price=60)]
    its = build_itineraries(flights_out, flights_back, q(origins=["ILG", "PHL"], allow_open_jaw=True))
    assert len(its) == 4
    jaw = next(i for i in its if i.home_out == "ILG" and i.home_back == "PHL")
    assert jaw.is_open_jaw and jaw.home_label == "ILG→PHL"


def test_unselected_home_airports_are_ignored():
    its = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["ILG"]))
    assert its == []


def test_query_requires_an_origin():
    with pytest.raises(ValueError):
        q(origins=[])


def test_dataframe_has_home_column():
    its = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))
    assert list(to_dataframe(its)["Home"]) == ["PHL"]


def test_home_side_costs_add_parking_and_drives():
    costs = {"ILG": HomeCost(20, 0.0), "PHL": HomeCost(40, 17.0), "BWI": HomeCost(None, 14.0)}
    ilg = build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "ILG", 11, hour=15)], q(origins=["ILG"]))[0]
    phl = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))[0]
    bwi = build_itineraries([f("BWI", "TPA", 8, hour=9)], [f("TPA", "BWI", 11, hour=16)], q(origins=["BWI"]))[0]
    enrich_home([ilg, phl, bwi], costs)
    assert (ilg.parking_cost, ilg.home_drive_min) == (0.0, 40)
    assert (phl.parking_cost, phl.home_drive_min) == (51.0, 80)   # 3 nights × $17
    assert bwi.home_drive_min is None and bwi.parking_cost == 42.0
    assert phl.home_side_cost == round(51 + 80 / 60 * 15, 2)


def test_open_jaw_adds_the_drive_between_home_airports():
    costs = {"ILG": HomeCost(20, 0.0), "PHL": HomeCost(40, 17.0)}
    its = build_itineraries([f("ILG", "TPA", 8, hour=8)], [f("TPA", "PHL", 11, hour=16)],
                            q(origins=["ILG", "PHL"], allow_open_jaw=True))
    (jaw,) = enrich_home(its, costs)
    assert 30 < inter_home_minutes("ILG", "PHL") < 50
    assert jaw.home_drive_min == round(20 + 40 + inter_home_minutes("PHL", "ILG"), 1)
    assert jaw.parking_cost == 0.0   # car is parked at ILG


def test_door_to_door_includes_home_side_when_destination_drives_known():
    costs = {"PHL": HomeCost(40, 17.0)}
    (it,) = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))
    enrich_home([it], costs)
    assert it.door_to_door_cost is None            # destination drives unknown → unchanged behaviour
    enrich_drives([it], {"TPA": 30}, 15.0)
    assert it.door_to_door_cost == round(it.total_price + 60 / 60 * 15 + it.home_side_cost, 2)
    assert "Home-side $" in to_dataframe([it]).columns


def test_clear_drives_restores_the_default_rate_and_drops_drives():
    """Regression: a cleared place must not leave its drives or $/hour on reused itineraries,
    or home-side drive time would still be priced at the old place's rate."""
    from optimizer import DEFAULT_DRIVE_VALUE_PER_HOUR, clear_drives

    costs = {"PHL": HomeCost(40, 17.0)}
    (it,) = build_itineraries([f("PHL", "TPA", 8, hour=9)], [f("TPA", "PHL", 11, hour=16)], q(origins=["PHL"]))
    enrich_home([it], costs)
    default_home_side = it.home_side_cost
    assert default_home_side == round(51 + 80 / 60 * DEFAULT_DRIVE_VALUE_PER_HOUR, 2)

    enrich_drives([it], {"TPA": 30}, 60.0)
    assert it.drive_value_per_hour == 60.0 and it.home_side_cost == round(51 + 80 / 60 * 60, 2)
    assert it.door_to_door_cost is not None

    assert clear_drives([it]) == [it]
    assert (it.drive_out_min, it.drive_back_min) == (None, None)
    assert it.door_to_door_cost is None
    assert it.drive_value_per_hour == DEFAULT_DRIVE_VALUE_PER_HOUR
    assert it.home_side_cost == default_home_side
    assert it.parking_cost == 51.0 and it.home_drive_min == 80   # home-side data is not touched


def test_packages_use_avelo_rows_only_until_phase_3():
    conn = db.get_connection(":memory:")
    out_f9 = Flight(origin="PHL", destination="TPA", departure_datetime=datetime(2026, 10, 8, 9), price=60,
                    duration_minutes=150, airline="F9", flight_id="F9-1-PHL-TPA")
    back_f9 = Flight(origin="TPA", destination="PHL", departure_datetime=datetime(2026, 10, 11, 16), price=60,
                     duration_minutes=150, airline="F9", flight_id="F9-2-TPA-PHL")
    db.upsert_flights([out_f9, back_f9, f("ILG", "TPA", 8, hour=8), f("TPA", "ILG", 11, hour=15)], conn)
    its = find_itineraries(q(origins=["ILG", "PHL"]), conn=conn)
    assert {i.outbound.airline for i in its} == {"XP"} and len(its) == 1
    conn.close()


def test_deal_score_medians_ignore_other_airlines():
    conn = db.get_connection(":memory:")
    cheap_f9 = [Flight(origin="ILG", destination="TPA", departure_datetime=datetime(2026, 10, 9, h), price=400,
                       duration_minutes=150, airline="F9", flight_id=f"F9-{h}-ILG-TPA") for h in (9, 10, 11)]
    db.upsert_flights([f("ILG", "TPA", 8, hour=8, price=100), f("TPA", "ILG", 11, hour=15, price=100), *cheap_f9], conn)
    (it,) = find_itineraries(q(), conn=conn)
    assert it.typical_total == 200      # Avelo medians only; F9 at 400 must not lift it
    conn.close()
