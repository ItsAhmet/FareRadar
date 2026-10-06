import config


def test_all_sixteen_ilg_destinations_present_and_grouped():
    assert config.ORIGIN == "ILG"
    assert set(config.DESTINATIONS) == {"LAL", "SRQ", "TPA", "MCO", "RSW", "DAB", "FLL", "PBI", "MYR", "ILM", "SAV", "CHS", "GSP", "RDU", "BNA", "SJU"}
    assert config.REGIONS == ["Florida", "Carolinas & Georgia", "Tennessee", "Caribbean"]
    assert config.region_codes("Tennessee") == ["BNA"]
    assert set(config.REGION_COLORS) == set(config.REGIONS)


def test_distances_are_sane():
    assert 20 < config.distance_miles("TPA", "LAL") < 50
    assert 80 < config.distance_miles("CHS", "SAV") < 100
    assert config.distance_miles("TPA", "FLL") > 150
    assert config.distance_miles("ILG", "SJU") > 1500
    assert config.distance_miles("TPA", "TPA") == 0


def test_default_routes_are_both_directions_from_ilg():
    assert config.default_routes(["chs"]) == [("ILG", "CHS"), ("CHS", "ILG")]
    assert len(config.default_routes()) == 32
