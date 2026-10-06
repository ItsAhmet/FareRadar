from datetime import date

import config
from weather import forecast, parse_forecast


def test_parse_forecast_filters_to_trip_days():
    payload = {
        "daily": {
            "time": ["2026-10-07", "2026-10-08", "2026-10-09"],
            "temperature_2m_max": [84.1, 86.0, 80.2],
            "temperature_2m_min": [70.0, 71.5, 69.0],
            "precipitation_probability_max": [10, 60, 0],
            "weathercode": [1, 80, 0],
        }
    }
    rows = parse_forecast(payload, date(2026, 10, 8), date(2026, 10, 9))
    assert [r["day"] for r in rows] == [date(2026, 10, 8), date(2026, 10, 9)]
    assert rows[0]["sky"] == "Showers" and rows[0]["rain_pct"] == 60
    assert parse_forecast({}, date(2026, 10, 8), date(2026, 10, 9)) == []


def test_forecast_returns_none_when_offline(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", True)
    assert forecast("TPA", date(2026, 10, 8), date(2026, 10, 9)) is None
    assert forecast("XXX", date(2026, 10, 8), date(2026, 10, 9)) is None


def test_sun_score_and_slicing():
    from weather import mean_high, score, slice_days

    rows = [
        {"day": date(2026, 10, 8), "high_f": 90, "low_f": 70, "rain_pct": 0, "sky": "Clear"},
        {"day": date(2026, 10, 9), "high_f": 60, "low_f": 50, "rain_pct": 100, "sky": "Rain"},
        {"day": date(2026, 10, 10), "high_f": 75, "low_f": 60, "rain_pct": 50, "sky": "Showers"},
    ]
    assert score(rows[:1]) == 1.0
    assert score(rows[1:2]) == 0.0
    assert score(rows[2:3]) == 0.25
    assert score([]) is None
    assert [r["day"].day for r in slice_days(rows, date(2026, 10, 9), date(2026, 10, 10))] == [9, 10]
    assert mean_high(rows) == 75.0
