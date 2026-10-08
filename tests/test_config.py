import os
import subprocess
import sys
from pathlib import Path

import config


def test_all_sixteen_ilg_destinations_present_and_grouped():
    assert set(config.DESTINATIONS) == {"LAL", "SRQ", "TPA", "MCO", "RSW", "DAB", "FLL", "PBI", "MYR", "ILM", "SAV", "CHS", "GSP", "RDU", "BNA", "SJU"}
    assert config.REGIONS[:4] == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert config.region_codes("Tennessee") == ["BNA"]
    assert set(config.REGION_COLORS) == set(config.REGIONS)


def test_destination_universe_covers_both_airlines():
    assert set(config.ALL_DESTINATIONS) == set(config.DESTINATIONS) | set(config.FRONTIER_AIRPORTS)
    assert config.REGIONS[:4] == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert set(config.REGION_COLORS) == set(config.REGIONS) == {a.region for a in config.ALL_DESTINATIONS.values()}
    assert "DEN" in config.region_codes("West") and "MIA" in config.region_codes("Florida")
    assert set(config.FRONTIER_SHARED_DESTINATIONS) <= set(config.DESTINATIONS)
    assert config.parse_airline_codes("XP,F9") == ["XP", "F9"]


def test_distances_are_sane():
    assert 20 < config.distance_miles("TPA", "LAL") < 50
    assert 80 < config.distance_miles("CHS", "SAV") < 100
    assert config.distance_miles("TPA", "FLL") > 150
    assert config.distance_miles("ILG", "SJU") > 1500
    assert 20 < config.distance_miles("ILG", "PHL") < 30
    assert config.distance_miles("TPA", "TPA") == 0


def test_home_airports_default_and_parsing():
    assert not hasattr(config, "ORIGIN")
    assert set(config.HOME_AIRPORT_INFO) == {"ILG", "PHL", "BWI"}
    assert config.parse_home_codes("ILG,PHL,BWI") == ["ILG", "PHL", "BWI"]
    assert config.parse_home_codes(" phl, xyz,PHL ") == ["PHL"]
    assert config.parse_home_codes("xyz") == ["ILG"]
    assert config.parse_home_codes("") == ["ILG"]
    assert config.PRIMARY_HOME == config.HOME_CODES[0]
    assert config.is_home("phl") and not config.is_home("TPA")
    assert config.DEFAULT_HOME_COSTS["PHL"].parking_per_day == 17.0
    assert config.DEFAULT_HOME_COSTS["ILG"] == config.HomeCost(None, 0.0)


def test_default_routes_are_both_directions_from_each_home():
    assert config.default_routes(["chs"], origins=["ILG"]) == [("ILG", "CHS"), ("CHS", "ILG")]
    assert config.default_routes(["chs"], origins=["ILG", "PHL"]) == [
        ("ILG", "CHS"), ("CHS", "ILG"), ("PHL", "CHS"), ("CHS", "PHL")]
    assert len(config.default_routes(origins=["ILG"])) == 32


def test_suite_pins_home_airports_regardless_of_owner_settings():
    # tests/conftest.py sets AVELO_HOME_AIRPORTS before config is imported, so the
    # owner's real .env / shell value cannot change what the suite sees.
    assert config.HOME_CODES == ["ILG", "PHL", "BWI"]
    assert config.PRIMARY_HOME == "ILG"


def test_suite_is_hermetic_when_owner_puts_another_home_first():
    """Many tests hard-code ILG fares and lean on defaults that follow PRIMARY_HOME
    (GetawayQuery.origin, alerts.watched_routes, the mock generator). Running them
    in a child pytest whose environment has PHL first must still pass."""
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "AVELO_OFFLINE": "1", "AVELO_HOME_AIRPORTS": "PHL,ILG,BWI"}
    files = [f"tests/test_{n}.py" for n in ("alerts", "bookings", "optimizer", "planner", "signals", "run_scraper")]
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files],
        cwd=root, env=env, capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-500:]


def test_airline_settings_and_frontier_airports():
    assert config.parse_airline_codes("xp, f9,XP,zz") == ["XP", "F9"]
    assert config.parse_airline_codes("") == ["XP"] and config.parse_airline_codes("zz") == ["XP"]
    assert config.DEFAULT_MEMBERSHIPS == {"XP": True, "F9": False}
    assert {"DEN", "LAS", "ATL", "MIA", "CUN", "ORD"} <= set(config.FRONTIER_AIRPORTS)
    assert not set(config.FRONTIER_AIRPORTS) & set(config.DESTINATIONS)       # Avelo's list is untouched
    assert config.airport_name("DEN") == "Denver" and config.distance_miles("PHL", "DEN") > 1400
    assert config.storage_state_path("f9").name == "browser_state_F9.json"
    assert set(config.FRONTIER_ROUTE_PAGES) == {"PHL", "BWI"}


def test_carry_on_fee_estimates():
    assert set(config.CARRY_ON_FEE) == set(config.KNOWN_AIRLINES)
    assert config.CARRY_ON_FEE["XP"] >= 0 and config.CARRY_ON_FEE["F9"] > 0
