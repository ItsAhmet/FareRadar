"""Shared data model used by the scraper, database, optimizer and dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Flight:
    """One nonstop flight on one date at one observed price.

    `price` is the fare we would pay: the Avelo PLUS member fare whenever the
    site shows one (`is_member_fare=True`), otherwise the public fare.
    `base_price` keeps the public fare alongside for comparison.
    """

    origin: str
    destination: str
    departure_datetime: datetime
    price: float
    currency: str = "USD"
    duration_minutes: int | None = None
    flight_number: str | None = None
    arrival_datetime: datetime | None = None
    base_price: float | None = None
    is_member_fare: bool = False
    promo_code: str | None = None  # promo applied when this fare was observed
    timestamp_scraped: datetime = field(default_factory=datetime.now)
    flight_id: str = ""  # stable identity for upserts; derived unless given
    airline: str = "XP"  # IATA code: XP = Avelo, F9 = Frontier

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", self.origin.strip().upper())
        object.__setattr__(self, "destination", self.destination.strip().upper())
        object.__setattr__(self, "airline", (self.airline or "XP").strip().upper())
        if not self.flight_id:
            object.__setattr__(self, "flight_id", self.make_id(self.flight_number, self.origin, self.destination))
        if self.arrival_datetime is None and self.duration_minutes:
            object.__setattr__(
                self, "arrival_datetime", self.departure_datetime + timedelta(minutes=self.duration_minutes)
            )

    @staticmethod
    def make_id(flight_number: str | None, origin: str, destination: str) -> str:
        return f"{(flight_number or 'UNK').upper()}-{origin.upper()}-{destination.upper()}"

    @property
    def departure_date(self) -> date:
        return self.departure_datetime.date()

    @property
    def route(self) -> str:
        return f"{self.origin}→{self.destination}"

    @property
    def time_known(self) -> bool:
        """Calendar scrapes only know the day; detail scrapes know the clock."""
        return self.arrival_datetime is not None

    @property
    def effective_arrival(self) -> datetime:
        return self.arrival_datetime or self.departure_datetime

    @property
    def savings(self) -> float:
        """How much the member fare saves over the public fare (0 if unknown)."""
        if self.is_member_fare and self.base_price is not None:
            return round(max(self.base_price - self.price, 0.0), 2)
        return 0.0
