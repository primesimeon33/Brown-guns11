"""
Google Sheets handler for the Family Bonus Tracker export.

Uses a service account (credentials from env var — raw JSON or base64,
no file required on Railway) to:
  * Open or create a spreadsheet named "Family_Bonus_Tracker"
  * Clear & rewrite the "Summary" and "Details" tabs on each export
  * Make the sheet viewable by anyone with the link
  * Return the shareable URL
"""

import base64
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_NAME

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

_SUMMARY_HEADERS = ["Username", "User ID", "Total Kills", "Total Bonus", "Events Attended"]
_DETAILS_HEADERS = ["Date", "Event Type", "Submitter", "User ID", "Username", "Kills", "Bonus"]


class SheetsHandler:
    """Wrapper around gspread for the bonus-export workflow."""

    def __init__(
        self,
        credentials_path: str = GOOGLE_SERVICE_ACCOUNT_JSON,
        sheet_name: str = GOOGLE_SHEET_NAME,
    ) -> None:
        self.credentials_path = credentials_path
        self.sheet_name = sheet_name
        self._client: Optional[gspread.Client] = None
        self._spreadsheet: Optional[gspread.Spreadsheet] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_credentials(credentials_path: str) -> str:
        """
        Resolve service-account credentials to a JSON file path.

        Supports (in order):
          1. A path to an existing JSON key file (local dev).
          2. A base64-encoded JSON key (Railway env var).
          3. Raw JSON text (Railway env var).
        """
        if os.path.isfile(credentials_path):
            return credentials_path

        raw = credentials_path.strip()
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
            json.loads(decoded)
            payload = decoded
        except Exception:  # noqa: BLE001 - not base64, try raw JSON
            try:
                json.loads(raw)
                payload = raw
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is neither an existing file, "
                    "a base64-encoded JSON key, nor valid raw JSON."
                ) from exc

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        tmp.write(payload)
        tmp.close()
        logger.info("Wrote service-account credentials to temp file %s", tmp.name)
        return tmp.name

    def connect(self) -> None:
        creds_path = self._resolve_credentials(self.credentials_path)
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, _SCOPES)
        self._client = gspread.authorize(creds)
        logger.info("Connected to Google Sheets API.")

    def _ensure_connected(self) -> None:
        if self._client is None:
            self.connect()

    # ------------------------------------------------------------------
    # Spreadsheet management
    # ------------------------------------------------------------------
    def get_spreadsheet(self) -> gspread.Spreadsheet:
        """Open the target spreadsheet by name, creating it if missing."""
        self._ensure_connected()
        assert self._client is not None

        try:
            self._spreadsheet = self._client.open(self.sheet_name)
        except gspread.SpreadsheetNotFound:
            self._spreadsheet = self._client.create(self.sheet_name)
            logger.info("Created new spreadsheet: %s", self.sheet_name)

        return self._spreadsheet

    def _get_or_create_worksheet(self, title: str, rows: int = 100, cols: int = 10) -> gspread.Worksheet:
        ss = self.get_spreadsheet()
        try:
            return ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=title, rows=str(rows), cols=str(cols))
            logger.info("Created worksheet: %s", title)
            return ws

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_submissions(self, submissions: List[dict]) -> str:
        """
        Write approved submissions to the Summary + Details tabs.

        Returns the spreadsheet's shareable URL.
        """
        approved = [s for s in submissions if s.get("status") == "approved"]

        ss = self.get_spreadsheet()

        # --- Summary sheet ---
        summary_ws = self._get_or_create_worksheet("Summary")
        summary_ws.clear()

        totals: Dict[int, Dict[str, int]] = {}
        for sub in approved:
            event_type = sub.get("event_type", "")
            for p in sub.get("participants", []):
                uid = int(p["user_id"])
                kills = int(p.get("kills", 0))
                bonus = int(p.get("bonus", 0))
                entry = totals.setdefault(
                    uid,
                    {
                        "username": p.get("username") or f"User#{uid}",
                        "kills": 0,
                        "bonus": 0,
                        "events": 0,
                    },
                )
                entry["kills"] += kills
                entry["bonus"] += bonus
                entry["events"] += 1

        sorted_rows = sorted(totals.items(), key=lambda kv: kv[1]["bonus"], reverse=True)

        summary_data: List[list] = [_SUMMARY_HEADERS]
        for uid, data in sorted_rows:
            summary_data.append(
                [data["username"], str(uid), data["kills"], data["bonus"], data["events"]]
            )
        if sorted_rows:
            total_bonus = sum(d["bonus"] for _, d in sorted_rows)
            total_kills = sum(d["kills"] for _, d in sorted_rows)
            summary_data.append(["GRAND TOTAL", "", total_kills, total_bonus, ""])

        summary_ws.update(summary_data, value_input_option="USER_ENTERED")

        # --- Details sheet ---
        details_ws = self._get_or_create_worksheet("Details")
        details_ws.clear()

        details_data: List[list] = [_DETAILS_HEADERS]
        for sub in approved:
            event_type = sub.get("event_type", "")
            submitter = sub.get("submitter_name", "")
            date = sub.get("timestamp", "")
            for p in sub.get("participants", []):
                kills = int(p.get("kills", 0))
                bonus = int(p.get("bonus", 0))
                details_data.append([
                    date,
                    event_type,
                    submitter,
                    str(p["user_id"]),
                    p.get("username") or f"User#{p['user_id']}",
                    kills,
                    bonus,
                ])

        details_ws.update(details_data, value_input_option="USER_ENTERED")

        # Make the spreadsheet viewable by anyone with the link.
        try:
            self._make_viewable(ss)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not set sheet sharing permissions: %s", exc)

        logger.info(
            "Exported %d approved submissions (%d unique users) to %s",
            len(approved),
            len(totals),
            self.sheet_name,
        )
        return ss.url

    def _make_viewable(self, ss: gspread.Spreadsheet) -> None:
        """Set the spreadsheet to 'anyone with link can view'."""
        ss.share("", perm_type="anyone", role="reader", with_link=True)
        logger.info("Set sheet to viewable by anyone with the link.")
