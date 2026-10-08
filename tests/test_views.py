from datetime import datetime, timedelta

import pandas as pd

import config
import views
from models import Flight
from optimizer import GetawayQuery, Window, build_itineraries

WINDOW = Window(datetime(2026, 10, 7, 18), datetime(2026, 10, 11, 23, 59))
CATALOG = {
    "XP": [("ILG", "TPA"), ("TPA", "ILG"), ("ILG", "MCO"), ("MCO", "ILG")],
    "F9": [("PHL", "MCO"), ("MCO", "PHL"), ("PHL", "DEN"), ("DEN", "PHL"), ("BWI", "LAS"), ("LAS", "BWI")],
}


def leg(airline, origin, dest, day, hour, price, den=None):
    return Flight(airline=airline, origin=origin, destination=dest, departure_datetime=datetime(2026, 10, day, hour),
                  price=price, base_price=price, member_price=den, duration_minutes=150,
                  flight_id=f"{airline}-{origin}-{dest}-{hour}")


def test_destinations_follow_the_selected_airlines_and_homes():
    both = views.served_destinations(["XP", "F9"], ["ILG", "PHL", "BWI"], CATALOG)
    assert both == {"DEN": ["F9"], "LAS": ["F9"], "MCO": ["F9", "XP"], "TPA": ["XP"]}
    assert views.served_destinations(["F9"], ["PHL"], CATALOG) == {"DEN": ["F9"], "MCO": ["F9"]}
    assert views.served_destinations(["XP"], ["PHL"], CATALOG) == {}
    assert views.served_destinations([], ["ILG"], CATALOG) == {} and views.served_destinations(["XP"], [], CATALOG) == {}
    assert views.destination_label("MCO", ["F9", "XP"]) == "MCO · Orlando · Avelo, Frontier"
    assert views.destination_label("DEN", ["F9"]) == "DEN · Denver · Frontier"


def test_empty_catalog_falls_back_to_known_airports():
    served = views.served_destinations(["XP", "F9"], ["ILG", "PHL"], {"XP": [], "F9": []})
    assert set(served) == set(config.DESTINATIONS) | set(config.FRONTIER_AIRPORTS)
    assert served["TPA"] == ["F9", "XP"] if "TPA" in config.FRONTIER_SHARED_DESTINATIONS else served["TPA"] == ["XP"]
    assert served["DEN"] == ["F9"]
    assert views.served_destinations(["F9"], ["ILG"], {"F9": []}) == {}      # Frontier has no page for ILG
    assert set(views.served_destinations(["XP"], ["PHL"], {"XP": []})) == set(config.DESTINATIONS)


def test_scrape_plan_counts_what_run_scraper_would_request():
    plan = views.scrape_plan(["XP", "F9"], ["ILG", "PHL"], ["TPA", "MCO", "DEN"], CATALOG)
    assert plan == {"XP": 8, "F9": 4}       # Avelo: 2 Avelo destinations × 2 homes × 2 directions; Frontier: catalog pairs
    assert views.scrape_plan(["F9"], ["BWI"], ["TPA"], CATALOG) == {"F9": 0}
    assert views.scrape_plan([], ["ILG"], ["TPA"], CATALOG) == {}


def test_scrape_plan_with_an_empty_frontier_catalog_is_an_upper_estimate_not_zero():
    # a real scrape refreshes the empty catalog first and then visits every matching route
    assert views.scrape_plan(["F9"], ["PHL"], ["DEN"], {"F9": []}) == {"F9": 2}
    assert views.scrape_plan(["F9"], ["PHL"], ["DEN"], {}) == {"F9": 2}
    assert views.scrape_plan(["F9"], ["ILG"], ["DEN"], {"F9": []}) == {"F9": 0}     # Frontier has no page for ILG
    plan = views.scrape_plan(["F9", "XP"], ["ILG", "PHL", "BWI"], ["TPA", "MCO", "DEN"], {"F9": [], "XP": []})
    assert plan["F9"] > 0 and plan["XP"] == 12


