from datetime import datetime

from models import Flight
from optimizer import GetawayQuery, Window, build_itineraries
from planner import _greedy, candidates_by_window, plan_season


def pkg(dest, week, price, fit_days=3):
    """One package in the week-th window (Wednesdays Oct 7, 14, 21, 28)."""
    day = 7 + 7 * week
    out = Flight(origin="ILG", destination=dest, departure_datetime=datetime(2026, 10, day, 19), price=price / 2, duration_minutes=150, flight_id=f"O-{dest}-{week}")
    back = Flight(origin=dest, destination="ILG", departure_datetime=datetime(2026, 10, day + fit_days, 15), price=price / 2, duration_minutes=150, flight_id=f"I-{dest}-{week}")
    w = Window(datetime(2026, 10, day, 18), datetime(2026, 10, day + 4, 23, 59))
    return build_itineraries([out], [back], GetawayQuery({dest}, [w], min_nights=1))[0]


def test_plan_maximises_trips_then_cost_with_no_repeats():
    its = [pkg("TPA", 0, 100), pkg("CHS", 0, 80), pkg("TPA", 1, 90), pkg("SAV", 1, 200), pkg("TPA", 2, 300)]
    plan = plan_season(its, budget=300)
    assert [t.window.start.day for t in plan.trips] == [7, 14]
    assert plan.destinations == ["CHS", "TPA"]  # CHS week 0 ($80) + TPA week 1 ($90): 2 trips, cheapest
    assert plan.total_cost == 170 and plan.remaining == 130


def test_repeats_allowed_and_budget_binding():
    its = [pkg("TPA", 0, 100), pkg("TPA", 1, 100), pkg("TPA", 2, 100)]
    assert len(plan_season(its, budget=250, no_repeat=True).trips) == 1
    assert len(plan_season(its, budget=250, no_repeat=False).trips) == 2
    assert len(plan_season(its, budget=250, no_repeat=False, max_trips=1).trips) == 1
    assert plan_season(its, budget=50).trips == []


def test_fit_objective_prefers_longer_trips():
    short, long_ = pkg("TPA", 0, 100, fit_days=1), pkg("CHS", 0, 120, fit_days=4)
    assert plan_season([short, long_], 500, objective="price").destinations == ["TPA"]
    assert plan_season([short, long_], 500, objective="fit").destinations == ["CHS"]


def test_greedy_matches_exact_on_small_case():
    its = [pkg("TPA", 0, 100), pkg("CHS", 0, 80), pkg("TPA", 1, 90), pkg("SAV", 1, 200)]
    windows = candidates_by_window(its, "price")
    greedy = _greedy(windows, 300, 4, True, "price")
    exact = plan_season(its, 300).trips
    assert [t.destination for t in greedy] == [t.destination for t in exact]
