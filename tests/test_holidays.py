from datetime import date

from holidays import federal_holidays, holidays_between, observed


def test_2026_federal_holidays():
    h = federal_holidays(2026)
    assert h[date(2026, 1, 19)] == "MLK Day"
    assert h[date(2026, 2, 16)] == "Presidents' Day"
    assert h[date(2026, 5, 25)] == "Memorial Day"
    assert h[date(2026, 6, 19)] == "Juneteenth"
    assert h[date(2026, 7, 3)] == "Independence Day"  # Jul 4 is a Saturday → observed Friday
    assert h[date(2026, 9, 7)] == "Labor Day"
    assert h[date(2026, 10, 12)] == "Columbus Day"
    assert h[date(2026, 11, 11)] == "Veterans Day"
    assert h[date(2026, 11, 26)] == "Thanksgiving"
    assert h[date(2026, 12, 25)] == "Christmas Day"
    assert date(2026, 7, 4) not in h


def test_observed_rules_and_year_boundary():
    assert observed(date(2021, 12, 25)) == date(2021, 12, 24)  # Saturday → Friday
    assert observed(date(2022, 1, 1)) == date(2021, 12, 31)  # Saturday → Friday, lands in 2021
    assert federal_holidays(2021)[date(2021, 12, 31)] == "New Year's Day"
    assert federal_holidays(2027)[date(2027, 1, 1)] == "New Year's Day"


def test_holidays_between_spans_years():
    h = holidays_between(date(2026, 12, 20), date(2027, 1, 20))
    assert set(h.values()) == {"Christmas Day", "New Year's Day", "MLK Day"}
