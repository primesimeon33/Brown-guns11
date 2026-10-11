"""
Centralized configuration for the Family Bonus Tracker bot.

All tunable values (rates, event definitions, channel IDs, credentials) live here
so they can be modified without touching the core logic.
"""

import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# Discord configuration
# ---------------------------------------------------------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

SUBMISSION_CHANNEL_ID = int(os.getenv("SUBMISSION_CHANNEL_ID", "0"))
ADMIN_APPROVAL_CHANNEL_ID = int(os.getenv("ADMIN_APPROVAL_CHANNEL_ID", "0"))

# Channel where every approved submission is posted as a public log.
APPROVED_LOGS_CHANNEL_ID = int(os.getenv("APPROVED_LOGS_CHANNEL_ID", "0"))

# Role IDs permitted to approve / reject submissions
_admin_role_raw = os.getenv("ADMIN_ROLE_IDS", "")
ADMIN_ROLE_IDS = [
    int(rid.strip()) for rid in _admin_role_raw.split(",") if rid.strip()
]

# ---------------------------------------------------------------------------
# Event definitions & bonus rules
# ---------------------------------------------------------------------------
# Each event has:
#   aliases      - keywords that identify it in a submission (case-insensitive)
#   bonus_mode   - how the bonus is calculated
#   rate         - primary rate (per-kill, per-person, per-caravan, etc.)
#   unit         - currency/unit for display ("$" or "RP Ticket")
#   flat_bonus   - (optional) extra flat amount added per person
#   small_rate / big_rate - used by "state_control" for territory tiers
#
# bonus_mode values:
#   per_kill       -> rate * kills
#   per_person     -> rate (+ flat_bonus if set) per participant
#   flat_join      -> rate per participant (just for joining)
#   state_control  -> small_rate or big_rate * kills (based on territory)
#   per_parachute  -> rate * total_parachutes, split evenly among participants
#   per_caravan    -> rate * total_caravans, split evenly among participants
#   rp_ticket      -> rate RP tickets per person
EVENT_TYPES = {
    "Biz War": {
        "aliases": ["biz war", "bizwar", "biz-war"],
        "bonus_mode": "per_kill",
        "rate": 100_000,
        "unit": "$",
    },
    "Rating Battle": {
        "aliases": ["rating battle", "ratingbattle", "rating-battle", "rating"],
        "bonus_mode": "per_kill",
        "rate": 15_000,
        "unit": "$",
    },
    "Trade Center": {
        "aliases": ["trade center", "tradecenter", "trade-center"],
        "bonus_mode": "per_person",
        "rate": 50_000,
        "flat_bonus": 100_000,  # registration bonus per person
        "unit": "$",
    },
    "Weapons Factory": {
        "aliases": ["weapon factory", "weapons factory", "weaponfactory", "weapon-factory", "wf"],
        "bonus_mode": "per_kill",
        "rate": 20_000,
        "unit": "$",
    },
    "Foundry": {
        "aliases": ["foundry"],
        "bonus_mode": "per_kill",
        "rate": 30_000,
        "unit": "$",
    },
    "Informals": {
        "aliases": ["informal", "informals"],
        "bonus_mode": "per_kill",
        "rate": 50_000,
        "unit": "$",
    },
    "State Control": {
        "aliases": ["state", "state control"],
        "bonus_mode": "state_control",
        "small_rate": 50_000,
        "big_rate": 100_000,
        "unit": "$",
    },
    "Battle of Harbour": {
        "aliases": ["battle of harbour", "harbour", "harbor"],
        "bonus_mode": "per_parachute",
        "rate": 25_000,
        "unit": "$",
    },
    "RP Ticket": {
        "aliases": ["rp ticket", "rp"],
        "bonus_mode": "rp_ticket",
        "rate": 1,
        "unit": "RP Ticket",
    },
    "Caravan": {
        "aliases": ["caravan", "carvan"],
        "bonus_mode": "per_caravan",
        "rate": 20_000,
        "unit": "$",
    },
    "Fam Raid": {
        "aliases": ["fam raid", "family raid"],
        "bonus_mode": "flat_join",
        "rate": 10_000,
        "unit": "$",
    },
}


def get_event_config(event_type: str) -> Optional[dict]:
    """Return the config dict for an event type, or None."""
    return EVENT_TYPES.get(event_type)


def get_event_unit(event_type: str) -> str:
    """Return the display unit for an event (default '$')."""
    cfg = EVENT_TYPES.get(event_type)
    return cfg["unit"] if cfg else "$"


def calculate_bonus(
    event_type: str,
    kills: int,
    num_participants: int,
    meta: dict,
) -> int:
    """
    Calculate the bonus for a single participant of an event.

    meta may contain:
      territory   - "small" or "big" (for State Control)
      parachutes  - total parachute count (for Battle of Harbour)
      caravans    - total caravan count (for Caravan)
    """
    cfg = EVENT_TYPES.get(event_type)
    if not cfg:
        return 0

    mode = cfg["bonus_mode"]
    n = max(num_participants, 1)

    if mode == "per_kill":
        return kills * cfg["rate"]

    if mode == "state_control":
        if meta.get("territory") == "small":
            return kills * cfg["small_rate"]
        return kills * cfg["big_rate"]

    if mode == "per_person":
        bonus = cfg["rate"]
        if "flat_bonus" in cfg:
            bonus += cfg["flat_bonus"]
        return bonus

    if mode == "flat_join":
        return cfg["rate"]

    if mode == "per_parachute":
        total = meta.get("parachutes", 0) * cfg["rate"]
        return total // n

    if mode == "per_caravan":
        total = meta.get("caravans", 0) * cfg["rate"]
        return total // n

    if mode == "rp_ticket":
        return cfg["rate"]

    return 0

# ---------------------------------------------------------------------------
# Google Sheets configuration (for the !export command)
# ---------------------------------------------------------------------------
# Service account credentials. On Railway you CANNOT upload a file, so set
# this to the FULL CONTENTS of the service account JSON key (raw JSON), OR to
# a base64-encoded version of it. Locally you can keep it as a file path.
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Family_Bonus_Tracker")

# ---------------------------------------------------------------------------
# Persistence file for submissions (survives bot restarts)
# ---------------------------------------------------------------------------
SUBMISSIONS_STORE = "submissions.json"

# Column headers used in the exported Excel "Details" sheet.
EXPORT_HEADERS = [
    "Date",
    "Event Type",
    "User ID",
    "Username / Discord Tag",
    "Kills Recorded",
    "Calculated Bonus Amount",
    "Status",
]
