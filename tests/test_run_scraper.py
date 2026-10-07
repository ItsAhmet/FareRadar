from datetime import date, timedelta

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


# ---- watches are evaluated once per invocation, after every airline (Task 8 fix round 1); an airline's
# ---- route signals are pushed right after its run when another airline follows (final review fix) ----
import alerts
import signals
from signals import Event

BOTH = ["--airlines", "XP,F9", "--origins", "ILG,PHL", "--destinations", "MCO,TPA", "--days", "5"]


def _two_airlines(monkeypatch, plan, calls=None):
    """XP and F9 scrapers that return a clean report, or raise when `plan[code]` is an exception; the F9
    catalog lists PHL⇄MCO; signal detection yields one event per run; alert delivery is recorded as
    `deliver` (the final evaluation: run id, signal messages) and `early` (signals pushed between airlines)."""
    def cls_for(code):
        class Fake:
            def __init__(self, **_kw):
                pass

            async def run(self, routes, dates):
                if calls is not None:
                    calls.append((code, list(routes)))
                if isinstance(plan.get(code), BaseException):
                    raise plan[code]
                return ScrapeReport(attempted=2, succeeded=2, flights_written=3)
        return Fake

    seen = {"detect": [], "deliver": [], "early": []}

    def detect(run_id, conn=None, allow_sold_out=True):
        seen["detect"].append(run_id)
        if isinstance(plan.get("detect"), BaseException):
            raise plan["detect"]
        return [Event("sale_wave", "ILG", "TPA", date(2026, 11, 1), f"run{run_id}")]

    def deliver(run_id, events, conn=None):
        seen["deliver"].append((run_id, [e.message for e in events]))
        if isinstance(plan.get("deliver"), BaseException):
            raise plan["deliver"]
        return {"pushed": len(events)}

    def early(events, conn=None):
        if isinstance(plan.get("early"), BaseException):
            raise plan["early"]
        seen["early"].append([e.message for e in events])
        return {"pushed": len(events)}

    monkeypatch.setattr(run_scraper, "scraper_class", cls_for)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    monkeypatch.setattr(signals, "run_detection", detect)
    monkeypatch.setattr(alerts, "evaluate_and_deliver", deliver)
    monkeypatch.setattr(alerts, "deliver_signals", early)
    database.init_db()
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    return seen


def test_two_airlines_push_each_signal_once_and_evaluate_watches_once(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {})
    assert run_scraper.main(BOTH) == 0
    assert seen["detect"] == [1, 2]
    assert seen["early"] == [["run1"]]                                    # Avelo's signals: before Frontier starts
    assert seen["deliver"] == [(2, ["run2"])]                             # one evaluation, after the last airline
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert runs["F9"]["summary"]["alerts"] == {"pushed": 1} and "alerts" not in runs["XP"]["summary"]
    assert runs["XP"]["summary"]["signal_alerts"] == {"pushed": 1} and runs["XP"]["status"] == "ok"
    assert runs["XP"]["summary"]["signals"] == runs["F9"]["summary"]["signals"] == 1
    assert runs["F9"]["status"] == "ok" and runs["F9"]["summary"]["flights_written"] == 3


def test_last_airline_crash_still_alerts_for_the_airlines_that_finished(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {"F9": RuntimeError("chromium failed to launch")})
    assert run_scraper.main(BOTH) == 1
    assert seen["detect"] == [1]                                          # the crashed run detects nothing
    assert seen["early"] == [["run1"]]                                    # Avelo's events are still pushed, once
    assert seen["deliver"] == [(2, [])]                                   # and watches are still evaluated
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert runs["XP"]["status"] == "ok" and runs["F9"]["status"] == "failed"
    assert "chromium" in runs["F9"]["summary"]["error"] and runs["F9"]["summary"]["alerts"] == {"pushed": 0}


