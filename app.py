"""Streamlit dashboard: Flexible Getaways from your home airports on Avelo.

Run with:  streamlit run app.py

Nothing is scraped on page load. The sidebar collects destinations, the weekly
free window, the date span and budget; results come from whatever is already
in SQLite. "Execute Target Scrape" runs the scraper for exactly that selection.
"""
from __future__ import annotations

import os
import subprocess
import time as _time
from datetime import date, datetime, time, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st

import config
import database
import alerts
import bookings
import places
import runlock
import scheduler
import weather
from handoff import handoff_command
from planner import plan_season
from optimizer import (
    BENEFITS,
    WEEKDAYS,
    Availability,
    GetawayQuery,
    Itinerary,
    clear_drives,
    enrich_drives,
    enrich_home,
    enrich_weather,
    find_itineraries,
    intersect_windows,
    rank_by_door_to_door,
    rank_by_price,
    rank_by_schedule,
    rank_by_sun,
    to_dataframe,
    wildcard,
)
from run_scraper import build_command

st.set_page_config(page_title="Avelo Getaways", page_icon="✈️", layout="wide")

PRICE_SCALE = ["#e8f1fb", "#b9d3f0", "#84b0e3", "#4f8bd3", "#2a63b8", "#153f8a"]  # one hue, light = cheap
ACCENT = "#2a63b8"
ALL_CODES = list(config.DESTINATIONS)


# --------------------------------------------------------------------------- #
# Data access (cached; keyed on the DB file's mtime so a scrape invalidates)
# --------------------------------------------------------------------------- #


def _db_version() -> float:
    try:
        return os.path.getmtime(config.DB_PATH)
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def load_selection_stats(destinations: tuple[str, ...], start: date, end: date, _version: float) -> dict:
    return database.selection_stats(config.HOME_CODES, list(destinations), start, end)


@st.cache_data(show_spinner=False)
def load_calendar(start: date, end: date, _version: float) -> pd.DataFrame:
    return pd.DataFrame(database.route_calendar(start, end, airlines=["XP"]))  # Avelo only until Phase 3


@st.cache_data(show_spinner=False)
def load_freshness(destinations: tuple[str, ...], _version: float) -> pd.DataFrame:
    return pd.DataFrame(database.route_freshness(config.HOME_CODES, list(destinations)))


@st.cache_data(show_spinner=False)
def load_history(flight_id: str, departure_iso: str, _version: float) -> pd.DataFrame:
    rows = database.price_history(flight_id, datetime.fromisoformat(departure_iso))
    df = pd.DataFrame(rows)
    if not df.empty:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"])
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_weather(code: str, start: date, end: date) -> list[dict] | None:
    return weather.forecast(code, start, end)


@st.cache_data(ttl=3600, show_spinner=False)
def load_forecast_all(code: str) -> list[dict] | None:
    return weather.forecast_all(code)


@st.cache_data(ttl=86400, show_spinner=False)
def load_place(query: str) -> places.Place | None:
    return places.geocode(query)


@st.cache_data(ttl=86400, show_spinner=False)
def load_drives(query: str, airports: tuple[str, ...]) -> dict[str, tuple[float, str]]:
    place = places.geocode(query)
    return places.drive_table(place, list(airports)) if place else {}


@st.cache_data(show_spinner=False)
def load_signal_map(_version: float) -> dict[tuple[str, str, str], set[str]]:
    """(origin, destination, day) → signal kinds from the most recent run."""
    runs = database.last_runs(1)
    if not runs:
        return {}
    out: dict[tuple[str, str, str], set[str]] = {}
    for e in database.list_events(limit=1000, run_id=runs[0]["run_id"]):
        if e["origin"] and e["destination"] and e["day"]:
            out.setdefault((e["origin"], e["destination"], e["day"]), set()).add(e["kind"])
    return out


SIGNAL_ICONS = {"floor_fare": "🔥 floor", "new_floor": "🔥 new low", "sale_wave": "🏷 sale", "floor_gone": "⬆ floor gone", "sold_out": "⛔ sold out"}


def signal_text(it: Itinerary, sig: dict) -> str:
    kinds: set[str] = set()
    for leg in (it.outbound, it.inbound):
        kinds |= sig.get((leg.origin, leg.destination, leg.departure_date.isoformat()), set())
    return " · ".join(SIGNAL_ICONS[k] for k in SIGNAL_ICONS if k in kinds)


@st.cache_resource(show_spinner=False)
def _search_cache() -> dict:
    return {}


def run_search(query: GetawayQuery) -> list[Itinerary]:
    key = (
        tuple(sorted(query.destinations)),
        tuple((w.start, w.end) for w in query.windows),
        query.min_nights,
        query.max_budget,
        query.allow_mix,
        query.max_pair_miles,
        tuple(query.origins),
        query.allow_open_jaw,
        _db_version(),
    )
    cache = _search_cache()
    if key not in cache:
        cache.clear()
        cache[key] = find_itineraries(query)
    return cache[key]


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #


def _init_selection_state() -> None:
    if "dest_select" not in st.session_state:
        st.session_state.dest_select = list(ALL_CODES)
        st.session_state.select_all = True
        for region in config.REGIONS:
            st.session_state[f"region_{region}"] = True


def _on_select_all() -> None:
    on = st.session_state.select_all
    st.session_state.dest_select = list(ALL_CODES) if on else []
    for region in config.REGIONS:
        st.session_state[f"region_{region}"] = on


def _on_region(region: str) -> None:
    current = set(st.session_state.dest_select)
    codes = set(config.region_codes(region))
    current = current | codes if st.session_state[f"region_{region}"] else current - codes
    st.session_state.dest_select = [c for c in ALL_CODES if c in current]
    st.session_state.select_all = len(current) == len(ALL_CODES)


