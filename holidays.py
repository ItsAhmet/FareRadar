"""US federal holidays, computed (no dependency), with observed-day rules.

Used to stretch a weekly free window around a long weekend: a Monday holiday
extends a window that ends Sunday; a holiday on the window's first day pulls
the start back an evening.
"""
from __future__ import annotations

from datetime import date, timedelta


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(d: date) -> date:
    """Federal rule: Saturday → Friday, Sunday → Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def federal_holidays(year: int) -> dict[date, str]:
    """Observed date → name for one year."""
    fixed = {
        (1, 1): "New Year's Day",
        (6, 19): "Juneteenth",
        (7, 4): "Independence Day",
        (11, 11): "Veterans Day",
        (12, 25): "Christmas Day",
    }
    out: dict[date, str] = {observed(date(year, m, d)): name for (m, d), name in fixed.items()}
    out[nth_weekday(year, 1, 0, 3)] = "MLK Day"
    out[nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    out[last_weekday(year, 5, 0)] = "Memorial Day"
    out[nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[nth_weekday(year, 10, 0, 2)] = "Columbus Day"
    out[nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    # New Year's of next year can be observed on Dec 31 of this year.
    ny_next = observed(date(year + 1, 1, 1))
    if ny_next.year == year:
        out[ny_next] = "New Year's Day"
    return dict(sorted(out.items()))


def holidays_between(start: date, end: date) -> dict[date, str]:
    out: dict[date, str] = {}
    for year in range(start.year, end.year + 1):
        out.update({d: n for d, n in federal_holidays(year).items() if start <= d <= end})
    return out
