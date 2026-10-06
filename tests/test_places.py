import config
import database
import places


def test_parse_geocode_picks_first_result_with_coordinates():
    payload = {"results": [{"name": "Siesta Key", "admin1": "Florida", "country_code": "US", "latitude": 27.27, "longitude": -82.55}]}
    p = places.parse_geocode(payload, "siesta key")
    assert p.name == "Siesta Key, Florida, US" and p.lat == 27.27 and p.query == "siesta key"
    assert places.parse_geocode({"results": []}, "x") is None


def test_estimate_minutes_is_monotonic():
    assert places.estimate_minutes(0) == 10
    assert places.estimate_minutes(30) < places.estimate_minutes(60)


def test_drive_minutes_falls_back_to_estimate_and_caches(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "OFFLINE", True)
    place = places.Place("siesta key", "Siesta Key, FL", 27.2673, -82.5459)
    minutes, source = places.drive_minutes(place, "SRQ")
    assert source == "estimate" and 10 < minutes < 40
    far, _ = places.drive_minutes(place, "FLL")
    assert far > 180
    assert database.get_drive("siesta key", "SRQ")["minutes"] == minutes  # cached
    assert places.drive_minutes(place, "XXX") == (float("inf"), "unknown")
    assert places.geocode("nowhere") is None  # offline and not cached
    database.set_place("nowhere", "Nowhere, XX", 1.0, 2.0)
    assert places.geocode("nowhere").name == "Nowhere, XX"