def _on_airports() -> None:
    current = set(st.session_state.dest_select)
    for region in config.REGIONS:
        st.session_state[f"region_{region}"] = set(config.region_codes(region)) <= current
    st.session_state.select_all = len(current) == len(ALL_CODES)


def destination_picker() -> list[str]:
    _init_selection_state()
    st.sidebar.subheader("Destinations")
    st.sidebar.checkbox("Select all routes", key="select_all", on_change=_on_select_all)
    cols = st.sidebar.columns(2)
    for i, region in enumerate(config.REGIONS):
        cols[i % 2].checkbox(
            f"{region} ({len(config.region_codes(region))})",
            key=f"region_{region}",
            on_change=_on_region,
            args=(region,),
        )
    st.sidebar.multiselect(
        "Airports",
        options=ALL_CODES,
        key="dest_select",
        on_change=_on_airports,
        format_func=lambda c: config.DESTINATIONS[c].label,
    )
    return list(st.session_state.dest_select)


def _availability_inputs(container, prefix: str, start_idx: int = 2, end_idx: int = 6) -> Availability:
    c1, c2 = container.columns([3, 2])
    start_day = c1.selectbox("Free from", WEEKDAYS, index=start_idx, key=f"{prefix}_from_day")
    start_time = c2.time_input("at", value=time(18, 0), key=f"{prefix}_from_time", step=timedelta(minutes=30))
    c3, c4 = container.columns([3, 2])
    end_day = c3.selectbox("Back by", WEEKDAYS, index=end_idx, key=f"{prefix}_to_day")
    end_time = c4.time_input("at", value=time(23, 59), key=f"{prefix}_to_time", step=timedelta(minutes=30))
    return Availability(WEEKDAYS.index(start_day), start_time, WEEKDAYS.index(end_day), end_time)


def availability_picker() -> tuple[Availability, Availability | None, bool]:
    st.sidebar.subheader("When are you free?")
    mine = _availability_inputs(st.sidebar, "me")
    holiday_extend = st.sidebar.checkbox(
        "Extend around US holidays", value=True,
        help="A Monday holiday stretches the window through Monday; a Friday holiday pulls the start to Thursday evening.",
    )
    partner = None
    with st.sidebar.expander("👥 Travel partner's window"):
        if st.checkbox("Only when we're both free", key="partner_on"):
            partner = _availability_inputs(st, "partner", start_idx=4, end_idx=6)
    return mine, partner, holiday_extend


def sidebar() -> tuple[GetawayQuery | None, list[str], tuple[date, date], str, dict]:
    st.sidebar.title("✈️ Avelo Getaways")
    st.sidebar.caption(f"Nonstop on Avelo from {' · '.join(config.HOME_CODES)}")

    today = date.today()
    if st.sidebar.button("⚡ This weekend", help="Next 5 days, every destination, scrape now", width="stretch"):
        _init_selection_state()
        st.session_state.dest_select = list(ALL_CODES)
        st.session_state.select_all = True
        for region in config.REGIONS:
            st.session_state[f"region_{region}"] = True
        st.session_state.span = (today, today + timedelta(days=5))
        st.session_state.pending_scrape = {"destinations": list(ALL_CODES), "start": today, "end": today + timedelta(days=5)}
        st.rerun()

    destinations = destination_picker()
    availability, partner, holiday_extend = availability_picker()

    st.sidebar.subheader("Search span & limits")
    if "span" not in st.session_state:
        st.session_state.span = (today + timedelta(days=7), today + timedelta(days=60))
    span = st.sidebar.date_input(
        "Look for getaways between",
        min_value=today,
        max_value=today + timedelta(days=330),
        key="span",
    )
    if isinstance(span, tuple) and len(span) == 2:
        span_start, span_end = span
    else:
        span_start = span_end = span[0] if isinstance(span, tuple) else span

    min_nights = st.sidebar.slider("Minimum nights away", 0, 7, 2)
    budget = st.sidebar.slider("Max total getaway cost ($)", 50, 1500, 400, step=10)
    allow_mix = st.sidebar.checkbox("Allow mix-and-match airports", value=True)
    max_pair = st.sidebar.slider(
        "Max distance between arrival & departure airports (mi)", 0, 200, 100, step=10, disabled=not allow_mix
    )
    st.sidebar.subheader("📍 Where are you actually going?")
    place_query = st.sidebar.text_input(
        "Place (optional)", placeholder="Siesta Key, FL", key="place_query",
        help="Ranks packages by door-to-door time: fare plus the drive from each airport to this place.",
    ).strip()
    place = load_place(place_query) if place_query else None
    max_drive = st.sidebar.slider("Max drive from airport (min)", 30, 240, 150, step=15, disabled=place is None)
    drive_value = st.sidebar.slider("Value of your time ($/hour)", 0, 60, 15, step=5, disabled=place is None)
    drives: dict[str, tuple[float, str]] = {}
    if place_query and place is None:
        st.sidebar.warning("Couldn't find that place (offline or unknown). Try 'City, ST'.")
    elif place:
        drives = load_drives(place_query, tuple(destinations))
        reachable = [c for c in destinations if drives.get(c, (float("inf"), ""))[0] <= max_drive]
        st.sidebar.caption(f"📍 {place.name} · {len(reachable)} of {len(destinations)} airports within {max_drive} min")
        destinations = reachable

    modes = ["Cheapest package", "Best window fit", "Sunniest under budget"] + (["Door-to-door"] if place else [])
    sort_mode = st.sidebar.radio("Rank by", modes, horizontal=True)

    st.session_state.filters = {
        "destinations": destinations, "availability": availability, "min_nights": min_nights,
        "max_budget": float(budget), "allow_mix": allow_mix, "max_pair_miles": float(max_pair),
        "span_days": (span_end - date.today()).days, "holiday_extend": holiday_extend,
        "origins": list(config.HOME_CODES), "allow_open_jaw": False,
    }
    st.session_state.place_mode = {"place": place, "drives": drives, "value": float(drive_value)}
    windows = availability.windows(span_start, span_end, holiday_extend=holiday_extend)
    if partner is not None:
        windows = intersect_windows(windows, partner.windows(span_start, span_end, holiday_extend=holiday_extend))
    query = None
    if destinations and windows:
        query = GetawayQuery(
            origins=list(config.HOME_CODES),
            destinations=set(destinations),
            windows=windows,
            min_nights=min_nights,
            max_budget=float(budget),
            allow_mix=allow_mix,
            max_pair_miles=float(max_pair),
        )

    st.sidebar.divider()
    scrape_opts = scraper_panel(destinations, span_start, span_end)
    st.sidebar.divider()
    rule_panel()
    scheduler_panel()
    return query, destinations, (span_start, span_end), sort_mode, scrape_opts


