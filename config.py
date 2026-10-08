"""Central configuration: home airports, every nonstop destination, scraper tunables.

Home airports (ILG, PHL, BWI) are where trips start and end; which ones are
active comes from AVELO_HOME_AIRPORTS. Destinations are grouped by region for
the sidebar and the map, and carry coordinates so the optimizer can decide
whether two airports are close enough to mix-and-match.
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


@dataclass(frozen=True)
class HomeCost:
    """Owner-set cost of starting a trip at a home airport."""

    drive_min: float | None = None  # minutes from your home; None = not set, not counted
    parking_per_day: float = 0.0


HOME_AIRPORT_INFO: dict[str, Airport] = {
    a.code: a
    for a in (
        Airport("ILG", "Wilmington, DE", "Home", 39.6787, -75.6065),
        Airport("PHL", "Philadelphia", "Home", 39.8744, -75.2424),
        Airport("BWI", "Baltimore", "Home", 39.1754, -76.6683),
    )
}

# Parking defaults are estimates. Overrides live in the settings table (database.set_home_cost);
# They are editable in the dashboard's Settings panel.
DEFAULT_HOME_COSTS: dict[str, HomeCost] = {
    "ILG": HomeCost(None, 0.0),
    "PHL": HomeCost(None, 17.0),
    "BWI": HomeCost(None, 14.0),
}


def parse_home_codes(raw: str) -> list[str]:
    """Known home codes from a comma list, in order, de-duplicated; ILG if none."""
    codes = [c.strip().upper() for c in (raw or "").split(",") if c.strip()]
    known = [c for c in dict.fromkeys(codes) if c in HOME_AIRPORT_INFO]
    return known or ["ILG"]


HOME_CODES: list[str] = parse_home_codes(os.getenv("AVELO_HOME_AIRPORTS", "ILG,PHL,BWI"))
HOME_AIRPORTS: dict[str, Airport] = {c: HOME_AIRPORT_INFO[c] for c in HOME_CODES}
PRIMARY_HOME: str = HOME_CODES[0]


def is_home(code: str) -> bool:
    return code.strip().upper() in HOME_AIRPORT_INFO


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

# Fixed categorical colours per region (Okabe–Ito, colour-blind safe), RGB.
REGION_COLORS: dict[str, tuple[int, int, int]] = {
    "Florida": (230, 159, 0),
    "Carolinas & Georgia": (0, 158, 115),
    "Tennessee": (204, 121, 167),
    "Caribbean": (0, 114, 178),
    "Texas & Gulf": (213, 94, 0),
    "West": (86, 180, 233),
    "Midwest": (240, 228, 66),
    "Mexico & Central America": (120, 120, 120),
}
HUB_COLOR: tuple[int, int, int] = (31, 41, 55)
INACTIVE_COLOR: tuple[int, int, int] = (176, 183, 195)

# Airports only Frontier flies from the home airports. Kept apart from DESTINATIONS (Avelo's list); ALL_DESTINATIONS
# is the union the dashboard offers.
FRONTIER_AIRPORTS: dict[str, Airport] = {
    a.code: a
    for a in (
        Airport("MIA", "Miami", "Florida", 25.7959, -80.2870),
        Airport("JAX", "Jacksonville", "Florida", 30.4941, -81.6879),
        Airport("ATL", "Atlanta", "Carolinas & Georgia", 33.6407, -84.4277),
        Airport("CLT", "Charlotte", "Carolinas & Georgia", 35.2144, -80.9473),
        Airport("DFW", "Dallas/Fort Worth", "Texas & Gulf", 32.8998, -97.0403),
        Airport("IAH", "Houston", "Texas & Gulf", 29.9902, -95.3368),
        Airport("MSY", "New Orleans", "Texas & Gulf", 29.9934, -90.2580),
        Airport("DEN", "Denver", "West", 39.8561, -104.6737),
        Airport("LAS", "Las Vegas", "West", 36.0840, -115.1537),
        Airport("PHX", "Phoenix", "West", 33.4352, -112.0101),
        Airport("LAX", "Los Angeles", "West", 33.9416, -118.4085),
        Airport("SFO", "San Francisco", "West", 37.6213, -122.3790),
        Airport("DTW", "Detroit", "Midwest", 42.2162, -83.3554),
        Airport("IND", "Indianapolis", "Midwest", 39.7169, -86.2956),
        Airport("MSP", "Minneapolis", "Midwest", 44.8848, -93.2223),
        Airport("ORD", "Chicago", "Midwest", 41.9742, -87.9073),
        Airport("CUN", "Cancún", "Mexico & Central America", 21.0365, -86.8771),
        Airport("SAL", "San Salvador", "Mexico & Central America", 13.4409, -89.0557),
        Airport("PUJ", "Punta Cana", "Caribbean", 18.5674, -68.3634),
        Airport("SDQ", "Santo Domingo", "Caribbean", 18.4297, -69.6689),
        Airport("MBJ", "Montego Bay", "Caribbean", 18.5037, -77.9134),
    )
}

ALL_DESTINATIONS: dict[str, Airport] = {**DESTINATIONS, **FRONTIER_AIRPORTS}
# Avelo destinations Frontier also sells from PHL/BWI. Only a fallback for the dashboard's destination list
# when the route catalog has not been filled yet; the catalog is the truth once it has.
FRONTIER_SHARED_DESTINATIONS: tuple[str, ...] = ("MCO", "TPA", "RSW", "SJU", "BNA", "RDU", "PBI")

# Avelo's four regions first, in today's order, then the regions only Frontier adds.
REGIONS: list[str] = list(dict.fromkeys(a.region for a in ALL_DESTINATIONS.values()))

ALL_AIRPORTS: dict[str, Airport] = {**HOME_AIRPORT_INFO, **ALL_DESTINATIONS}

# --------------------------------------------------------------------------- #
# Airlines
# --------------------------------------------------------------------------- #

KNOWN_AIRLINES: tuple[str, ...] = ("XP", "F9")  # Avelo, Frontier
DEFAULT_MEMBERSHIPS: dict[str, bool] = {"XP": True, "F9": False}  # Avelo PLUS member; not in Discount Den

# What one carry-on bag costs per flight leg, per airline. Estimates the owner can change (env): only added to a
# package's cost when a query asks (count_carry_on, the sidebar's "Count a carry-on bag"), so a bare fare is never compared with a fare that includes a bag.
CARRY_ON_FEE: dict[str, float] = {
    "XP": float(os.getenv("AVELO_CARRY_ON_XP", "0")),
    "F9": float(os.getenv("AVELO_CARRY_ON_F9", "55")),
}


def parse_airline_codes(raw: str) -> list[str]:
    """Known airline codes from a comma list, in order, de-duplicated; Avelo if none."""
    codes = [c.strip().upper() for c in (raw or "").split(",") if c.strip()]
    known = [c for c in dict.fromkeys(codes) if c in KNOWN_AIRLINES]
    return known or ["XP"]


# Which airlines a scrape covers when --airlines is not given, and the cap for the nightly job (--from-rules): a
# saved rule or watch never adds an airline that is not listed here.
ACTIVE_AIRLINES: list[str] = parse_airline_codes(os.getenv("AVELO_AIRLINES", "XP,F9"))

FRONTIER_HOME_URL = "https://www.flyfrontier.com/"
FRONTIER_SELECT_URL = "https://booking.flyfrontier.com/Flight/InternalSelect"
FRONTIER_CALENDAR_ENDPOINT = "GetLowFareCalendarAvailability"  # POSTed by the calendar view
FRONTIER_ROUTES_URL = "https://flights.flyfrontier.com/en/flights-from-{slug}"
CATALOG_REFRESH_DAYS = 7  # a scrape re-reads a Frontier route page whose listing is older than this
FRONTIER_ROUTE_PAGES: dict[str, str] = {"PHL": "philadelphia", "BWI": "baltimore"}  # home airport → page slug
FRONTIER_SITE_CODES: dict[str, str] = {"PBI": "DJT"}  # app code → the code Frontier's site uses
# Day searches used to learn whether a route has nonstops: one per weekday, because schedules repeat weekly and
# a route flown nonstop a few days a week is otherwise missed (and then skipped for a month).
FRONTIER_PROBE_DAYS: int = 7


def region_codes(region: str) -> list[str]:
    return [c for c, a in ALL_DESTINATIONS.items() if a.region == region]


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


def default_routes(destinations: list[str] | None = None, origins: list[str] | None = None) -> list[tuple[str, str]]:
    """home→dest and dest→home for each destination and each home airport
    (all destinations and all active home airports by default)."""
    routes: list[tuple[str, str]] = []
    for code in destinations or list(DESTINATIONS):
        code = code.upper()
        for home in origins or HOME_CODES:
            home = home.upper()
            routes.append((home, code))
            routes.append((code, home))
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
MEMBER_BADGE = "Avelo Plus Pricing Active (Free Carry-On & Seat Selection on Avelo legs)"

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

# Stealth: cookies (only cookies, never localStorage) persist between runs so each
# visit looks like the same returning visitor instead of a brand-new browser.
STORAGE_STATE_PATH = PROJECT_ROOT / "data" / "browser_state.json"


def storage_state_path(airline: str) -> Path:
    """Per-airline cookie file, so one site's session never reaches the other."""
    return PROJECT_ROOT / "data" / f"browser_state_{airline.strip().upper()}.json"

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
    # Oct 2026 layout: one div.main-block per flight with fare bundles; the button is the Sep 2026 card.
    "flight_card": "div.main-block, button.flight-block-wrapper-v2",
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
