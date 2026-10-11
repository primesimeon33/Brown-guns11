"""
Parsing utilities for submission messages.

Handles:
  * Detecting the event type from the message header.
  * Extracting Discord mentions and the kill count associated with each.
  * Extracting the (Event | time | date) parenthetical block.
  * Extracting event-specific metadata (territory size, parachute count, etc.).
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from config import EVENT_TYPES, get_event_unit

# Matches a Discord user mention, e.g. <@123456> or <@!123456>
MENTION_RE = re.compile(r"<@!?(\d+)>")

# Matches a kill token: "3k", "3 k", "3K", "3 kills", "3 Kills", "0k".
STANDALONE_KILLS_RE = re.compile(r"(\d+)\s*k(?:ills?)?", re.IGNORECASE)

# Matches the parenthetical block: ( EventName | time | date [| extra ] )
# e.g. (Informal | 18:00 | 11/10)  or  (State Control | 20:00 | 12/10 | Small)
PAREN_BLOCK_RE = re.compile(r"\(([^)]*)\)")

# Individual part patterns inside the parenthetical block
TIME_RE = re.compile(r"\d{1,2}:\d{2}")
DATE_RE = re.compile(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?")

# Event-specific metadata patterns (scanned from the full message body)
PARACHUTE_RE = re.compile(r"(\d+)\s*parachute", re.IGNORECASE)
CARAVAN_RE = re.compile(r"(\d+)\s*caravan", re.IGNORECASE)
SMALL_TERRITORY_RE = re.compile(r"\bsmall\b", re.IGNORECASE)
BIG_TERRITORY_RE = re.compile(r"\b(big|large)\b", re.IGNORECASE)


@dataclass
class Participant:
    user_id: int
    kills: int
    username: str = ""  # resolved later via API
    bonus: int = 0  # computed at approval time
    unit: str = "$"  # "$" or "RP Ticket"

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "kills": self.kills,
            "username": self.username,
            "bonus": self.bonus,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Participant":
        return cls(
            user_id=int(data["user_id"]),
            kills=int(data["kills"]),
            username=data.get("username", ""),
            bonus=int(data.get("bonus", 0)),
            unit=data.get("unit", "$"),
        )


def detect_event_type(content: str) -> Optional[str]:
    """
    Inspect the message content and return the canonical event type name,
    or None if no recognised event keyword is found.
    """
    lowered = content.lower()
    for canonical, cfg in EVENT_TYPES.items():
        for alias in cfg["aliases"]:
            if re.search(rf"(?:^|\W){re.escape(alias)}(?:\W|$)", lowered):
                return canonical
    return None


def parse_event_meta(content: str) -> dict:
    """
    Extract event metadata from the submission message.

    Returns a dict with:
      time       - "18:00" or ""
      date       - "11/10" or ""
      territory  - "small", "big", or None
      parachutes - int (total parachute count)
      caravans   - int (total caravan count)
    """
    meta = {
        "time": "",
        "date": "",
        "territory": None,
        "parachutes": 0,
        "caravans": 0,
    }

    # 1. Parse the (Event | time | date [| extra]) parenthetical block.
    paren_match = PAREN_BLOCK_RE.search(content)
    if paren_match:
        parts = [p.strip() for p in paren_match.group(1).split("|")]
        for part in parts:
            if TIME_RE.fullmatch(part):
                meta["time"] = part
            elif DATE_RE.fullmatch(part):
                meta["date"] = part
            elif SMALL_TERRITORY_RE.search(part):
                meta["territory"] = "small"
            elif BIG_TERRITORY_RE.search(part):
                meta["territory"] = "big"

    # 2. Scan the full body for event-specific counts / territory keywords.
    para_match = PARACHUTE_RE.search(content)
    if para_match:
        meta["parachutes"] = int(para_match.group(1))

    car_match = CARAVAN_RE.search(content)
    if car_match:
        meta["caravans"] = int(car_match.group(1))

    if meta["territory"] is None:
        if SMALL_TERRITORY_RE.search(content):
            meta["territory"] = "small"
        elif BIG_TERRITORY_RE.search(content):
            meta["territory"] = "big"

    return meta


def build_event_details(meta: dict) -> str:
    """Build a human-readable event details string from parsed meta."""
    parts = []
    if meta.get("date"):
        parts.append(meta["date"])
    if meta.get("time"):
        parts.append(meta["time"])
    if meta.get("territory"):
        parts.append(meta["territory"].title() + " Territory")
    return " · ".join(parts)


def parse_participants(content: str) -> List[Participant]:
    """
    Parse every mention and its associated kill count.

    Strategy: find all mention positions, then for each mention locate the
    *next* kill token that appears after it (before the next mention).
    Participants without a recognisable kill count default to 0 kills.
    """
    mentions = list(MENTION_RE.finditer(content))
    if not mentions:
        return []

    kill_tokens = list(STANDALONE_KILLS_RE.finditer(content))

    participants: List[Participant] = []
    for idx, mention_match in enumerate(mentions):
        user_id = int(mention_match.group(1))
        mention_end = mention_match.end()

        next_mention_start = (
            mentions[idx + 1].start() if idx + 1 < len(mentions) else len(content)
        )

        kills = 0
        for kt in kill_tokens:
            if kt.start() >= mention_end and kt.start() < next_mention_start:
                kills = int(kt.group(1))
                break

        participants.append(Participant(user_id=user_id, kills=kills))

    return participants


def build_participant_display(
    participants: List[Participant],
    show_bonus: bool = False,
    show_kills: bool = True,
) -> str:
    """
    Return a readable multi-line list of participants for embeds.

    show_kills=False hides the kill count (used for non-kill events where
    kills are always 0 and would just clutter the display).
    """
    lines = []
    for p in participants:
        name = p.username or f"<@{p.user_id}>"
        line = f"• {name}"
        if show_kills:
            line += f" — **{p.kills}** kills"
        if show_bonus and p.bonus:
            if p.unit == "$":
                line += f" → **${p.bonus:,}**"
            else:
                line += f" → **{p.bonus} {p.unit}**"
        lines.append(line)
    return "\n".join(lines) if lines else "_No participants detected._"