def rule_panel() -> None:
    """Save the current sidebar filters as a rule the nightly job evaluates."""
    with st.sidebar.expander("★ Save filters as an alert rule"):
        label = st.text_input("Rule name", placeholder="Florida under $150", key="rule_label")
        f = st.session_state.get("filters", {})
        if st.button("Save rule", disabled=not (label.strip() and f.get("destinations")), key="save_rule"):
            payload = alerts.rule_payload(
                f["destinations"], f["availability"], f["min_nights"], f["max_budget"], f["allow_mix"],
                f["max_pair_miles"], max(f["span_days"], 7), holiday_extend=f.get("holiday_extend", False),
                origins=f.get("origins"), allow_open_jaw=f.get("allow_open_jaw", False),
            )
            database.add_watch("rule", label.strip(), payload)
            st.success(f"Saved rule '{label.strip()}'. You'll be alerted when new packages match.")
        st.caption(
            "Push alerts go to ntfy topic "
            + (f"`{config.NTFY_TOPIC}`" if config.NTFY_TOPIC else "**(not set — add AVELO_NTFY_TOPIC to .env)**")
            + "; every alert also appears in the Watchlist & alerts tab."
        )


def scheduler_panel() -> None:
    with st.sidebar.expander("⏰ Nightly scrape"):
        status = scheduler.status()
        c1, c2 = st.columns(2)
        hour = c1.number_input("Hour", 0, 23, int(status.get("hour") or config.SCHEDULE_HOUR))
        minute = c2.number_input("Minute", 0, 59, int(status.get("minute") or config.SCHEDULE_MINUTE), step=5)
        dests, days = scheduler.targets_from_rules()
        st.caption(f"Job will scrape {len(dests)} destination(s) for {days} days (from your saved rules).")
        b1, b2 = st.columns(2)
        if b1.button("Install / update", key="sched_install"):
            result = scheduler.install(scheduler.job_command(dests, days), int(hour), int(minute))
            if result.get("installed"):
                st.success(f"Scheduled daily at {int(hour):02d}:{int(minute):02d}")
            elif result.get("cron"):
                st.info("Not macOS — add this cron line:")
                st.code(result["cron"], language="bash")
            else:
                st.error(result.get("error") or "Install failed")
        if b2.button("Remove", key="sched_remove", disabled=not status.get("installed")):
            scheduler.remove()
            st.info("Nightly job removed")
        if status.get("installed"):
            st.caption(f"Installed: daily at {status['hour']:02d}:{status['minute']:02d} · loaded: {status.get('loaded')}")
        runs = database.last_runs(3)
        if runs:
            st.caption("Recent runs: " + " · ".join(
                f"{r['started_at'][5:16].replace('T', ' ')} {r['mode']} ({r['summary'].get('flights_written', '?')} fares)" for r in runs
            ))


def scraper_panel(destinations: list[str], start: date, end: date) -> dict:
    """Filter-before-scrape: the scraper only ever runs from this button, with
    exactly the destinations, date span and promo code chosen above. It runs as
    a background process so the dashboard stays responsive; `scrape_monitor`
    tails its log."""
    st.sidebar.subheader("Scraper")
    depth = st.sidebar.selectbox(
        "Depth",
        ["calendar", "full", "detail"],
        help="calendar = lowest member fare per day, one page per route (fast). "
        "full = calendar plus a per-day search that adds departure times (slow).",
    )
    source = st.sidebar.radio("Source", ["Live (Playwright)", "Mock data"], horizontal=True)
    promo = st.sidebar.text_input(
        "Active Promo Code (Optional)",
        max_chars=20,
        placeholder="e.g. SAVE20",
        help="Typed into Avelo's search form on every search. Fares scraped with a code are tagged with it.",
    ).strip().upper()
    st.session_state.promo_code_value = promo
    headful = st.sidebar.checkbox("Show browser window", value=False)
    routes = len(destinations) * 2 * len(config.HOME_CODES)
    days = (end - start).days + 1
    st.sidebar.caption(
        f"Target: {routes} routes × {days} days · {'/'.join(config.HOME_CODES)} ⇄ {', '.join(destinations) or '—'}"
        + (f" · promo {promo}" if promo else "")
    )

    running = st.session_state.get("scrape_proc") is not None
    pending = st.session_state.pop("pending_scrape", None) if not running else None
    if pending:
        destinations, start, end = pending["destinations"], pending["start"], pending["end"]
    if pending or st.sidebar.button(
        "🎯 Execute Target Scrape", type="primary", width="stretch", disabled=not destinations or running
    ):
        busy = runlock.running_pid()
        if busy:  # the nightly job or a CLI scrape: don't spawn one that would exit 3
            st.session_state.scrape_result = {
                "rc": runlock.BUSY_EXIT, "pid": busy, "tail": f"another scrape is already running (pid {busy}); not starting\n",
            }
            st.rerun()
        database.maintenance()
        cmd = build_command(
            destinations, start, end, mode=depth, mock=source.startswith("Mock"), headful=headful, promo=promo or None,
            origins=list(config.HOME_CODES),
        )
        config.SCRAPE_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(config.SCRAPE_LOG, "w")
        log_file.write("$ " + " ".join(cmd[1:]) + "\n\n")
        log_file.flush()
        st.session_state.scrape_proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=config.PROJECT_ROOT
        )
        st.session_state.scrape_started = _time.time()
        st.session_state.pop("scrape_result", None)
        st.rerun()
    return {"depth": depth, "headful": headful, "promo": promo}


