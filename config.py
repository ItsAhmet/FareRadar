"""Central configuration: the ILG hub, every nonstop destination, scraper tunables.

Origin is fixed to Wilmington, DE (ILG). Destinations are grouped by region for
the sidebar and the map, and carry coordinates so the optimizer can decide
whether two airports are close enough to mix-and-match (fly into one, home
from another).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("AVELO_DB_PATH", PROJECT_ROOT / "data" / "flights.db"))
SCREENSHOT_DIR = PROJECT_ROOT / "data" / "screenshots"

# --------------------------------------------------------------------------- #
# Airports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Airport:
    code: str
    city: str
    region: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.code} · {self.city}"


ORIGIN = "ILG"
ORIGIN_AIRPORT = Airport("ILG", "Wilmington, DE", "Hub", 39.6787, -75.6065)

# Every nonstop Avelo destination from ILG, grouped by region. Order matters:
# it is the order regions and airports appear in the UI and on the map legend.
DESTINATIONS: dict[str, Airport] = {
    a.code: a
    for a in (
        # Florida
        Airport("LAL", "Lakeland", "Florida", 27.9889, -82.0186),
        Airport("SRQ", "Sarasota", "Florida", 27.3954, -82.5544),
        Airport("TPA", "Tampa", "Florida", 27.9755, -82.5332),
        Airport("MCO", "Orlando", "Florida", 28.4294, -81.3090),
        Airport("RSW", "Fort Myers", "Florida", 26.5362, -81.7552),
        Airport("DAB", "Daytona Beach", "Florida", 29.1799, -81.0581),
        Airport("FLL", "Fort Lauderdale", "Florida", 26.0726, -80.1527),
        Airport("PBI", "West Palm Beach", "Florida", 26.6832, -80.0956),
        # Carolinas & Georgia
        Airport("MYR", "Myrtle Beach", "Carolinas & Georgia", 33.6797, -78.9283),
        Airport("ILM", "Wilmington, NC", "Carolinas & Georgia", 34.2706, -77.9026),
        Airport("SAV", "Savannah", "Carolinas & Georgia", 32.1276, -81.2021),
        Airport("CHS", "Charleston", "Carolinas & Georgia", 32.8986, -80.0405),
        Airport("GSP", "Greenville", "Carolinas & Georgia", 34.8957, -82.2189),
        Airport("RDU", "Raleigh/Durham", "Carolinas & Georgia", 35.8776, -78.7875),
        # Tennessee
        Airport("BNA", "Nashville", "Tennessee", 36.1245, -86.6782),
        # Caribbean
        Airport("SJU", "San Juan, PR", "Caribbean", 18.4394, -66.0018),
    )
}

REGIONS: list[str] = list(dict.fromkeys(a.region for a in DESTINATIONS.values()))

# Fixed categorical colours per region (Okabe–Ito, colour-blind safe), RGB.
REGION_COLORS: dict[str, tuple[int, int, int]] = {
    "Florida": (230, 159, 0),
    "Carolinas & Georgia": (0, 158, 115),
    "Tennessee": (204, 121, 167),
    "Caribbean": (0, 114, 178),
}
HUB_COLOR: tuple[int, int, int] = (31, 41, 55)
INACTIVE_COLOR: tuple[int, int, int] = (176, 183, 195)

ALL_AIRPORTS: dict[str, Airport] = {ORIGIN: ORIGIN_AIRPORT, **DESTINATIONS}


def region_codes(region: str) -> list[str]:
    return [c for c, a in DESTINATIONS.items() if a.region == region]


def airport_name(code: str) -> str:
    a = ALL_AIRPORTS.get(code)
    return a.city if a else code


def distance_miles(a: str, b: str) -> float:
    """Great-circle distance between two airport codes (0 if unknown)."""
    if a == b:
        return 0.0
    pa, pb = ALL_AIRPORTS.get(a), ALL_AIRPORTS.get(b)
    if not (pa and pb):
        return float("inf")
    lat1, lon1, lat2, lon2 = map(math.radians, (pa.lat, pa.lon, pb.lat, pb.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def default_routes(destinations: list[str] | None = None) -> list[tuple[str, str]]:
    """ILG→dest and dest→ILG for each destination (all of them by default)."""
    routes: list[tuple[str, str]] = []
    for code in destinations or list(DESTINATIONS):
        code = code.upper()
        routes.append((ORIGIN, code))
        routes.append((code, ORIGIN))
    return routes


# --------------------------------------------------------------------------- #
# Avelo PLUS membership
# --------------------------------------------------------------------------- #

# Membership benefits that would otherwise be paid add-ons. These are locked
# to $0 for the member: the optimizer's Total Getaway Cost is fares only.
MEMBER_BENEFITS: dict[str, float] = {
    "carry_on_bag": 0.0,
    "seat_selection": 0.0,
}
MEMBER_BADGE = "Avelo Plus Pricing Active (Free Carry-On & Seat Selection Applied)"

# For the ROI meter: what the membership costs per year, and what the free
# carry-on + seat selection would have cost a non-member per flight leg.
PLUS_ANNUAL_COST: float = float(os.getenv("AVELO_PLUS_ANNUAL_COST", "99"))
PLUS_VALUE_PER_LEG: float = float(os.getenv("AVELO_PLUS_VALUE_PER_LEG", "50"))
BOOKED_DROP_ALERT_USD: float = 10.0  # alert when a booked trip is now this much cheaper

# --------------------------------------------------------------------------- #
# Signals, alerts and scheduling
# --------------------------------------------------------------------------- #

# A route-wide "sale wave": at least SALE_WAVE_MIN_DAYS days compared, and at
# least SALE_WAVE_SHARE of them dropped by SALE_WAVE_DROP or more since the
# previous run of that route.
SALE_WAVE_MIN_DAYS: int = 4
SALE_WAVE_SHARE: float = 0.4
SALE_WAVE_DROP: float = 0.15

# A watched package alerts when its total falls by either threshold.
ALERT_MIN_DROP_USD: float = 5.0
ALERT_MIN_DROP_PCT: float = 0.05

# Push notifications: ntfy.sh needs only a topic name (pick something
# unguessable); email is optional and needs the SMTP settings.
NTFY_SERVER: str = os.getenv("AVELO_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC: str = os.getenv("AVELO_NTFY_TOPIC", "").strip()
SMTP_HOST: str = os.getenv("AVELO_SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("AVELO_SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("AVELO_SMTP_USER", "")
SMTP_PASS: str = os.getenv("AVELO_SMTP_PASS", "")
SMTP_TO: str = os.getenv("AVELO_SMTP_TO", "")
NOTIFY_TIMEOUT_S: int = 6

# Nightly job defaults (local time).
SCHEDULE_HOUR: int = int(os.getenv("AVELO_SCHEDULE_HOUR", "3"))
SCHEDULE_MINUTE: int = int(os.getenv("AVELO_SCHEDULE_MINUTE", "15"))
SCHEDULE_LABEL = "com.avelo-finder.scrape"

# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

HISTORY_RETENTION_DAYS: int = int(os.getenv("AVELO_HISTORY_DAYS", "120"))
SCRAPE_TIMEOUT_MIN: int = int(os.getenv("AVELO_SCRAPE_TIMEOUT_MIN", "60"))
SCRAPE_LOG = PROJECT_ROOT / "data" / "scrape.log"
OFFLINE: bool = os.getenv("AVELO_OFFLINE", "false").lower() in {"1", "true", "yes"}

# Open-Meteo: free, keyless daily forecast (16 days ahead).
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_S = 4

# --------------------------------------------------------------------------- #
# Scraper behaviour
# --------------------------------------------------------------------------- #

HEADLESS: bool = os.getenv("AVELO_HEADLESS", "true").lower() in {"1", "true", "yes"}
DAYS_AHEAD: int = int(os.getenv("AVELO_DAYS_AHEAD", "60"))
DELAY_RANGE: tuple[float, float] = (
    float(os.getenv("AVELO_DELAY_MIN", "2.0")),
    float(os.getenv("AVELO_DELAY_MAX", "5.0")),
)
MAX_RETRIES: int = 2
PAGE_TIMEOUT_MS: int = 45_000

# Stealth: cookies/localStorage persist between runs so each visit looks like
# the same returning visitor instead of a brand-new browser.
STORAGE_STATE_PATH = PROJECT_ROOT / "data" / "browser_state.json"
HUMAN_PAUSE_S: tuple[float, float] = (0.35, 1.4)  # between form actions
BROWSER_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
window.chrome = window.chrome || { runtime: {}, loadTimes() {}, csi() {} };
const _query = window.navigator.permissions && window.navigator.permissions.query;
if (_query) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : _query(p);
}
"""