def test_airline_comparison_puts_each_airlines_best_side_by_side():
    outs = [leg("XP", "ILG", "MCO", 8, 8, 120), leg("F9", "PHL", "MCO", 8, 9, 60, den=59), leg("F9", "PHL", "MCO", 9, 9, 40)]
    backs = [leg("XP", "MCO", "ILG", 11, 15, 100), leg("F9", "MCO", "PHL", 11, 16, 50)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["ILG", "PHL"]))
    df = views.airline_comparison(its)
    assert list(df.columns) == ["Window", "Destination", "Avelo", "Avelo from", "Frontier", "Frontier from", "Cheaper", "Saves"]
    (row,) = df.to_dict("records")
    assert (row["Avelo"], row["Avelo from"], row["Frontier"], row["Frontier from"]) == (220.0, "ILG", 90.0, "PHL")
    assert (row["Cheaper"], row["Saves"]) == ("Frontier", 130.0)
    only_avelo = views.airline_comparison([i for i in its if i.airlines == ("XP", "XP")])
    assert pd.isna(only_avelo.iloc[0]["Frontier"]) and only_avelo.iloc[0]["Cheaper"] == "Avelo"
    assert views.airline_comparison([]).empty


def test_map_arcs_are_per_home_and_airline():
    served = {"MCO": ["F9", "XP"], "DEN": ["F9"], "TPA": ["XP"]}
    arcs = views.map_arcs(["ILG", "PHL"], served, ["MCO", "DEN"], CATALOG)
    assert {(a["home"], a["code"], a["airline"]) for a in arcs} == {("ILG", "MCO", "XP"), ("PHL", "MCO", "F9"), ("PHL", "DEN", "F9")}
    assert all(len(a["color"]) == 3 and len(a["source"]) == 2 and a["airline_name"] in ("Avelo", "Frontier") for a in arcs)


def test_map_view_keeps_every_dot_inside_a_narrow_map():
    assert views.map_view(["ILG", "PHL"], ["TPA", "MCO"], ["TPA"]) == {"latitude": 30.5, "longitude": -77.0, "zoom": 3.9}
    # a selected western city widens the frame and centres it on what is drawn, not on a fixed longitude
    homes, plotted = ["ILG", "PHL", "BWI"], ["SFO", "LAX", "LAS", "DEN", "TPA"]
    view = views.map_view(homes, plotted, ["SFO"])
    assert view["zoom"] == 3.2
    per_degree = 512 * 2 ** view["zoom"] / 360              # deck.gl: 512 * 2^zoom px per 360 degrees of longitude
    widths = {c: abs(view["longitude"] - config.ALL_AIRPORTS[c].lon) * per_degree * 2 for c in homes + plotted}
    assert max(widths.values()) < 700, widths                # the map is about 820 px wide on a 1280 px window
    assert views.map_view(homes, plotted, ["TPA"])["zoom"] == 3.9     # a western dot that is not selected does not widen
    assert views.map_view(homes, ["TPA"], ["SFO"])["zoom"] == 3.9     # nor does a selected city that is no longer offered


def test_program_meter_text():
    den = {"airline": "F9", "program": "Discount Den", "legs": 2, "would_have_saved": 12.0}
    outs, backs = [leg("F9", "PHL", "MCO", 8, 9, 60, den=55)], [leg("F9", "MCO", "PHL", 11, 16, 50, den=46)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["PHL"]))
    text = views.program_meter(den, its)
    assert "Discount Den" in text and "$12" in text and "$9" in text          # booked legs, and the best listed package
    assert views.program_meter({**den, "legs": 0, "would_have_saved": 0.0}, []) is None


def test_program_meter_omits_a_booked_legs_figure_that_is_zero():
    den = {"airline": "F9", "program": "Discount Den", "legs": 2, "would_have_saved": 0.0}
    assert views.program_meter(den, []) is None                                  # nothing known or already paid
    its = build_itineraries([leg("F9", "PHL", "MCO", 8, 9, 60, den=55)], [leg("F9", "MCO", "PHL", 11, 16, 50, den=46)],
                            GetawayQuery({"MCO"}, [WINDOW], origins=["PHL"]))
    text = views.program_meter(den, its)
    assert "booked" not in text and "$0 on" not in text and "package listed now" in text
    assert "booked leg" in views.program_meter({**den, "would_have_saved": 12.0}, [])


