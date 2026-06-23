"""
VATSIM APAC Monitoring Bot
==========================
Runs a 30-minute cycle that:
  1. Fetches VATSIM data once per cycle
  2. Posts an OPS summary to Discord
  3. Posts a Command Centre summary to Discord
  4. Fetches METAR data once per cycle and posts weather alerts if any exist

Scheduling: APScheduler BlockingScheduler, single job, 30-minute interval.
No Flask — this is a pure background worker.
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from collections import defaultdict

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
AVWX_API_KEY = os.environ.get("AVWX_API_KEY", "")

if not DISCORD_WEBHOOK_URL:
    log.warning("DISCORD_WEBHOOK_URL is not set — Discord posts will be skipped.")

# ---------------------------------------------------------------------------
# Authoritative airport dataset
# ---------------------------------------------------------------------------
AIRPORTS = {
    # Australia
    "YMML": {"name": "Melbourne",       "country": "Australia"},
    "YSSY": {"name": "Sydney",          "country": "Australia"},
    "YBBN": {"name": "Brisbane",        "country": "Australia"},
    "YSCB": {"name": "Canberra",        "country": "Australia"},
    "YPPH": {"name": "Perth",           "country": "Australia"},
    "YPDN": {"name": "Darwin",          "country": "Australia"},
    "YPAD": {"name": "Adelaide",        "country": "Australia"},
    # New Zealand
    "NZAA": {"name": "Auckland",        "country": "New Zealand"},
    "NZWN": {"name": "Wellington",      "country": "New Zealand"},
    "NZCH": {"name": "Christchurch",    "country": "New Zealand"},
    # South Pacific
    "NFFN": {"name": "Nadi",            "country": "South Pacific"},
    # Indonesia
    "WADD": {"name": "Bali/Denpasar",   "country": "Indonesia"},
}

ICAO_LIST = list(AIRPORTS.keys())

# ---------------------------------------------------------------------------
# VATSIM
# ---------------------------------------------------------------------------
VATSIM_DATA_URL = "https://data.vatsim.net/v3/vatsim-data.json"

# ATC suffix groups used for Command Centre
ATC_SUFFIXES = ["_DEL", "_GND", "_TWR", "_APP", "_DEP", "_CTR", "_FSS"]

# ---------------------------------------------------------------------------
# Helpers — Discord
# ---------------------------------------------------------------------------

def post_to_discord(content: str) -> bool:
    """Post a plain-text message to the configured Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("Skipping Discord post — DISCORD_WEBHOOK_URL not set.")
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        log.error("Discord returned HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        log.error("Discord post failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Helpers — VATSIM data
# ---------------------------------------------------------------------------

def fetch_vatsim_data() -> dict | None:
    """Fetch the VATSIM v3 data feed. Returns parsed JSON or None on error."""
    try:
        resp = requests.get(VATSIM_DATA_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("Failed to fetch VATSIM data: %s", exc)
        return None


def get_dtg() -> str:
    """Return current UTC date-time group string: DDHHMMZ MON YYYY"""
    now = datetime.now(timezone.utc)
    return now.strftime("%d%H%MZ %b %Y").upper()


# ---------------------------------------------------------------------------
# OPS Summary
# ---------------------------------------------------------------------------

def build_ops_summary(vatsim_data: dict) -> str:
    """
    Build the OPS summary message from a VATSIM data snapshot.
    Format is preserved exactly as the original specification.
    """
    dtg = get_dtg()
    pilots = vatsim_data.get("pilots", [])
    controllers = vatsim_data.get("controllers", [])

    # Map callsign prefix → airport ICAO
    # e.g. "YMML_TWR" → "YMML"
    def icao_from_callsign(cs: str) -> str | None:
        for icao in ICAO_LIST:
            if cs.upper().startswith(icao):
                return icao
        return None

    # Pilots on ground or in flight at/near monitored airports
    # We use departure + arrival fields
    airport_pilots: dict[str, list] = defaultdict(list)
    for p in pilots:
        dep = (p.get("flight_plan") or {}).get("departure", "")
        arr = (p.get("flight_plan") or {}).get("arrival", "")
        for icao in ICAO_LIST:
            if dep == icao or arr == icao:
                airport_pilots[icao].append(p.get("callsign", ""))
                break  # count each pilot once

    # ATC online at monitored airports
    airport_atc: dict[str, list] = defaultdict(list)
    for c in controllers:
        icao = icao_from_callsign(c.get("callsign", ""))
        if icao:
            airport_atc[icao].append(c.get("callsign", ""))

    # Build HOT ZONES (airports with traffic)
    hot_zones = []
    for icao in ICAO_LIST:
        pax = airport_pilots.get(icao, [])
        atc = airport_atc.get(icao, [])
        if pax or atc:
            hot_zones.append((icao, pax, atc))

    total_pilots = len(pilots)
    total_atc = len(controllers)

    lines = []
    lines.append("```")
    lines.append("╔══════════════════════════════════════════╗")
    lines.append("║         VATSIM APAC OPS SUMMARY          ║")
    lines.append("╚══════════════════════════════════════════╝")
    lines.append(f"  DTG     : {dtg}")
    lines.append(f"  PILOTS  : {total_pilots} online globally")
    lines.append(f"  ATC     : {total_atc} online globally")
    lines.append("")

    if hot_zones:
        lines.append("  ── HOT ZONES ──────────────────────────────")
        for icao, pax, atc in hot_zones:
            info = AIRPORTS[icao]
            lines.append(f"  {icao}  {info['name']} ({info['country']})")
            lines.append(f"    TRAFFIC : {len(pax)} aircraft")
            if atc:
                lines.append(f"    ATC     : {', '.join(atc)}")
            else:
                lines.append("    ATC     : UNCONTROLLED")
    else:
        lines.append("  No monitored airports currently active.")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command Centre Summary
# ---------------------------------------------------------------------------

def build_command_centre(vatsim_data: dict) -> str:
    """
    Build the Command Centre summary from a VATSIM data snapshot.
    Shows ATC coverage breakdown by suffix across all monitored airports.
    """
    dtg = get_dtg()
    controllers = vatsim_data.get("controllers", [])

    def icao_from_callsign(cs: str) -> str | None:
        for icao in ICAO_LIST:
            if cs.upper().startswith(icao):
                return icao
        return None

    # Group controllers by airport and suffix
    coverage: dict[str, dict[str, list]] = {icao: defaultdict(list) for icao in ICAO_LIST}
    unmatched = []
    for c in controllers:
        cs = c.get("callsign", "")
        icao = icao_from_callsign(cs)
        if not icao:
            continue
        suffix = "OTHER"
        for sfx in ATC_SUFFIXES:
            if cs.upper().endswith(sfx):
                suffix = sfx.lstrip("_")
                break
        coverage[icao][suffix].append(cs)

    # Only include airports with at least one controller
    active = [(icao, coverage[icao]) for icao in ICAO_LIST if any(coverage[icao].values())]

    lines = []
    lines.append("```")
    lines.append("╔══════════════════════════════════════════╗")
    lines.append("║       VATSIM APAC COMMAND CENTRE         ║")
    lines.append("╚══════════════════════════════════════════╝")
    lines.append(f"  DTG : {dtg}")
    lines.append("")

    if active:
        lines.append("  ── ATC COVERAGE ───────────────────────────")
        for icao, suf_map in active:
            info = AIRPORTS[icao]
            lines.append(f"  {icao}  {info['name']}")
            for sfx in ATC_SUFFIXES:
                key = sfx.lstrip("_")
                if suf_map.get(key):
                    lines.append(f"    {key:<4} : {', '.join(suf_map[key])}")
            if suf_map.get("OTHER"):
                lines.append(f"    OTHER: {', '.join(suf_map['OTHER'])}")
            lines.append("")
    else:
        lines.append("  No ATC coverage at monitored airports.")
        lines.append("")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weather Alert System
# ---------------------------------------------------------------------------

AVWX_BASE = "https://avwx.rest/api/metar/{icao}"

# Weather code → alert text
WEATHER_CODE_ALERTS = {
    "TS":   "THUNDERSTORM ACTIVITY",
    "TSRA": "THUNDERSTORM ACTIVITY",
    "+RA":  "HEAVY RAIN",
    "FG":   "LOW VISIBILITY (FOG)",
    "BR":   "MIST / REDUCED VISIBILITY",
    "CB":   "CUMULONIMBUS DETECTED",
}


def fetch_metar(icao: str) -> dict | None:
    """Fetch METAR JSON from AVWX for a single ICAO. Returns parsed JSON or None."""
    url = AVWX_BASE.format(icao=icao)
    headers = {}
    if AVWX_API_KEY:
        headers["Authorization"] = f"TOKEN {AVWX_API_KEY}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        log.debug("AVWX returned HTTP %s for %s", resp.status_code, icao)
        return None
    except Exception as exc:
        log.debug("METAR fetch failed for %s: %s", icao, exc)
        return None


def parse_wind(metar_json: dict) -> tuple[int, int]:
    """
    Extract wind speed and gust from AVWX METAR JSON.
    Returns (speed_kt, gust_kt). Gust is 0 if not present.
    """
    wind = metar_json.get("wind_speed") or {}
    gust = metar_json.get("wind_gust") or {}
    speed = int(wind.get("value") or 0)
    gust_val = int(gust.get("value") or 0)
    return speed, gust_val


def parse_wx_codes(metar_json: dict) -> list[str]:
    """
    Extract weather condition codes from AVWX METAR JSON.
    Returns a list of raw code strings (e.g. ['TS', '+RA']).
    """
    codes = []
    for entry in metar_json.get("wx_codes", []) or []:
        code = entry.get("repr") or entry.get("value") or ""
        if code:
            codes.append(code.upper())
    return codes


def generate_weather_alerts(icao: str, metar_json: dict) -> list[str]:
    """
    Generate weather alert strings for a single airport.
    Returns a deduplicated list of alert lines.
    NOTE: Windshear risk is simulated — not real detection.
    """
    alerts = []
    seen = set()

    speed, gust = parse_wind(metar_json)
    wx_codes = parse_wx_codes(metar_json)

    wind_label = f"{speed}KT"
    if gust:
        wind_label += f" G{gust}KT"

    # Wind alerts
    if speed >= 25:
        msg = f"🌬 WIND ALERT – {icao}: {wind_label}"
        if msg not in seen:
            alerts.append(msg)
            seen.add(msg)
    elif speed >= 20:
        msg = f"🌬 WIND ALERT – {icao}: {wind_label}"
        if msg not in seen:
            alerts.append(msg)
            seen.add(msg)

    if gust >= 35:
        msg = f"🌬 WIND ALERT – {icao}: {wind_label}"
        if msg not in seen:
            alerts.append(msg)
            seen.add(msg)

    # Severe weather alerts
    has_ts = False
    for code in wx_codes:
        if "TS" in code:
            has_ts = True
        for pattern, label in WEATHER_CODE_ALERTS.items():
            if pattern in code:
                msg = f"⚠ WEATHER ALERT – {icao}: {label}"
                if msg not in seen:
                    alerts.append(msg)
                    seen.add(msg)
                break

    # Windshear risk (simulated — not real detection)
    if has_ts and (speed >= 20 or gust >= 30):
        msg = f"🛬 POTENTIAL WINDSHEAR RISK – {icao} (simulated, not real detection)"
        if msg not in seen:
            alerts.append(msg)
            seen.add(msg)

    return alerts


def fetch_all_metar_alerts() -> list[str]:
    """
    Fetch METAR for every monitored airport and collect all weather alerts.
    Returns a deduplicated list of alert strings.
    """
    all_alerts = []
    seen_global = set()
    fetched = 0

    for icao in ICAO_LIST:
        metar_json = fetch_metar(icao)
        if metar_json is None:
            continue
        fetched += 1
        for alert in generate_weather_alerts(icao, metar_json):
            if alert not in seen_global:
                all_alerts.append(alert)
                seen_global.add(alert)

    log.info("METAR data fetched for %d airports", fetched)
    log.info("Generated %d weather alerts", len(all_alerts))
    return all_alerts


def build_weather_alert_message(alerts: list[str]) -> str:
    """Format the weather alert block for Discord."""
    lines = []
    lines.append("```")
    lines.append("╔══════════════════════════════════════════╗")
    lines.append("║       VATSIM APAC WEATHER ALERTS         ║")
    lines.append("╚══════════════════════════════════════════╝")
    for alert in alerts:
        lines.append(f"  {alert}")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main 30-minute cycle
# ---------------------------------------------------------------------------

def run_cycle() -> None:
    """
    Execute one full monitoring cycle:
      1. Fetch VATSIM data (single call)
      2. Post OPS summary
      3. Post Command Centre summary
      4. Fetch METAR data (single call per airport)
      5. Post weather alerts if any exist
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("Starting 30-minute cycle at %s", now)

    # ── Step 1: Fetch VATSIM data (once) ────────────────────────────────────
    vatsim_data = fetch_vatsim_data()
    if vatsim_data is None:
        log.error("Aborting cycle — VATSIM data unavailable.")
        return

    pilots = vatsim_data.get("pilots", [])
    controllers = vatsim_data.get("controllers", [])
    log.info("VATSIM data fetched: %d pilots, %d ATC", len(pilots), len(controllers))

    # ── Step 2: OPS Summary ──────────────────────────────────────────────────
    try:
        ops_msg = build_ops_summary(vatsim_data)
        if post_to_discord(ops_msg):
            log.info("Posted OPS summary to Discord")
        else:
            log.warning("OPS summary post failed or was skipped")
    except Exception as exc:
        log.error("Error building/posting OPS summary: %s", exc)

    # Small delay to avoid Discord rate-limiting
    time.sleep(2)

    # ── Step 3: Command Centre ───────────────────────────────────────────────
    try:
        cc_msg = build_command_centre(vatsim_data)
        if post_to_discord(cc_msg):
            log.info("Posted Command Centre summary to Discord")
        else:
            log.warning("Command Centre post failed or was skipped")
    except Exception as exc:
        log.error("Error building/posting Command Centre summary: %s", exc)

    # Small delay before METAR calls
    time.sleep(2)

    # ── Step 4 & 5: Weather Alerts ───────────────────────────────────────────
    try:
        alerts = fetch_all_metar_alerts()
        if alerts:
            weather_msg = build_weather_alert_message(alerts)
            if post_to_discord(weather_msg):
                log.info("Posted weather alerts to Discord")
            else:
                log.warning("Weather alert post failed or was skipped")
        else:
            log.info("No weather alerts this cycle — skipping Discord post")
    except Exception as exc:
        log.error("Error in weather alert system: %s", exc)

    # ── Cycle complete ───────────────────────────────────────────────────────
    log.info("OPS CYCLE COMPLETE - 30MIN LOOP EXECUTED")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("VATSIM APAC Monitoring Bot starting up.")
    log.info("Monitored airports: %s", ", ".join(ICAO_LIST))

    # Run once immediately on startup so we don't wait 30 minutes for first post
    log.info("Running initial cycle on startup...")
    run_cycle()

    # Schedule subsequent cycles every 30 minutes
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=30, id="ops_cycle")
    log.info("Scheduler started — cycle will run every 30 minutes.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

