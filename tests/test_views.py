from datetime import datetime

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


def test_program_meter_text():
    den = {"airline": "F9", "program": "Discount Den", "legs": 2, "would_have_saved": 12.0}
    outs, backs = [leg("F9", "PHL", "MCO", 8, 9, 60, den=55)], [leg("F9", "MCO", "PHL", 11, 16, 50, den=46)]
    its = build_itineraries(outs, backs, GetawayQuery({"MCO"}, [WINDOW], origins=["PHL"]))
    text = views.program_meter(den, its)
    assert "Discount Den" in text and "$12" in text and "$9" in text          # booked legs, and the best listed package
    assert views.program_meter({**den, "legs": 0, "would_have_saved": 0.0}, []) is None


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
