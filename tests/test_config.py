import config


def test_all_sixteen_ilg_destinations_present_and_grouped():
    assert set(config.DESTINATIONS) == {"LAL", "SRQ", "TPA", "MCO", "RSW", "DAB", "FLL", "PBI", "MYR", "ILM", "SAV", "CHS", "GSP", "RDU", "BNA", "SJU"}
    assert config.REGIONS == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert config.region_codes("Tennessee") == ["BNA"]
    assert set(config.REGION_COLORS) == set(config.REGIONS)


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