@pytest.mark.parametrize("death", [KeyboardInterrupt(), SystemExit(137)])
def test_scrape_killed_during_the_second_airline_has_already_pushed_the_first_airlines_signals(isolated, monkeypatch, death):
    """Dashboard Cancel, its 60-minute timeout or Ctrl-C while Frontier is still scraping. Avelo's run is
    already finished as 'ok', so the next run takes it as its baseline and never detects that sale again."""
    seen = _two_airlines(monkeypatch, {"F9": death})
    with pytest.raises(type(death)):
        run_scraper.main(BOTH)
    assert seen["early"] == [["run1"]] and seen["deliver"] == []          # pushed before the kill; no final evaluation
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert runs["XP"]["status"] == "ok" and runs["XP"]["summary"]["signal_alerts"] == {"pushed": 1}


def test_one_airline_scrape_still_sends_a_single_combined_alert(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {})
    assert run_scraper.main(["--airlines", "XP", "--origins", "ILG", "--destinations", "TPA", "--days", "5"]) == 0
    assert seen["early"] == [] and seen["deliver"] == [(1, ["run1"])]     # signals and watches in one message, as before


def test_failed_early_signal_push_is_retried_with_the_final_evaluation(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {"early": RuntimeError("ntfy down")})
    assert run_scraper.main(BOTH) == 0
    assert seen["early"] == [] and seen["deliver"] == [(2, ["run1", "run2"])]
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert "signal_alerts" not in runs["XP"]["summary"] and runs["F9"]["status"] == "ok"


def test_deliver_signals_pushes_route_signals_on_watched_routes_only(isolated, monkeypatch):
    database.init_db()
    sent = []
    monkeypatch.setattr(alerts, "deliver", lambda pushes: sent.append([e.message for e in pushes]) or alerts.Delivery(pushed=len(pushes)))
    monkeypatch.setattr(alerts, "watched_routes", lambda conn=None: {("ILG", "TPA")})
    monkeypatch.setattr(alerts, "evaluate_packages", lambda *_a, **_k: 1 / 0)       # watches are not evaluated here
    events = [Event("sale_wave", "ILG", "TPA", date(2026, 11, 1), "watched"), Event("sale_wave", "ILG", "LAL", date(2026, 11, 1), "unwatched")]
    assert alerts.deliver_signals(events) == {"pushed": 1, "ntfy": None, "email": None}
    assert sent == [["watched"]]
    assert not (isolated / "scrape.lock").exists()


def test_signal_detection_crash_in_the_last_airline_still_alerts(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {})
    real = signals.run_detection
    monkeypatch.setattr(signals, "run_detection",
                        lambda run_id, **k: real(run_id, **k) if run_id == 1 else (_ for _ in ()).throw(RuntimeError("detect")))
    assert run_scraper.main(BOTH) == 1
    assert seen["early"] == [["run1"]] and seen["deliver"] == [(2, [])]


def test_alert_failure_is_logged_sets_exit_code_and_keeps_the_runs(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {"deliver": RuntimeError("ntfy down")})
    assert run_scraper.main(BOTH) == 1
    assert len(seen["deliver"]) == 1
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert (runs["XP"]["status"], runs["F9"]["status"]) == ("ok", "ok")   # the scrapes themselves were fine
    assert "ntfy down" in runs["F9"]["summary"]["alerts_error"]
    assert not (isolated / "scrape.lock").exists()


def test_no_alerts_flag_and_no_run_rows_mean_no_alert_evaluation(isolated, monkeypatch):
    seen = _two_airlines(monkeypatch, {})
    assert run_scraper.main(BOTH + ["--no-alerts"]) == 0
    assert seen == {"detect": [], "deliver": [], "early": []}
    # no airline has routes -> no run row -> nothing to evaluate
    assert run_scraper.main(["--airlines", "F9", "--origins", "BWI", "--destinations", "DEN", "--days", "3"]) == 0
    assert seen["deliver"] == [] and len(database.last_runs(5)) == 2