def _log_tail(chars: int = 3500) -> str:
    try:
        return config.SCRAPE_LOG.read_text()[-chars:]
    except OSError:
        return ""


def scrape_monitor() -> None:
    """Auto-refreshing status for a running scrape; clears caches when it ends."""
    proc = st.session_state.get("scrape_proc")
    if proc is None:
        result = st.session_state.get("scrape_result")
        if result:
            if result["rc"] == 0:
                st.sidebar.success("Scrape finished")
            elif result["rc"] == runlock.BUSY_EXIT:
                pid = f" (pid {result['pid']})" if result.get("pid") else ""
                st.sidebar.info(f"Another scrape is already running{pid}; try again when it finishes")
            else:
                st.sidebar.error("Scrape reported errors")
            with st.sidebar.expander("Scrape log"):
                st.code(result["tail"], language="text")
        return

    elapsed = _time.time() - st.session_state.get("scrape_started", _time.time())
    if proc.poll() is None and elapsed > config.SCRAPE_TIMEOUT_MIN * 60:
        proc.kill()
        proc.wait(timeout=10)
    if proc.poll() is None:
        st.sidebar.info(f"Scraping… {int(elapsed // 60)}m {int(elapsed % 60):02d}s")
        if st.sidebar.button("Cancel scrape", width="stretch"):
            proc.kill()
            proc.wait(timeout=10)
        with st.sidebar.expander("Live log", expanded=True):
            st.code(_log_tail(1500), language="text")
        return

    rc = proc.returncode
    st.session_state.scrape_result = {
        "rc": rc, "tail": _log_tail(), "pid": runlock.running_pid() if rc == runlock.BUSY_EXIT else None,
    }
    st.session_state.scrape_proc = None
    st.cache_data.clear()
    _search_cache().clear()
    st.rerun()


# --------------------------------------------------------------------------- #
# System map
# --------------------------------------------------------------------------- #