# Avelo's booking flow is a Blazor WebAssembly app under /flight-search. The
# search form is driven with real clicks; fares are read from the rendered
# results. Selectors below were captured from the live site (Sep 2026) and are
# the first thing to check if a scrape suddenly returns nothing.
AVELO_SEARCH_URL = "https://www.aveloair.com/flight-search/search"
ROUTES_ENDPOINT_SUFFIX = "/services/routes/all"  # JSON schedule the page loads on start

SELECTORS: dict[str, str] = {
    "form_ready": "button.location",
    "location_button": "button.location",  # nth(0)=origin, nth(1)=destination
    "one_way_radio": "input[type=radio][value='False']",
    "lowfare_checkbox": "#lowfare-checkbox",
    "date_button": "button.date-display",
    "date_cell": "td.selectable",  # + [aria-label$='September 16, 2026']
    "submit": "button.submit-button",
    # Low Fare Calendar results page (/flight-search/lowfare)
    "lowfare_day": "td.day.selectable",
    "lowfare_month_tab": "div.month",  # id='10-2026'
    # Standard results page (/flight-search/select)
    "flight_card": "button.flight-block-wrapper-v2",
    # Promo code (search form)
    "promo_toggle": "a.link:has-text('promo code')",
    "promo_input": "input#promo",
    "promo_apply": "button.promo-apply",
    "promo_popover": ".promo-input-container",  # holds the popup + any validation text
    "popover_overlay": ".popover-background",
    # Fare tiers inside a day cell / flight card
    "member_fare": ".price.subscription, .fare.subscription .price",  # Avelo PLUS
    "public_fare": ".price.standard, .fare.standard .price",
}

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
)

VIEWPORTS: tuple[tuple[int, int], ...] = ((1366, 768), (1440, 900), (1536, 864), (1920, 1080))
