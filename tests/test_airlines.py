from datetime import date

import pytest

import airlines


def test_registry_knows_avelo_and_frontier():
    assert set(airlines.AIRLINES) == {"XP", "F9"}
    assert airlines.get("f9").member_program == "Discount Den" and airlines.get("XP").name == "Avelo"
    with pytest.raises(KeyError):
        airlines.get("ZZ")


def test_owner_price_follows_membership():
    assert airlines.owner_price(True, 83.0, 84.0) == (83.0, True)
    assert airlines.owner_price(False, 83.0, 84.0) == (84.0, False)
    assert airlines.owner_price(False, 83.0, None) == (83.0, False)   # only the program fare is known: best estimate
    assert airlines.owner_price(True, None, 84.0) == (84.0, False)
    assert airlines.owner_price(True, None, None) == (None, False)


def test_frontier_urls_and_site_codes():
    url = airlines.frontier_search_url("PHL", "MCO", date(2026, 11, 12))
    assert url == "https://booking.flyfrontier.com/Flight/InternalSelect?o1=PHL&d1=MCO&dd1=Nov%2012%2C%202026&ADT=1&mon=true&promo="
    cal = airlines.frontier_search_url("PHL", "PBI", date(2026, 11, 2), calendar=True, promo="save20")
    assert "d1=DJT" in cal and "&c=true" in cal and cal.endswith("promo=SAVE20") and "dd1=Nov%202%2C%202026" in cal
    f9 = airlines.get("F9")
    assert f9.site_code("PBI") == "DJT" and f9.app_code("DJT") == "PBI" and f9.app_code("MCO") == "MCO"
    assert f9.booking_url("PHL", "MCO", date(2026, 11, 12)) == url
    assert airlines.get("XP").booking_url("ILG", "TPA", date(2026, 11, 12)) is None   # Avelo ignores URL parameters