def test_latest_run_signals_still_include_avelo_after_a_two_airline_scrape(isolated, monkeypatch):
    """The dashboard reads list_events(run_ids=latest_run_ids()); with one run row per airline the newest
    row alone (Frontier's) would hide every signal the Avelo run raised in the same invocation."""
    days = [date.today() + timedelta(days=i) for i in range(2, 8)]
    price = {"v": 100.0}

    def cls_for(code):
        class Fake:
            def __init__(self, **kw):
                self.run_id = kw["run_id"]

            async def run(self, routes, dates):
                if code == "XP":
                    with database.connection() as conn:
                        conn.executemany(
                            "INSERT INTO route_day_seen (run_id, origin, destination, day, price) VALUES (?,?,?,?,?)",
                            [(self.run_id, "ILG", "TPA", d.isoformat(), price["v"]) for d in days])
                return ScrapeReport(attempted=1, succeeded=1)
        return Fake

    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(run_scraper, "scraper_class", cls_for)
    monkeypatch.setattr(frontier, "refresh_catalog", lambda origins, **_k: 0)
    monkeypatch.setattr(alerts, "deliver", lambda pushes: alerts.Delivery(pushed=len(pushes)))
    database.init_db()
    database.upsert_catalog("F9", [("PHL", "MCO"), ("MCO", "PHL")], "route-page")
    args = ["--airlines", "XP,F9", "--origins", "ILG,PHL", "--destinations", "MCO,TPA", "--days", "10"]
    assert run_scraper.main(args) == 0
    price["v"] = 60.0                                                     # a 40% sale on every day
    assert run_scraper.main(args) == 0
    runs = database.last_runs(2)
    assert [r["airline"] for r in runs] == ["F9", "XP"]
    assert database.latest_run_ids() == [runs[0]["run_id"], runs[1]["run_id"]]
    latest = database.list_events(limit=1000, run_ids=database.latest_run_ids())
    assert any(e["kind"] == "sale_wave" and e["run_id"] == runs[1]["run_id"] for e in latest)


# ---- --routes with several airlines: each airline gets only the routes it could fly ----
def test_can_fly():
    assert run_scraper.can_fly("XP", ("ILG", "TPA")) and run_scraper.can_fly("XP", ("MCO", "PHL"))
    assert not run_scraper.can_fly("XP", ("PHL", "DEN"))                  # Denver is not an Avelo destination
    assert run_scraper.can_fly("F9", ("PHL", "DEN")) and run_scraper.can_fly("F9", ("MCO", "BWI"))
    assert run_scraper.can_fly("F9", ("PHL", "XYZ"))                      # off-catalog Frontier route stays allowed
    assert not run_scraper.can_fly("F9", ("ILG", "TPA"))                  # Frontier does not fly from Wilmington


def test_routes_flag_with_several_airlines_is_split_per_airline(isolated, monkeypatch):
    calls = []
    _two_airlines(monkeypatch, {}, calls)
    code = run_scraper.main(["--airlines", "XP,F9", "--routes", "ILG-TPA,PHL-DEN,phl-mco", "--days", "3", "--no-alerts"])
    assert code == 0
    assert calls == [("XP", [("ILG", "TPA"), ("PHL", "MCO")]), ("F9", [("PHL", "DEN"), ("PHL", "MCO")])]
    runs = {r["airline"]: r for r in database.last_runs(5)}
    assert runs["XP"]["routes"] == ["ILG-TPA", "PHL-MCO"] and runs["F9"]["routes"] == ["PHL-DEN", "PHL-MCO"]


def test_routes_flag_avelo_only_route_skips_frontier_instead_of_failing_it(isolated, monkeypatch):
    calls = []
    _two_airlines(monkeypatch, {"F9": RuntimeError("Frontier must not be sent to ILG-TPA")}, calls)
    assert run_scraper.main(["--airlines", "XP,F9", "--routes", "ILG-TPA", "--days", "3", "--no-alerts"]) == 0
    assert calls == [("XP", [("ILG", "TPA")])]
    assert [r["airline"] for r in database.last_runs(5)] == ["XP"]


def test_routes_flag_with_one_airline_is_taken_as_given(isolated, monkeypatch):
    calls = []
    _two_airlines(monkeypatch, {}, calls)
    assert run_scraper.main(["--airlines", "F9", "--routes", "ILG-TPA", "--days", "3", "--no-alerts"]) == 0
    assert run_scraper.main(["--routes", "PHL-DEN", "--days", "3", "--no-alerts"]) == 0
    assert calls == [("F9", [("ILG", "TPA")]), ("XP", [("PHL", "DEN")])]
