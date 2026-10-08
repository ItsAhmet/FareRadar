"""The sidebar caption must not promise fewer routes than the scrape it launches visits (run_scraper.routes_for)."""
import argparse
import itertools
from datetime import datetime, timedelta

import pytest

import config
import database
import frontier
import run_scraper
import views


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "flights.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(config, "OFFLINE", True)
    database.init_db(path)
    return path


def _launched(codes, homes, dests):
    args = argparse.Namespace(routes=None, destinations=",".join(dests), origins=",".join(homes))
    return {c: len(run_scraper.routes_for(c, args, list(codes))) for c in codes}


def _estimate(codes, homes, dests):
    """What app.scraper_panel computes for the caption."""
    catalog = {code: database.catalog_routes(code, origins=list(homes)) for code in config.KNOWN_AIRLINES}
    stale = views.stale_frontier_homes(homes, lambda h: database.catalog_checked("F9", h))
    return views.scrape_estimate(codes, homes, dests, catalog, refreshing=stale)


def _online(monkeypatch, listed):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(frontier, "parse_routes", lambda html, origin: set(listed))
    monkeypatch.setattr(frontier, "_http_get", lambda url: "<html/>")


def _holds(entry, launched):
    return {"exact": launched == entry.routes, "at_least": launched >= entry.routes, "up_to": launched <= entry.routes}[entry.bound]


def test_partial_catalog_is_a_lower_bound_because_the_other_home_is_read_first(db, monkeypatch):
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    _online(monkeypatch, {"MCO", "TPA"})
    est = _estimate(["F9"], ["PHL", "BWI"], ["MCO"])["F9"]
    launched = _launched(["F9"], ["PHL", "BWI"], ["MCO"])["F9"]
    assert est.bound == "at_least" and launched == 4 and _holds(est, launched)


def test_stale_catalog_is_a_lower_bound(db, monkeypatch):
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page", now=datetime.now() - timedelta(days=10))
    _online(monkeypatch, {"MCO", "TPA"})
    est = _estimate(["F9"], ["PHL"], ["MCO", "TPA"])["F9"]
    launched = _launched(["F9"], ["PHL"], ["MCO", "TPA"])["F9"]
    assert est.bound == "at_least" and launched == 4 and _holds(est, launched)


def test_empty_catalog_ceiling_covers_avelo_only_destinations(db, monkeypatch):
    avelo_only = [d for d in config.DESTINATIONS if d not in config.FRONTIER_SHARED_DESTINATIONS and d not in config.FRONTIER_AIRPORTS][:3]
    _online(monkeypatch, set(avelo_only))
    est = _estimate(["F9"], ["PHL"], avelo_only)["F9"]
    launched = _launched(["F9"], ["PHL"], avelo_only)["F9"]
    assert est.bound == "up_to" and launched == 6 and _holds(est, launched)


HOMES = [["ILG"], ["PHL"], ["BWI"], ["ILG", "PHL"], ["PHL", "BWI"], ["ILG", "PHL", "BWI"]]
DESTS = [["TPA"], ["MCO"], ["TPA", "MCO", "DEN"], ["LAL"]]


@pytest.mark.parametrize("homes,dests", list(itertools.product(HOMES, DESTS)))
def test_fresh_catalog_estimates_hold_for_every_selection(db, monkeypatch, homes, dests):
    pairs = [p for h in ("PHL", "BWI") for d in ("MCO", "TPA", "DEN") for p in ((h, d), (d, h))]
    database.upsert_catalog("F9", pairs, "route-page")
    _online(monkeypatch, {"MCO", "TPA", "DEN"})
    dests = [d for d in dests if d in config.ALL_DESTINATIONS]
    launched = _launched(["F9", "XP"], homes, dests)
    for code, entry in _estimate(["F9", "XP"], homes, dests).items():
        assert _holds(entry, launched[code]), (code, entry, launched[code])
