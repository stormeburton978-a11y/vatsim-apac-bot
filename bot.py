"""
VATSIM APAC Monitoring Bot
Fetches live VATSIM data every 30 minutes and posts formatted
traffic summaries to a Discord webhook.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VATSIM_DATA_URL = "https://data.vatsim.net/v3/vatsim-data.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
POLL_INTERVAL = 30 * 60  # 30 minutes in seconds

# Monitored APAC airports: ICAO -> display name
APAC_AIRPORTS = {
    "YSSY": "Sydney Kingsford Smith",
    "NZAA": "Auckland International",
    "WADD": "Ngurah Rai (Bali)",
    "YBBN": "Brisbane International",
    "YPPH": "Perth International",
    "YMML": "Melbourne Tullamarine",
    "YPDN": "Darwin International",
    "YPAD": "Adelaide International",
    "NZWN": "Wellington International",
    "YSCB": "Canberra International",
    "NZCH": "Christchurch International",
    "NFFN": "Nadi International",
}

# Traffic thresholds (pilot count) for status categorisation
TRAFFIC_THRESHOLDS = {
    "VERY BUSY": 10,   # >= 10 pilots  → 🔴
    "BUSY":       6,   # >= 6  pilots  → 🟠
    "MODERATE":   2,   # >= 2  pilots  → 🟡
    # < 2 pilots                       → 🟢 QUIET
}

STATUS_EMOJI = {
    "VERY BUSY": "🔴",
    "BUSY":      "🟠",
    "MODERATE":  "🟡",
    "QUIET":     "🟢",
}

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
# VATSIM data helpers
# ---------------------------------------------------------------------------

def fetch_vatsim_data() -> dict:
    """Download and return the raw VATSIM v3 JSON payload."""
    resp = requests.get(VATSIM_DATA_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def count_traffic(data: dict) -> dict[str, dict]:
    """
    Walk the VATSIM payload and return per-airport traffic counts.

    Returns a dict keyed by ICAO with:
        pilots  – number of pilots whose departure airport matches
        atc     – number of ATC positions whose callsign starts with
                  the airport's ICAO prefix
    """
    counts: dict[str, dict] = {
        icao: {"pilots": 0, "atc": 0} for icao in APAC_AIRPORTS
    }

    # Pilots: match on departure airport (flight plan field)
    for pilot in data.get("pilots", []):
        fp = pilot.get("flight_plan") or {}
        dep = (fp.get("departure") or "").upper()
        if dep in counts:
            counts[dep]["pilots"] += 1

    # ATC: match callsign prefix against each ICAO code
    for controller in data.get("controllers", []):
        callsign = (controller.get("callsign") or "").upper()
        for icao in counts:
            if callsign.startswith(icao):
                counts[icao]["atc"] += 1
                break  # one controller can only match one airport

    return counts


def classify_traffic(pilot_count: int) -> str:
    """Return a traffic status string based on pilot count."""
    if pilot_count >= TRAFFIC_THRESHOLDS["VERY BUSY"]:
        return "VERY BUSY"
    if pilot_count >= TRAFFIC_THRESHOLDS["BUSY"]:
        return "BUSY"
    if pilot_count >= TRAFFIC_THRESHOLDS["MODERATE"]:
        return "MODERATE"
    return "QUIET"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def build_message(counts: dict[str, dict]) -> str:
    """
    Build the full Discord message string.

    Example layout:

    ╔══════════════════════════════════════╗
    ║     VATSIM APAC TRAFFIC MONITOR      ║
    ╚══════════════════════════════════════╝
    MSG ID: APAC-20240615-1430Z
    DTG: 151430Z JUN 2024
    ──────────────────────────────────────
    REGION STATUS: 🟠 BUSY
    Total PIX: 42  |  Total ATC: 11
    ──────────────────────────────────────
    AIRPORT BREAKDOWN
    🔴 YSSY  Sydney Kingsford Smith
         PIX: 14  |  ATC: 4
    ...
    ══════════════════════════════════════
    """
    now = datetime.now(timezone.utc)
    msg_id  = now.strftime("APAC-%Y%m%d-%H%MZ")
    dtg     = now.strftime("%d%H%MZ %b %Y").upper()

    total_pilots = sum(v["pilots"] for v in counts.values())
    total_atc    = sum(v["atc"]    for v in counts.values())

    # Overall region status is driven by the busiest single airport
    max_pilots   = max((v["pilots"] for v in counts.values()), default=0)
    region_status = classify_traffic(max_pilots)
    region_emoji  = STATUS_EMOJI[region_status]

    width = 42  # inner width of the ASCII box

    lines: list[str] = []

    # Header box
    lines.append(f"```")
    lines.append(f"╔{'═' * width}╗")
    title = "VATSIM APAC TRAFFIC MONITOR"
    lines.append(f"║{title.center(width)}║")
    lines.append(f"╚{'═' * width}╝")
    lines.append(f"MSG ID : {msg_id}")
    lines.append(f"DTG    : {dtg}")
    lines.append("─" * (width + 2))
    lines.append(f"REGION STATUS : {region_emoji} {region_status}")
    lines.append(f"TOTAL PIX : {total_pilots:>3}   |   TOTAL ATC : {total_atc:>3}")
    lines.append("─" * (width + 2))
    lines.append("AIRPORT BREAKDOWN")
    lines.append("")

    # Per-airport rows, sorted by pilot count descending
    sorted_airports = sorted(
        APAC_AIRPORTS.items(),
        key=lambda kv: counts[kv[0]]["pilots"],
        reverse=True,
    )

    for icao, name in sorted_airports:
        pilots = counts[icao]["pilots"]
        atc    = counts[icao]["atc"]
        status = classify_traffic(pilots)
        emoji  = STATUS_EMOJI[status]
        lines.append(f"{emoji} {icao}  {name}")
        lines.append(f"     PIX : {pilots:>3}   |   ATC : {atc:>3}")
        lines.append("")

    lines.append("═" * (width + 2))
    lines.append("```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def post_to_discord(message: str) -> None:
    """Send a plain-text message to the configured Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL is not set — cannot post to Discord.")
        return

    payload = {"content": message}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

    if resp.status_code in (200, 204):
        log.info("Posted traffic summary to Discord.")
    else:
        log.error(
            "Discord webhook returned HTTP %s: %s",
            resp.status_code,
            resp.text[:200],
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> None:
    """Fetch VATSIM data, build a summary, and post it to Discord."""
    log.info("Fetching VATSIM data from %s", VATSIM_DATA_URL)
    try:
        data = fetch_vatsim_data()
    except Exception as exc:
        log.error("Failed to fetch VATSIM data: %s", exc)
        return

    counts  = count_traffic(data)
    message = build_message(counts)
    log.info("Built message (%d chars). Posting to Discord…", len(message))
    post_to_discord(message)


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        log.error(
            "DISCORD_WEBHOOK_URL environment variable is not set. "
            "Set it before starting the bot."
        )
        sys.exit(1)

    log.info(
        "VATSIM APAC Monitor starting — polling every %d minutes.",
        POLL_INTERVAL // 60,
    )

    while True:
        run_once()
        log.info("Sleeping for %d minutes…", POLL_INTERVAL // 60)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

