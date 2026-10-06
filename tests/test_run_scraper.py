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
    routes = config.default_routes(parse_destinations(args.destinations))
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
    assert build_parser().parse_args(cmd[2:]).promo == "SAVE20"
    assert "--promo" not in build_command(["CHS"], date(2026, 10, 1), date(2026, 10, 7), promo="  ", python="py")
