from datetime import date

import pytest

import config
from run_scraper import build_command, build_dates, build_parser, parse_destinations


def test_build_command_carries_ui_filters_verbatim():
    cmd = build_command(["TPA", "lal"], date(2026, 10, 1), date(2026, 10, 31), mode="full", mock=False, headful=True, python="py")
    assert cmd == [
        "py", str(config.PROJECT_ROOT / "run_scraper.py"),
        "--destinations", "TPA,LAL", "--start", "2026-10-01", "--end", "2026-10-31",
        "--mode", "full", "--purge", "--headful",
    ]
    # and the CLI parses that command back into the same targets
    args = build_parser().parse_args(cmd[2:])
    routes = config.default_routes(parse_destinations(args.destinations), origins=["ILG"])
    assert routes == [("ILG", "TPA"), ("TPA", "ILG"), ("ILG", "LAL"), ("LAL", "ILG")]
    dates = build_dates(args.start, args.end, args.days)
    assert dates[0] == date(2026, 10, 1) and dates[-1] == date(2026, 10, 31) and len(dates) == 31
    assert args.mode == "full" and args.purge and args.headful and not args.mock


def test_build_dates_days_fallback_and_validation():
    assert len(build_dates(date(2026, 10, 1), None, 5)) == 5
    with pytest.raises(SystemExit):
        build_dates(date(2026, 10, 5), date(2026, 10, 1), None)


def test_unknown_destination_rejected():
    with pytest.raises(SystemExit):
        parse_destinations("TPA,XYZ")


def test_build_command_passes_promo_code():
    cmd = build_command(["CHS"], date(2026, 10, 1), date(2026, 10, 7), promo=" save20 ", python="py")
    assert cmd[-2:] == ["--promo", "SAVE20"]
    assert build_parser().parse_args(cmd[2:]).promo == ["SAVE20"]  # --promo is repeatable
    assert "--promo" not in build_command(["CHS"], date(2026, 10, 1), date(2026, 10, 7), promo="  ", python="py")


def test_cli_text_describes_all_home_airports_not_a_fixed_origin():
    import run_scraper

    # with no --routes/--destinations the scraper covers every active home airport...
    assert len(config.default_routes()) == 2 * len(config.HOME_CODES) * len(config.DESTINATIONS)
    # ...so the help text and the examples must not claim it only scrapes ILG
    assert "AVELO_HOME_AIRPORTS" in build_parser().description
    assert "ILG" not in build_parser().description
    assert "all ILG routes" not in run_scraper.__doc__


import database
import runlock
import run_scraper


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "f.db")
    monkeypatch.setattr(runlock, "LOCK_PATH", tmp_path / "scrape.lock")
    return tmp_path


def test_build_command_passes_origins():
    cmd = build_command(["TPA"], date(2026, 10, 1), date(2026, 10, 7), origins=["ilg", "phl"], python="py")
    assert cmd[cmd.index("--origins") + 1] == "ILG,PHL"
    assert "--origins" not in build_command(["TPA"], date(2026, 10, 1), date(2026, 10, 7), python="py")


def test_parse_origins_validates():
    assert run_scraper.parse_origins("ilg,PHL") == ["ILG", "PHL"]
    assert run_scraper.parse_origins(None) == config.HOME_CODES
    with pytest.raises(SystemExit):
        run_scraper.parse_origins("ILG,TPA")


def test_mock_run_finishes_ok_and_releases_lock(isolated):
    assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "7", "--no-alerts", "--origins", "ILG"]) == 0
    assert database.last_runs(1)[0]["status"] == "ok"
    assert not (isolated / "scrape.lock").exists()


def test_crash_is_recorded_as_failed_run(isolated, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("browser exploded")
    monkeypatch.setattr(run_scraper, "generate_mock_flights", boom)
    assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "7", "--no-alerts"]) == 1
    run = database.last_runs(1)[0]
    assert run["status"] == "failed" and run["finished_at"] and "browser exploded" in run["summary"]["error"]
    assert not (isolated / "scrape.lock").exists()


def test_refuses_to_start_while_another_scrape_runs(isolated):
    assert runlock.acquire()
    try:
        assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "3", "--no-alerts"]) == 3
    finally:
        runlock.release()
    assert database.last_runs(1) == []


def test_mock_run_records_sightings_only_for_primary_home_routes(isolated):
    # The mock schedule only flies routes touching PRIMARY_HOME; another home's
    # routes must not get "seen, no fare" rows (they would read as sold out).
    other = next(c for c in config.HOME_AIRPORT_INFO if c != config.PRIMARY_HOME)
    origins = f"{config.PRIMARY_HOME},{other}"
    assert run_scraper.main(["--mock", "--destinations", "TPA", "--days", "7", "--no-alerts", "--origins", origins]) == 0
    run_id = database.last_runs(1)[0]["run_id"]
    assert set(database.routes_in_run(run_id)) == {(config.PRIMARY_HOME, "TPA"), ("TPA", config.PRIMARY_HOME)}


def _fake_live_scraper(monkeypatch, failed, succeeded):
    """Stand in for the airline's scraper (picked by run_scraper.scraper_class) with a report of the given shape."""
    from browser import ScrapeReport

    class FakeScraper:
        def __init__(self, *_a, **_k):
            pass

        async def run(self, routes, dates):
            return ScrapeReport(attempted=failed + succeeded, succeeded=succeeded, failed=failed, failures=["x"] * failed)

    monkeypatch.setattr(run_scraper, "scraper_class", lambda code: FakeScraper)


