"""The airlines the app knows: names, membership programs, booking links.

Which airline flies which route is data (the `route_catalog` table), not code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import config


def frontier_search_url(origin: str, destination: str, day: date, calendar: bool = False, promo: str = "") -> str:
    """Frontier's search page for one route and day (it reads these URL parameters)."""
    f9 = AIRLINES["F9"]
    when = quote(f"{day:%b} {day.day}, {day.year}")
    url = (f"{config.FRONTIER_SELECT_URL}?o1={f9.site_code(origin)}&d1={f9.site_code(destination)}"
           f"&dd1={when}&ADT=1&mon=true")
    if calendar:
        url += "&c=true"
    return f"{url}&promo={(promo or '').strip().upper()}"


@dataclass(frozen=True)
class Airline:
    code: str
    name: str
    member_program: str
    color: tuple[int, int, int]
    # Is the fare calendar's price for a day a fare the owner can book on a nonstop? Avelo's is. Frontier's is
    # the lowest Discount Den total of any routing, often a connection: a different price basis from a day search.
    calendar_bookable: bool = True

    def site_code(self, app_code: str) -> str:
        """The airport code this airline's site uses for one of the app's codes."""
        code = app_code.strip().upper()
        return config.FRONTIER_SITE_CODES.get(code, code) if self.code == "F9" else code

    def app_code(self, site_code: str) -> str:
        code = site_code.strip().upper()
        if self.code == "F9":
            return {site: app for app, site in config.FRONTIER_SITE_CODES.items()}.get(code, code)
        return code

    def booking_url(self, origin: str, destination: str, day: date) -> str | None:
        """A link that opens this leg's search, or None when the site needs the assisted flow."""
        return frontier_search_url(origin, destination, day) if self.code == "F9" else None


AIRLINES: dict[str, Airline] = {
    "XP": Airline("XP", "Avelo", "Avelo PLUS", (88, 28, 135)),
    "F9": Airline("F9", "Frontier", "Discount Den", (0, 102, 67), calendar_bookable=False),
}


def get(code: str) -> Airline:
    return AIRLINES[code.strip().upper()]


def owner_price(member: bool, member_price: float | None, base_price: float | None) -> tuple[float | None, bool]:
    """(the fare the owner would pay, whether it is the program fare)."""
    if member and member_price is not None:
        return member_price, True
    if base_price is not None:
        return base_price, False
    return member_price, False  # only a program fare is known (Frontier's calendar): the best estimate