def system_map(selected: list[str]) -> None:
    hub = config.HOME_AIRPORTS[config.PRIMARY_HOME]
    chosen = set(selected)
    points = [
        {
            "code": a.code,
            "city": a.city,
            "region": a.region,
            "lat": a.lat,
            "lon": a.lon,
            "color": list(config.REGION_COLORS[a.region] if a.code in chosen else config.INACTIVE_COLOR),
            "radius": 26000 if a.code in chosen else 14000,
        }
        for a in config.DESTINATIONS.values()
    ]
    points.append(
        {"code": hub.code, "city": hub.city, "region": "Hub", "lat": hub.lat, "lon": hub.lon,
         "color": list(config.HUB_COLOR), "radius": 34000}
    )
    arcs = [
        {
            "code": a.code,
            "city": a.city,
            "region": a.region,
            "source": [hub.lon, hub.lat],
            "target": [a.lon, a.lat],
            "color": list(config.REGION_COLORS[a.region]),
        }
        for a in config.DESTINATIONS.values()
        if a.code in chosen
    ]
    layers = [
        pdk.Layer(
            "ArcLayer",
            data=arcs,
            get_source_position="source",
            get_target_position="target",
            get_source_color=list(config.HUB_COLOR),
            get_target_color="color",
            get_width=3,
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=points,
            get_position="[lon, lat]",
            get_fill_color="color",
            get_radius="radius",
            radius_min_pixels=4,
            stroked=True,
            get_line_color=[255, 255, 255],
            line_width_min_pixels=1,
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            data=points,
            get_position="[lon, lat]",
            get_text="code",
            get_size=12,
            get_color=[40, 40, 40],
            get_pixel_offset=[0, -14],
            get_alignment_baseline="'bottom'",
        ),
    ]
    view = pdk.ViewState(latitude=30.5, longitude=-77.0, zoom=3.9, pitch=0)
    st.pydeck_chart(
        pdk.Deck(layers=layers, initial_view_state=view, tooltip={"text": "{code} · {city}\n{region}"}),
        width="stretch",
        height=380,
    )
    legend = " &nbsp; ".join(
        f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:rgb{config.REGION_COLORS[r]};margin-right:4px"></span>{r}'
        for r in config.REGIONS
    )
    legend += f' &nbsp; <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:rgb{config.HUB_COLOR};margin-right:4px"></span>{config.PRIMARY_HOME} hub'
    legend += f' &nbsp; <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:rgb{config.INACTIVE_COLOR};margin-right:4px"></span>not selected'
    st.markdown(f'<div style="font-size:0.85rem">{legend}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Main panel pieces
# --------------------------------------------------------------------------- #


def member_badge() -> None:
    roi = bookings.plus_roi()
    tone = "0,158,115" if roi["paid_off"] else "230,159,0"
    roi_text = (
        f"Plus ROI: ${roi['total_value']:,.0f} of ${roi['annual_cost']:,.0f} membership"
        f" ({roi['ratio']:.1f}×, {roi['bookings']} booked trip{'s' if roi['bookings'] != 1 else ''})"
        if roi["annual_cost"] else f"Plus value: ${roi['total_value']:,.0f}"
    )
    st.markdown(
        f'<div style="display:inline-block;padding:6px 12px;border-radius:999px;'
        f'background:rgba(0,158,115,0.12);border:1px solid rgba(0,158,115,0.5);font-size:0.9rem;margin-bottom:8px">'
        f'✅ <strong>{config.MEMBER_BADGE}</strong> · add-on fees locked at ${BENEFITS.total:.2f}</div>'
        f' <div style="display:inline-block;padding:6px 12px;border-radius:999px;'
        f'background:rgba({tone},0.12);border:1px solid rgba({tone},0.5);font-size:0.9rem;margin-bottom:8px">'
        f'{"🏆" if roi["paid_off"] else "📈"} {roi_text}</div>',
        unsafe_allow_html=True,
    )


def freshness_panel(destinations: list[str], version: float) -> None:
    df = load_freshness(tuple(destinations), version)
    with st.expander("Last scraped per route", expanded=False):
        if df.empty:
            st.write("No routes scraped yet for this selection.")
            return
        df = df.assign(
            route=df.origin + "→" + df.destination,
            last_scraped=pd.to_datetime(df.last_scraped),
        )
        now = pd.Timestamp.now()
        df["age"] = (now - df.last_scraped).apply(_humanize)
        show = df[["route", "last_scraped", "age", "fares", "first_day", "last_day"]].rename(
            columns={"route": "Route", "last_scraped": "Last scraped", "age": "Age", "fares": "Fares",
                     "first_day": "First day", "last_day": "Last day"}
        )
        st.dataframe(show, hide_index=True, width="stretch",
                     column_config={"Last scraped": st.column_config.DatetimeColumn(format="MMM D, HH:mm")})


def _humanize(delta: pd.Timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{max(seconds // 60, 0)} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def package_inspector(ranked: list[Itinerary], version: float) -> None:
    """Price trend across scraper runs and weather for one package."""
    top = ranked[:25]
    if not top:
        return
    labels = [
        f"#{i} {it.destination_city} · {it.route_mix} · {it.window.label} · ${it.total_price:,.0f}"
        for i, it in enumerate(top, start=1)
    ]
    choice = st.selectbox("Inspect a package", labels, index=0)
    it = top[labels.index(choice)]
    b1, b2, b3 = st.columns([1, 1, 2])
    if b1.button("★ Watch this package", help="Alert me when its total drops or a leg disappears"):
        watch_id, created = alerts.watch_package(it)
        if created:
            st.success(f"Watching {alerts.package_label(it)} at ${it.total_price:,.0f}")
        else:
            st.info("You're already watching this package.")
    if b2.button("✈️ Open on Avelo", help="Opens a visible browser with each leg's search filled in and stops at the results. Never books."):
        legs = [(it.outbound.origin, it.outbound.destination, it.outbound.departure_date),
                (it.inbound.origin, it.inbound.destination, it.inbound.departure_date)]
        promo = st.session_state.get("promo_code_value") or None
        subprocess.Popen(handoff_command(legs, promo), cwd=config.PROJECT_ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st.info("A browser window is opening with both legs searched. Pick your fares there; nothing is booked automatically.")
    with b3.popover("✅ Mark as booked"):
        paid = st.number_input("Total you paid ($)", min_value=0.0, value=float(it.total_price), step=1.0, key="paid_input")
        notes = st.text_input("Notes", placeholder="confirmation code…", key="booking_notes")
        if st.button("Save booking", key="save_booking"):
            database.add_booking(alerts.package_label(it), bookings.booking_payload(it), paid, notes or None)
            st.success("Booked trip saved — I'll keep watching its fare and count it toward your Plus ROI.")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown(f"**Price trend** · last scraped {it.last_scraped:%b %d %H:%M} · {it.trend}")
        out_h = load_history(it.outbound.flight_id, it.outbound.departure_datetime.isoformat(timespec="minutes"), version)
        in_h = load_history(it.inbound.flight_id, it.inbound.departure_datetime.isoformat(timespec="minutes"), version)
        if out_h.empty or in_h.empty:
            st.info("No history yet for this package.")
        else:
            series = pd.concat(
                [
                    out_h.set_index("scraped_at")["price"].rename(f"Out {it.outbound.route}"),
                    in_h.set_index("scraped_at")["price"].rename(f"Back {it.inbound.route}"),
                ],
                axis=1,
            ).sort_index().ffill()
            series["Total Getaway Cost"] = series.sum(axis=1) + it.addon_fees
            if len(series) < 2:
                st.caption("One observation so far — run the scraper again to see movement.")
            st.line_chart(series, height=220)
    with c2:
        st.markdown(f"**Weather in {it.destination_city}**")
        start, end = it.outbound.departure_date, it.inbound.departure_date
        rows = load_weather(it.destination, start, end)
        if rows is None:
            st.caption("Forecast unavailable (offline or service down).")
        elif not rows:
            st.caption(f"Trip is beyond the 16-day forecast horizon (starts {start:%b %d}).")
        else:
            wdf = pd.DataFrame(rows)
            wdf["day"] = pd.to_datetime(wdf["day"]).dt.strftime("%a %b %d")
            st.dataframe(
                wdf.rename(columns={"day": "Day", "high_f": "High °F", "low_f": "Low °F", "rain_pct": "Rain %", "sky": "Sky"}),
                hide_index=True, width="stretch", height=min(60 + 35 * len(wdf), 260),
            )


def wildcard_panel(itins: list[Itinerary]) -> None:
    c1, c2 = st.columns([1, 5])
    if c1.button("🎲 Wildcard weekend", help="A random package under your budget, weighted towards cheap ones"):
        pick = wildcard(itins)
        st.session_state.wildcard = (pick.outbound.flight_id, pick.outbound.departure_datetime, pick.inbound.flight_id,
                                     pick.inbound.departure_datetime, pick.window.start) if pick else None
    key = st.session_state.get("wildcard")
    if key:
        match = next(
            (it for it in itins if (it.outbound.flight_id, it.outbound.departure_datetime, it.inbound.flight_id,
                                    it.inbound.departure_datetime, it.window.start) == key),
            None,
        )
        if match:
            c2.success(
                f"**{match.destination_city}** · {match.route_mix} · {match.window.label} · "
                f"out {match.outbound.departure_date:%a %b %d}, back {match.inbound.departure_date:%a %b %d} · "
                f"**${match.total_price:,.0f}** all-in · {match.trend}"
            )


def alerts_tab() -> None:
    c1, c2 = st.columns([4, 1])
    c1.subheader("Alerts")
    if c2.button("Mark all read", disabled=database.unread_event_count() == 0):
        database.mark_events_read()
        st.rerun()
    feed = database.list_events(limit=60, notify_only=True)
    if not feed:
        st.info("No alerts yet. Watch a package, save a rule, then run the scraper (or wait for the nightly job).")
    for e in feed:
        marker = "🔵 " if not e["read"] else ""
        st.write(f"{marker}**{e['created_at'][:16].replace('T', ' ')}** · {e['message']}")

    st.subheader("Signals from the latest run")
    runs = database.last_runs(1)
    latest = database.list_events(limit=40, run_id=runs[0]["run_id"]) if runs else []
    latest = [e for e in latest if e["kind"] in SIGNAL_ICONS]
    if latest:
        st.dataframe(
            pd.DataFrame([{"Kind": SIGNAL_ICONS[e["kind"]], "Route": f"{e['origin']}→{e['destination']}", "Day": e["day"], "Detail": e["message"]} for e in latest]),
            hide_index=True, width="stretch",
        )
    else:
        st.caption("No fare signals yet — signals compare consecutive runs of the same route.")

    st.subheader("My booked trips")
    booked = database.list_bookings()
    if not booked:
        st.caption("No bookings recorded. Use **Mark as booked** in Trend & weather after you buy.")
    for b in booked:
        c1, c2 = st.columns([6, 1])
        st_ = b["state"]
        now = st_.get("current_total")
        verdict = ""
        if now is not None:
            d = now - float(b["price_paid"])
            verdict = f" · now ${now:,.0f} → {'you beat the market by' if d >= 0 else 'it dropped'} ${abs(d):,.0f}"
        c1.write(f"**{b['label']}** — paid ${b['price_paid']:,.0f} on {b['booked_at'][:10]}{verdict}"
                 + (f" · saved ${bookings.fare_savings(b['payload']):,.0f} with Plus fares" if bookings.fare_savings(b['payload']) else "")
                 + (f" · _{b['notes']}_" if b.get("notes") else ""))
        if c2.button("Remove", key=f"rmb_{b['booking_id']}"):
            database.remove_booking(b["booking_id"])
            st.rerun()

    st.subheader("Watchlist")
    watches = database.list_watches()
    if not watches:
        st.caption("Nothing watched yet.")
    for w in watches:
        c1, c2 = st.columns([6, 1])
        if w["kind"] == "rule":
            p = w["payload"]
            c1.write(
                f"**Rule · {w['label']}** — {', '.join(p['destinations'])} · "
                f"{WEEKDAYS[p['start_weekday']][:3]} {p['start_time']} → {WEEKDAYS[p['end_weekday']][:3]} {p['end_time']} · "
                f"≤ ${p['max_budget']:.0f} · {w['state'].get('match_count', '?')} matches"
            )
        else:
            st_ = w["state"]
            c1.write(f"**Package · {w['label']}** — last ${st_.get('last_total', 0):,.0f}"
                     + (" · ⚠ no longer available" if st_.get("unavailable") else ""))
        if c2.button("Remove", key=f"rm_{w['watch_id']}"):
            database.remove_watch(w["watch_id"])
            st.rerun()


def season_tab(itins: list[Itinerary], windows: int) -> None:
    st.caption(f"One getaway per free window ({windows} windows in your span), never the same destination twice unless you allow it.")
    c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
    budget = c1.slider("Season budget ($)", 100, 3000, 600, step=50)
    max_trips = c2.number_input("Max trips", 1, max(windows, 1), max(windows, 1))
    no_repeat = c3.checkbox("No repeat cities", value=True)
    objective = c4.radio("Optimise for", ["price", "fit", "sun"], horizontal=True, format_func=lambda o: {"price": "Cheapest", "fit": "Best fit", "sun": "Sunniest"}[o])
    plan = plan_season(itins, float(budget), int(max_trips), no_repeat, objective)
    if not plan.trips:
        st.info("Nothing fits that budget. Raise it or relax the rules.")
        return
    m1, m2, m3 = st.columns(3)
    m1.metric("Trips", len(plan.trips))
    m2.metric("Season cost", f"${plan.total_cost:,.0f}")
    m3.metric("Budget left", f"${plan.remaining:,.0f}")
    rows = [
        {"Window": t.window.label, "Destination": t.destination_city, "Route Mix": t.route_mix,
         "Out": t.outbound.departure_date.strftime("%a %b %d"), "Back": t.inbound.departure_date.strftime("%a %b %d"),
         "Cost": t.total_price, "Fit": t.fit, "Sun": t.sun_score}
        for t in plan.trips
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Cost": st.column_config.NumberColumn(format="$%d"),
                                "Fit": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0f%%"),
                                "Sun": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    if st.button("★ Watch all planned trips"):
        created = sum(alerts.watch_package(t)[1] for t in plan.trips)
        st.success(f"Watching {created} new trip(s); {len(plan.trips) - created} already watched.")
    itinerary_timeline(plan.trips, n=len(plan.trips), key="season_timeline")


def coverage_line(stats: dict, destinations: list[str], span: tuple[date, date]) -> None:
    if not destinations:
        st.info("Pick at least one destination in the sidebar.")
        return
    if not stats.get("flights"):
        st.warning(
            f"No fares stored yet for {', '.join(destinations)} between {span[0]:%b %d} and {span[1]:%b %d}. "
            "Use **Execute Target Scrape** in the sidebar (try *Mock data* first)."
        )
        return
    last = (stats.get("last_scraped") or "")[:16].replace("T", " ")
    st.caption(
        f"{stats['flights']:,} fares on {stats['routes']} routes for this selection · "
        f"{stats['member_fares']:,} are Avelo PLUS member fares · last scraped {last}"
    )


def metrics(itins: list[Itinerary], windows: int) -> None:
    c1, c2, c3, c4 = st.columns(4)
    if not itins:
        c1.metric("Cheapest getaway", "—")
        c2.metric("Best window fit", "—")
        c3.metric("Valid packages", "0")
        c4.metric("Free windows in span", str(windows))
        return
    cheapest = rank_by_price(itins)[0]
    best_fit = rank_by_schedule(itins)[0]
    c1.metric(
        "Cheapest getaway",
        f"${cheapest.total_price:,.0f}",
        f"{cheapest.destination_city} · {cheapest.route_mix} · {cheapest.window.label}",
        delta_color="off",
    )
    c2.metric(
        "Best window fit",
        f"{best_fit.fit:.0%} · ${best_fit.total_price:,.0f}",
        f"{best_fit.destination_city} · {best_fit.route_mix} · {best_fit.window.label}",
        delta_color="off",
    )
    c3.metric("Valid packages", f"{len(itins):,}")
    c4.metric("Free windows in span", str(windows))


def results_table(ranked: list[Itinerary], version: float = 0.0) -> None:
    df = to_dataframe(ranked)
    sig = load_signal_map(version)
    df.insert(7, "Signals", [signal_text(it, sig) for it in ranked])
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        height=min(60 + 35 * len(df), 560),
        column_config={
            "Out Price": st.column_config.NumberColumn(format="$%d"),
            "Back Price": st.column_config.NumberColumn(format="$%d"),
            "Total Getaway Cost": st.column_config.NumberColumn(format="$%d"),
            "Savings vs Public": st.column_config.NumberColumn(format="$%d"),
            "Add-on Fees": st.column_config.NumberColumn(format="$%.2f"),
            "Deal vs Typical": st.column_config.NumberColumn(format="%+.0f%%", help="Positive = cheaper than the median fare on these routes"),
            "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=1, format="%.0f%%"),
            "Sun Score": st.column_config.ProgressColumn("Sun", min_value=0, max_value=1, format="%.2f", help="Warm and dry over the trip days; blank beyond the 16-day forecast"),
            "Δ°F vs home": st.column_config.NumberColumn(format="%+.0f°F"),
            "Drive (min)": st.column_config.NumberColumn(format="%.0f"),
            "Door-to-door": st.column_config.NumberColumn(
                format="$%d",
                help="Fares, plus your drive at the destination end and the Home-side $ (home-airport parking and "
                     "drive), with drive time valued at your $/hour"),
            "Home-side $": st.column_config.NumberColumn(
                format="$%d",
                help="Parking at the home airport you fly from (per night, at least one day) plus your drive from "
                     "home to that airport and back at your $/hour. Default parking: ILG $0, PHL $17, BWI $14 a day; "
                     "the home drive counts only once set. There is no dashboard control for these yet."),
            "Member Fare": st.column_config.CheckboxColumn(disabled=True),
            "Times Verified": st.column_config.CheckboxColumn(disabled=True),
        },
        column_order=[
            "Rank", "Window", "Destination", "Home", "Route Mix", "Total Getaway Cost", "Deal vs Typical", "Trend", "Signals",
            "Out Route", "Out Date", "Out Time", "Out Price",
            "Back Route", "Back Date", "Back Time", "Back Price",
            "Nights", "Fit", "Sun Score", "Δ°F vs home", "Drive (min)", "Door-to-door", "Home-side $",
            "Member Fare", "Savings vs Public", "Add-on Fees", "Times Verified",
            "Airport Gap (mi)", "Last Scraped",
        ],
    )
    if not all(it.time_verified for it in ranked):
        st.caption("Rows without **Times Verified** come from calendar scrapes (day known, time not). "
                   "Run the scraper with depth *full* to confirm they fit your window's hours.")
    st.download_button("Download CSV", df.to_csv(index=False).encode(), "avelo_getaways.csv", "text/csv")


def calendar_heatmap(cal: pd.DataFrame, destinations: list[str], span: tuple[date, date]) -> None:
    """Route × day grid of the lowest fare. Empty cell = no flight that day."""
    if cal.empty:
        st.info("No fares in this span to draw a calendar for.")
        return
    out = cal[cal.origin.isin(config.HOME_CODES) & cal.destination.isin(destinations)]
    back = cal[cal.destination.isin(config.HOME_CODES) & cal.origin.isin(destinations)]
    sub = pd.concat([out, back])
    if sub.empty:
        st.info("No fares for the selected destinations in this span.")
        return
    sub = sub.assign(route=sub.origin + "→" + sub.destination, day=pd.to_datetime(sub.day))
    routes = list(dict.fromkeys(sorted(out.origin + "→" + out.destination) + sorted(back.origin + "→" + back.destination)))
    days = pd.date_range(span[0], span[1])
    grid = sub.pivot_table(index="route", columns="day", values="min_price", aggfunc="min").reindex(index=routes, columns=days)
    counts = sub.pivot_table(index="route", columns="day", values="flights", aggfunc="sum").reindex(index=routes, columns=days)
    text = grid.map(lambda v: "" if pd.isna(v) else f"${v:.0f}")
    fig = go.Figure(
        go.Heatmap(
            z=grid.values,
            x=[d.strftime("%a %b %d") for d in days],
            y=routes,
            text=text.values,
            texttemplate="%{text}" if len(days) <= 45 else "",
            textfont={"size": 10},
            customdata=counts.fillna(0).values,
            colorscale=PRICE_SCALE,
            colorbar={"title": "Lowest fare", "tickprefix": "$", "thickness": 12},
            hovertemplate="<b>%{y}</b><br>%{x}<br>from $%{z:.0f} · %{customdata:.0f} flight(s)<extra></extra>",
            xgap=2,
            ygap=2,
            hoverongaps=False,
        )
    )
    fig.update_layout(
        height=120 + 30 * len(routes),
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        yaxis={"autorange": "reversed", "showgrid": False},
        xaxis={"showgrid": False, "tickangle": -45},
    )
    st.plotly_chart(fig, width="stretch", key="fare_calendar")


def itinerary_timeline(ranked: list[Itinerary], n: int = 12, key: str = "timeline") -> None:
    top = ranked[:n]
    if not top:
        return
    df = pd.DataFrame(
        {
            "Trip": f"#{i} {it.destination_city} · {it.route_mix}",
            "Start": it.outbound.departure_datetime,
            "End": it.inbound.effective_arrival if it.inbound.time_known else it.inbound.departure_datetime + timedelta(hours=23),
            "Total": it.total_price,
            "Fit": it.fit,
        }
        for i, it in enumerate(top, start=1)
    )
    fig = px.timeline(
        df, x_start="Start", x_end="End", y="Trip", text=df.Total.map("${:,.0f}".format),
        hover_data={"Total": ":$,.0f", "Fit": ":.0%", "Start": "|%a %b %d %H:%M", "End": "|%a %b %d %H:%M"},
        color_discrete_sequence=[ACCENT],
    )
    fig.update_traces(marker_line_width=0, textposition="inside", insidetextanchor="middle")
    fig.update_yaxes(autorange="reversed", title=None)
    fig.update_xaxes(title=None, showgrid=True, gridcolor="rgba(128,128,128,0.15)")
    fig.update_layout(height=80 + 28 * len(top), margin={"l": 10, "r": 10, "t": 10, "b": 10}, bargap=0.35)
    st.plotly_chart(fig, width="stretch", key=key)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def main() -> None:
    database.init_db()
    if not st.session_state.get("maintained") and not runlock.running_pid():
        database.maintenance()  # drop departed flights, prune old history (a running scrape does its own)
        st.session_state.maintained = True
    query, destinations, span, sort_mode, _ = sidebar()

    running = st.session_state.get("scrape_proc") is not None

    @st.fragment(run_every=2 if running else None)
    def monitor() -> None:
        scrape_monitor()

    monitor()
    version = _db_version()

    unread = database.unread_event_count()
    st.title("Flexible Getaways from " + " / ".join(config.HOME_CODES) + (f"  🔔 {unread}" if unread else ""))
    member_badge()
    system_map(destinations)

    coverage = load_selection_stats(tuple(destinations), span[0], span[1], version)
    coverage_line(coverage, destinations, span)
    if destinations:
        freshness_panel(destinations, version)
    if query is None:
        if destinations:
            st.info("Your free window doesn't fit inside the search span. Widen the span or shorten the window.")
        return

    itins = run_search(query)
    horizon = date.today() + timedelta(days=16)
    if any(it.outbound.departure_date <= horizon for it in itins):
        codes = {it.destination for it in itins if it.outbound.departure_date <= horizon}
        enrich_weather(itins, {c: load_forecast_all(c) for c in codes}, load_forecast_all(config.PRIMARY_HOME))
    pm = st.session_state.get("place_mode") or {}
    if pm.get("place"):
        enrich_drives(itins, {c: m for c, (m, _src) in pm["drives"].items()}, pm["value"])
    else:
        clear_drives(itins)  # run_search reuses cached objects; drop a cleared place's drives and rate
    enrich_home(itins, database.home_costs())
    ranked = {
        "Cheapest package": rank_by_price,
        "Best window fit": rank_by_schedule,
        "Sunniest under budget": rank_by_sun,
        "Door-to-door": rank_by_door_to_door,
    }[sort_mode](itins)
    metrics(itins, len(query.windows))
    if pm.get("place"):
        with st.expander(f"Drive times from airports to {pm['place'].name}"):
            rows = [{"Airport": f"{c} · {config.airport_name(c)}", "Minutes": m, "Source": src} for c, (m, src) in sorted(pm["drives"].items(), key=lambda kv: kv[1][0])]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if itins:
        wildcard_panel(itins)

    tab_table, tab_inspect, tab_season, tab_alerts, tab_calendar, tab_timeline = st.tabs(
        ["Getaway packages", "Trend & weather", "Season plan", "Watchlist & alerts", "Fare calendar", "Trip timeline"]
    )
    with tab_season:
        if itins:
            season_tab(itins, len(query.windows))
        else:
            st.info("Nothing to plan yet.")
    with tab_alerts:
        alerts_tab()
    with tab_table:
        if ranked:
            results_table(ranked, version)
        else:
            st.info("No valid packages. Widen the span, lower the minimum nights, raise the budget, or scrape more routes.")
    with tab_inspect:
        if ranked:
            package_inspector(ranked, version)
        else:
            st.info("Nothing to inspect yet.")
    with tab_calendar:
        calendar_heatmap(load_calendar(span[0], span[1], version), destinations, span)
    with tab_timeline:
        itinerary_timeline(ranked, key="ranked_timeline")


main()  # Streamlit executes the module top-level on every rerun