@pytest.mark.parametrize("failed,succeeded,status,allow_sold_out", [
    (0, 3, "ok", True),
    (1, 2, "partial", False),
    (2, 0, "failed", False),
])
def test_live_run_status_decides_whether_sold_out_can_fire(isolated, monkeypatch, failed, succeeded, status, allow_sold_out):
    """Spec §4: partial or failed runs never produce sold_out events. The run's status comes from the report, and
    only an ok run lets signal detection report a day as no longer bookable."""
    import signals

    calls = []
    monkeypatch.setattr(signals, "run_detection",
                        lambda run_id, conn=None, allow_sold_out=True: calls.append(allow_sold_out) or [])
    monkeypatch.setattr(run_scraper.alerts, "evaluate_and_deliver", lambda *_a, **_k: {})
    _fake_live_scraper(monkeypatch, failed, succeeded)

    code = run_scraper.main(["--destinations", "TPA", "--days", "2", "--origins", "ILG"])

    assert database.last_runs(1)[0]["status"] == status
    assert calls == [allow_sold_out]
    assert code == (0 if status == "ok" else 1)


import frontier
from browser import ScrapeReport


def test_parse_airlines_and_promos():
    assert run_scraper.parse_airlines(None) == config.ACTIVE_AIRLINES
    assert run_scraper.parse_airlines("f9,xp,F9") == ["F9", "XP"]
    with pytest.raises(SystemExit):
        run_scraper.parse_airlines("XP,ZZ")
    assert run_scraper.parse_promos(None) == {}
    assert run_scraper.parse_promos(["save20", "f9=den10"]) == {"XP": "SAVE20", "F9": "DEN10"}
    with pytest.raises(SystemExit):
        run_scraper.parse_promos(["ZZ=X"])


def test_build_command_passes_airlines():
    cmd = build_command(["MCO"], date(2026, 11, 10), date(2026, 11, 15), origins=["PHL"], airlines=["f9"], python="py")
    assert cmd[cmd.index("--airlines") + 1] == "F9"
    assert "--airlines" not in build_command(["MCO"], date(2026, 11, 10), date(2026, 11, 15), python="py")


def test_frontier_routes_come_from_the_catalog(isolated, monkeypatch):
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL"), ("PHL", "DEN"), ("DEN", "PHL"), ("BWI", "MCO"), ("MCO", "BWI")], "route-page")
    args = build_parser().parse_args(["--airlines", "F9", "--origins", "PHL", "--destinations", "MCO"])
    assert run_scraper.routes_for("F9", args) == [("MCO", "PHL"), ("PHL", "MCO")]
    everything = build_parser().parse_args(["--airlines", "F9", "--origins", "PHL"])
    assert len(run_scraper.routes_for("F9", everything)) == 4
    assert ("ILG", "TPA") in run_scraper.routes_for("XP", build_parser().parse_args(["--destinations", "TPA", "--origins", "ILG"]))


class FakeScraper:
    made = []

    def __init__(self, **kw):
        self.kw = kw
        FakeScraper.made.append(self)

    async def run(self, routes, dates):
        r = ScrapeReport(attempted=2, succeeded=2, flights_written=5)
        if self.kw.get("promo_code") == "FAIL":
            r = ScrapeReport(attempted=2, succeeded=1, failed=1, failures=["x"], blocked=True)
        return r


def test_one_run_per_airline_with_its_own_status_and_promo(isolated, monkeypatch):
    FakeScraper.made = []
    monkeypatch.setattr(run_scraper, "scraper_class", lambda code: FakeScraper)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    database.init_db()
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    code = run_scraper.main(["--airlines", "XP,F9", "--origins", "ILG,PHL", "--destinations", "MCO,TPA", "--days", "5",
                             "--no-alerts", "--promo", "save20", "--promo", "F9=fail"])
    assert code == 1                                                     # the Frontier part was partial
    runs = {r["airline"]: r for r in database.last_runs(2)}
    assert (runs["XP"]["status"], runs["F9"]["status"]) == ("ok", "partial")
    assert runs["F9"]["summary"]["blocked"] is True and runs["XP"]["promo_code"] == "SAVE20"
    assert [s.kw["promo_code"] for s in FakeScraper.made] == ["SAVE20", "FAIL"]
    assert runs["F9"]["routes"] == ["MCO-PHL", "PHL-MCO"]


def test_airline_without_routes_is_skipped_not_failed(isolated, monkeypatch):
    monkeypatch.setattr(run_scraper, "scraper_class", lambda code: FakeScraper)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    assert run_scraper.main(["--airlines", "F9", "--origins", "PHL", "--destinations", "MCO", "--days", "3", "--no-alerts"]) == 0
    assert database.last_runs(1) == []                                    # empty catalog: nothing to do


def test_mock_mode_is_avelo_only(isolated):
    assert run_scraper.main(["--mock", "--airlines", "XP,F9", "--destinations", "TPA", "--days", "7", "--no-alerts", "--origins", "ILG"]) == 0
    assert [r["airline"] for r in database.last_runs(5)] == ["XP"]