def test_program_meter_ignores_the_other_airlines_saving_in_a_mixed_package():
    outs, backs = [leg("XP", "ILG", "MCO", 8, 8, 120, den=90)], [leg("F9", "MCO", "ILG", 11, 16, 50, den=46)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["ILG"]))
    assert its[0].airlines == ("XP", "F9") and its[0].program_savings == 34.0
    den = {"airline": "F9", "program": "Discount Den", "legs": 0, "would_have_saved": 0.0}
    text = views.program_meter(den, [it for it in its if "F9" in it.airlines])
    assert "$4" in text and "$34" not in text
    plus = {"airline": "XP", "program": "Avelo PLUS", "legs": 0, "would_have_saved": 0.0}
    assert "$30" in views.program_meter(plus, its)
    # a program whose airline flies no non-member leg in the package has nothing to claim
    xp_only = {"airline": "XP", "program": "Avelo PLUS", "legs": 0, "would_have_saved": 0.0}
    only_f9 = build_itineraries([leg("F9", "PHL", "MCO", 8, 9, 60, den=50)], [leg("F9", "MCO", "PHL", 11, 16, 50)],
                                GetawayQuery({"MCO"}, [WINDOW], origins=["PHL"]))
    assert views.program_meter(xp_only, only_f9) is None


def test_scrape_estimate_words_only_real_estimates_as_estimates():
    est = lambda *a, **k: views.scrape_estimate(*a, **k)["F9"]
    # ILG has no Frontier route page: a scrape visits nothing, and that is a fact, not an estimate
    assert est(["F9"], ["ILG"], ["TPA", "MCO"], {"F9": []}) == views.PlanEntry("F9", 0)
    assert views.describe_plan(views.scrape_estimate(["F9", "XP"], ["ILG"], ["TPA", "MCO"], {"F9": [], "XP": []})) == "Avelo 4 · Frontier 0"
    assert est(["F9"], [], ["TPA"], {"F9": []}) == views.PlanEntry("F9", 0)                      # no homes selected
    assert est(["F9"], ["PHL"], [], {"F9": []}) == views.PlanEntry("F9", 0)                      # nothing wanted: 0, not "up to 0"
    # a page home, no rows yet: the ceiling counts every wanted destination, even ones outside the fallback list
    avelo_only = [d for d in config.DESTINATIONS if d not in config.FRONTIER_SHARED_DESTINATIONS and d not in config.FRONTIER_AIRPORTS]
    assert est(["F9"], ["PHL", "BWI"], avelo_only[:3], {"F9": []}) == views.PlanEntry("F9", 12, "up_to")
    # rows for the selected homes, none of them stale: exact
    assert est(["F9"], ["PHL"], ["MCO"], CATALOG) == views.PlanEntry("F9", 2)
    # a scrape re-reads a missing or old route page first and can only add routes
    assert est(["F9"], ["PHL", "BWI"], ["MCO"], CATALOG, refreshing=["BWI"]) == views.PlanEntry("F9", 2, "at_least")
    assert est(["F9"], ["PHL"], ["MCO"], CATALOG, refreshing=["BWI"]).bound == "exact"          # BWI is not selected


def test_scrape_plan_counts_one_direction_catalog_routes_once():
    assert views.scrape_plan(["F9"], ["PHL"], ["MCO"], {"F9": [("PHL", "MCO")]}) == {"F9": 1}


def test_describe_plan_wording():
    plan = {"XP": views.PlanEntry("XP", 8), "F9": views.PlanEntry("F9", 4, "up_to")}
    assert views.describe_plan(plan) == "Avelo 8 · Frontier up to 4 (route list not fetched yet)"
    plan["F9"] = views.PlanEntry("F9", 4, "at_least")
    assert views.describe_plan(plan) == "Avelo 8 · Frontier at least 4 (route list refreshes first)"
    assert views.describe_plan({}) == ""


def test_stale_frontier_homes_are_the_ones_a_scrape_rereads():
    now = datetime(2026, 10, 20)
    seen = {"PHL": now - timedelta(days=2), "BWI": now - timedelta(days=config.CATALOG_REFRESH_DAYS + 1)}
    assert views.stale_frontier_homes(["ILG", "PHL", "BWI"], seen.get, now) == ["BWI"]     # ILG has no page; PHL is fresh
    assert views.stale_frontier_homes(["PHL"], lambda h: None, now) == ["PHL"]             # never listed


def test_mock_selection_is_avelo_from_the_primary_home_only():
    assert views.mock_selection(["ILG", "PHL"], ["F9", "XP"]) == ([config.PRIMARY_HOME], ["XP"])
    assert views.mock_selection(["PHL"], ["F9"]) == ([], ["XP"])
    assert views.mock_selection(["ILG"], []) == (["ILG"], [])
