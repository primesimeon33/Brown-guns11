"""
Family Bonus Tracker — Discord Bot

Entry point. Wires together:
  * Submission-channel message parsing
  * Admin approval workflow (persistent views)
  * In-Discord bonus calculation + Excel export on admin request

Run:  python main.py
"""

import json
import logging
import os
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from PIL import Image, UnidentifiedImageError

import config
from parsers import (
    Participant,
    build_event_details,
    build_participant_display,
    detect_event_type,
    parse_event_meta,
    parse_participants,
)
from sheets import SheetsHandler
from views import ApprovalView

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bonus_bot")


# ---------------------------------------------------------------------------
# Bot definition
# ---------------------------------------------------------------------------
class BonusBot(commands.Bot):
    """Main bot class holding the submissions store."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.sheets = SheetsHandler()
        self._submissions: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def load_submissions(self) -> None:
        """Load pending submissions from the JSON store (called on startup)."""
        if not os.path.exists(config.SUBMISSIONS_STORE):
            self._submissions = {}
            return
        try:
            with open(config.SUBMISSIONS_STORE, "r", encoding="utf-8") as f:
                self._submissions = json.load(f)
            logger.info("Loaded %d submissions from store", len(self._submissions))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read submissions store: %s", exc)
            self._submissions = {}

    def save_submissions(self) -> None:
        """Persist the submissions store to disk."""
        try:
            with open(config.SUBMISSIONS_STORE, "w", encoding="utf-8") as f:
                json.dump(self._submissions, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Could not write submissions store: %s", exc)

    def get_submission(self, submission_id: int) -> Optional[dict]:
        return self._submissions.get(str(submission_id))

    def add_submission(self, submission_id: int, data: dict) -> None:
        self._submissions[str(submission_id)] = data
        self.save_submissions()

    def remove_submission(self, submission_id: int) -> None:
        self._submissions.pop(str(submission_id), None)
        self.save_submissions()

    # ------------------------------------------------------------------
    # Approval side-effects
    # ------------------------------------------------------------------
    async def process_approval(self, submission: dict, approver: discord.Member) -> None:
        """
        Hook for side-effects after an approval.

        Bonuses are calculated in the view (and persisted into the submission's
        participant dicts) before this is called. This method posts a clean log
        embed to the approved-logs channel (if configured).
        """
        logger.info(
            "Approved submission %s (%s) — %d participants (by %s)",
            submission.get("submission_id"),
            submission.get("event_type"),
            len(submission.get("participants", [])),
            approver,
        )
        await self.post_approved_log(submission, approver)

    async def post_approved_log(
        self, submission: dict, approver: discord.Member
    ) -> None:
        """Post a clean approved-submission log embed to the logs channel."""
        if not config.APPROVED_LOGS_CHANNEL_ID:
            return

        channel = self.get_channel(config.APPROVED_LOGS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(config.APPROVED_LOGS_CHANNEL_ID)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not fetch approved-logs channel: %s", exc)
                return

        event_type = submission.get("event_type", "Unknown")
        event_details = submission.get("event_details", "")
        submitter_name = submission.get("submitter_name", "Unknown")
        image_url = submission.get("image_url")

        title = f"✅ Approved — {event_type}"
        if event_details:
            title += f" ({event_details})"

        embed = discord.Embed(
            title=title,
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )
        embed.add_field(
            name="Submitter",
            value=submitter_name,
            inline=True,
        )
        embed.add_field(
            name="Approved by",
            value=approver.mention,
            inline=True,
        )

        participants = [
            Participant.from_dict(p) for p in submission.get("participants", [])
        ]
        cfg = config.get_event_config(event_type)
        kill_based_modes = {"per_kill", "state_control"}
        show_kills = cfg["bonus_mode"] in kill_based_modes if cfg else True

        # Compute the total bonus with the right unit.
        units = {p.unit for p in participants}
        total_bonus = sum(p.bonus for p in participants)
        if len(units) == 1 and units != {"$"}:
            total_str = f"**{total_bonus:,} {next(iter(units))}**"
        else:
            total_str = f"**${total_bonus:,}**"

        embed.add_field(
            name=f"Participants ({len(participants)})",
            value=build_participant_display(
                participants, show_bonus=True, show_kills=show_kills
            ),
            inline=False,
        )
        embed.add_field(
            name="Total Bonus",
            value=total_str,
            inline=False,
        )
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(
            text=f"Approved by {approver}",
            icon_url=approver.display_avatar.url,
        )

        try:
            await channel.send(embed=embed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not post approved log to channel: %s", exc)

    async def process_rejection(self, submission: dict) -> None:
        """Notify the original submitter that their submission was rejected."""
        submitter_id = int(submission["submitter_id"])
        try:
            user = await self.fetch_user(submitter_id)
            await user.send(
                f"Hello! Your event bonus submission (event: **{submission['event_type']}**) "
                f"has been **rejected** by an admin.\n"
                f"If you believe this is a mistake, please contact the admin team."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not DM submitter %d: %s", submitter_id, exc)

    # ------------------------------------------------------------------
    # View (re)registration
    # ------------------------------------------------------------------
    async def register_pending_views(self) -> None:
        """Re-add persistent views for every pending submissions after restart."""
        for sid_str, data in self._submissions.items():
            if data.get("status") != "pending":
                continue
            view = ApprovalView(submission_id=int(sid_str), bot=self)
            self.add_view(view)
            logger.info("Re-registered approval view for submission %s", sid_str)


# ---------------------------------------------------------------------------
# Bot instance
# ---------------------------------------------------------------------------
bot = BonusBot()


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    bot.load_submissions()
    await bot.register_pending_views()
    logger.info("Bonus bot is ready.")


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    """Global error handler — log everything, never let an event crash the bot."""
    logger.exception("Unhandled exception in event '%s'", event)


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore the bot's own messages and DMs.
    if message.author == bot.user or message.guild is None:
        return

    # Only process messages in the designated submission channel.
    if message.channel.id != config.SUBMISSION_CHANNEL_ID:
        await bot.process_commands(message)
        return

    # Wrap in try/except so a single malformed submission can never crash
    # the bot's event loop.
    try:
        await handle_submission(message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error processing submission %d: %s", message.id, exc)
        try:
            await message.add_reaction("❌")
            await message.reply(
                "⚠️ Something went wrong while processing your submission. "
                "Please contact an admin.",
                delete_after=30,
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
def _member_is_admin(member: discord.Member) -> bool:
    """Return True if the member has a configured admin role (or admin perm)."""
    if not config.ADMIN_ROLE_IDS:
        return member.guild_permissions.administrator
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & set(config.ADMIN_ROLE_IDS))


@bot.command(name="export")
@commands.has_permissions(administrator=True)
async def export_bonuses(ctx: commands.Context) -> None:
    """
    Export all approved submissions to a Google Sheet and post its link.

    The sheet contains two tabs:
      * Summary — per-user totals (kills, bonus, events attended)
      * Details — one row per approved participant

    The sheet is set to "anyone with the link can view".
    Admin-only (configurable via ADMIN_ROLE_IDS).
    """
    if not isinstance(ctx.author, discord.Member) or not _member_is_admin(ctx.author):
        await ctx.reply("❌ You do not have permission to export bonuses.", delete_after=15)
        return

    submissions = list(bot._submissions.values())
    approved = [s for s in submissions if s.get("status") == "approved"]

    if not approved:
        await ctx.reply("ℹ️ There are no approved submissions to export yet.")
        return

    # Defer so the Google Sheets API call (which can take a few seconds)
    # doesn't hit Discord's 3-second interaction/command timeout.
    await ctx.trigger_typing()

    try:
        sheet_url = bot.sheets.export_submissions(submissions)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to export to Google Sheets: %s", exc)
        await ctx.reply(f"⚠️ Failed to generate the Google Sheet: {exc}")
        return

    total_bonus = sum(
        int(p.get("bonus", 0))
        for s in approved
        for p in s.get("participants", [])
    )
    unique_users = len(
        {int(p["user_id"]) for s in approved for p in s.get("participants", [])}
    )

    await ctx.reply(
        f"📊 **Bonus Export**\n"
        f"Approved submissions: **{len(approved)}**\n"
        f"Unique users: **{unique_users}**\n"
        f"Total bonus paid out: **${total_bonus:,}**\n\n"
        f"🔗 [Open Google Sheet]({sheet_url})\n"
        f"_(Anyone with this link can view it.)_"
    )


@bot.command(name="stats")
@commands.has_permissions(administrator=True)
async def show_stats(ctx: commands.Context) -> None:
    """
    Show bonus statistics directly in Discord (no file download).

    Displays:
      * Total approved submissions
      * Unique users
      * Total bonus paid out
      * Top 10 users by bonus amount

    Admin-only (configurable via ADMIN_ROLE_IDS).
    """
    if not isinstance(ctx.author, discord.Member) or not _member_is_admin(ctx.author):
        await ctx.reply("❌ You do not have permission to view bonus stats.", delete_after=15)
        return

    submissions = list(bot._submissions.values())
    approved = [s for s in submissions if s.get("status") == "approved"]

    if not approved:
        await ctx.reply("ℹ️ There are no approved submissions yet.")
        return

    # Aggregate per-user totals.
    totals: Dict[int, Dict] = {}
    for sub in approved:
        for p in sub.get("participants", []):
            uid = int(p["user_id"])
            entry = totals.setdefault(
                uid,
                {
                    "username": p.get("username") or f"<@{uid}>",
                    "kills": 0,
                    "bonus": 0,
                    "events": 0,
                },
            )
            entry["kills"] += int(p.get("kills", 0))
            entry["bonus"] += int(p.get("bonus", 0))
            entry["events"] += 1

    sorted_users = sorted(totals.items(), key=lambda kv: kv[1]["bonus"], reverse=True)
    total_bonus = sum(d["bonus"] for _, d in sorted_users)
    total_kills = sum(d["kills"] for _, d in sorted_users)

    # Build the top-10 leaderboard (Discord embeds have a 4096 char description limit).
    lines = []
    for rank, (uid, data) in enumerate(sorted_users[:10], start=1):
        medal = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"`{rank}.`"
        lines.append(
            f"{medal} **{data['username']}** — "
            f"${data['bonus']:,} ({data['kills']} kills, {data['events']} events)"
        )

    leaderboard = "\n".join(lines) if lines else "_No data._"

    embed = discord.Embed(
        title="🏆 Bonus Statistics",
        color=discord.Color.gold(),
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="Total Bonus Paid Out",
        value=f"**${total_bonus:,}**",
        inline=True,
    )
    embed.add_field(
        name="Total Kills",
        value=f"**{total_kills:,}**",
        inline=True,
    )
    embed.add_field(
        name="Approved Submissions",
        value=f"**{len(approved)}**",
        inline=True,
    )
    embed.add_field(
        name="Top Earners",
        value=leaderboard,
        inline=False,
    )
    embed.set_footer(text=f"Tracks {len(sorted_users)} unique users")

    await ctx.reply(embed=embed)


# ---------------------------------------------------------------------------
# Submission handling
# ---------------------------------------------------------------------------
async def handle_submission(message: discord.Message) -> None:
    """Validate, parse, and forward a user submission to the admin channel."""
    content = message.content

    # ---- 1. Validate the screenshot attachment ----
    image_attachment = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_attachment = att
            break

    if image_attachment is None:
        await send_guidance(
            message,
            "Your submission is missing a screenshot.\n"
            "Please attach an image and try again.",
        )
        return

    # Verify the attachment is actually a readable image (rejects corrupt files
    # or files that merely claim an image content-type).
    if not await validate_image(image_attachment):
        await send_guidance(
            message,
            "The attached file does not appear to be a valid image.\n"
            "Please attach a valid screenshot (PNG/JPG/etc.).",
        )
        return

    # ---- 2. Detect event type ----
    event_type = detect_event_type(content)
    if event_type is None:
        await send_guidance(
            message,
            "I could not detect a supported event type in your message.\n"
            "Supported events: Informals, Biz War, State Control, Weapons Factory, "
            "Rating Battle, Foundry, Trade Center, Battle of Harbour, RP Ticket, "
            "Caravan, Fam Raid.",
        )
        return

    # ---- 3. Parse participants & event metadata ----
    participants = parse_participants(content)
    if not participants:
        await send_guidance(
            message,
            "No participants were found.\n"
            "Please tag users followed by their kills, e.g. `@User 3k @User2 0k`.",
        )
        return

    meta = parse_event_meta(content)

    # Resolve usernames from the guild for nicer display.
    await resolve_usernames(message.guild, participants)

    # ---- 4. Add the pending reaction ----
    await message.add_reaction("⏳")

    # ---- 5. Build the admin embed ----
    event_details = build_event_details(meta)
    embed = build_admin_embed(
        message=message,
        event_type=event_type,
        event_details=event_details,
        image_url=image_attachment.url,
        participants=participants,
    )

    admin_channel = bot.get_channel(config.ADMIN_APPROVAL_CHANNEL_ID)
    if admin_channel is None:
        logger.error("Admin approval channel not found (ID %d).", config.ADMIN_APPROVAL_CHANNEL_ID)
        return

    view = ApprovalView(submission_id=message.id, bot=bot)
    admin_message = await admin_channel.send(embed=embed, view=view)

    # ---- 6. Persist the submission ----
    submission_data = {
        "submission_id": message.id,
        "channel_id": message.channel.id,
        "submitter_id": message.author.id,
        "submitter_name": str(message.author),
        "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M"),
        "event_type": event_type,
        "event_details": event_details,
        "meta": meta,
        "image_url": image_attachment.url,
        "participants": [p.to_dict() for p in participants],
        "admin_message_id": admin_message.id,
        "admin_channel_id": admin_channel.id,
        "status": "pending",
    }
    bot.add_submission(message.id, submission_data)

    logger.info(
        "New submission %d from %s (%s) — %d participants",
        message.id,
        message.author,
        event_type,
        len(participants),
    )


# ---------------------------------------------------------------------------
# Embed / display helpers
# ---------------------------------------------------------------------------
def build_admin_embed(
    *,
    message: discord.Message,
    event_type: str,
    event_details: str,
    image_url: str,
    participants: List[Participant],
) -> discord.Embed:
    """Construct the embed posted to the admin approval channel."""
    title = f"New Submission — {event_type}"
    if event_details:
        title += f" ({event_details})"

    embed = discord.Embed(
        title=title,
        color=discord.Color.blurple(),
        timestamp=message.created_at,
    )
    embed.set_author(
        name=f"Submitted by {message.author}",
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(
        name="Event Type",
        value=event_type,
        inline=True,
    )
    embed.add_field(
        name="Submitter",
        value=f"{message.author.mention}\nID: `{message.author.id}`",
        inline=True,
    )
    cfg = config.get_event_config(event_type)
    kill_based_modes = {"per_kill", "state_control"}
    show_kills = cfg["bonus_mode"] in kill_based_modes if cfg else True
    embed.add_field(
        name="Participants" + (" & Kills" if show_kills else ""),
        value=build_participant_display(participants, show_kills=show_kills),
        inline=False,
    )
    embed.set_image(url=image_url)
    embed.set_footer(text="⏳ Pending approval")
    return embed


async def resolve_usernames(
    guild: Optional[discord.Guild], participants: List[Participant]
) -> None:
    """Fill in the username for each participant where possible."""
    if guild is None:
        return
    for p in participants:
        if p.username:
            continue
        member = guild.get_member(p.user_id)
        if member is not None:
            p.username = str(member)


async def validate_image(attachment: discord.Attachment) -> bool:
    """
    Download the attachment and confirm it opens as a valid image via Pillow.
    Returns False for corrupt or unreadable files.
    """
    try:
        data = await attachment.read()
        with Image.open(BytesIO(data)) as img:
            img.verify()  # raises on corrupt data
        return True
    except (UnidentifiedImageError, OSError, Exception) as exc:  # noqa: BLE001
        logger.warning("Image validation failed for %s: %s", attachment.filename, exc)
        return False


async def send_guidance(message: discord.Message, text: str) -> None:
    """Send a temporary guidance message and add a ❌ reaction to the submission."""
    await message.add_reaction("❌")
    try:
        await message.reply(
            f"⚠️ **Submission not processed**\n{text}\n\n"
            "**Expected format:**\n"
            "```\n"
            "<Event Type> [date time]\n"
            "@User1 3k @User2 0k @User3 12k\n"
            "[attached screenshot]\n"
            "```",
            delete_after=30,
        )
    except discord.Forbidden:
        logger.warning("Cannot reply in channel %d", message.channel.id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _validate_config() -> None:
    """Log warnings for any missing/zero configuration values."""
    checks = [
        ("DISCORD_BOT_TOKEN", bool(config.DISCORD_BOT_TOKEN)),
        ("SUBMISSION_CHANNEL_ID", config.SUBMISSION_CHANNEL_ID != 0),
        ("ADMIN_APPROVAL_CHANNEL_ID", config.ADMIN_APPROVAL_CHANNEL_ID != 0),
    ]
    for name, ok in checks:
        if not ok:
            logger.warning("⚠️  %s is not set or is zero — check your Railway env vars.", name)

    if not config.APPROVED_LOGS_CHANNEL_ID:
        logger.info(
            "APPROVED_LOGS_CHANNEL_ID is not set — approved submissions will "
            "not be posted to a logs channel."
        )

    if not config.ADMIN_ROLE_IDS:
        logger.warning(
            "⚠️  ADMIN_ROLE_IDS is empty — falling back to server Administrator "
            "permission for approvals and the !export command."
        )

    if not config.GOOGLE_SERVICE_ACCOUNT_JSON or (
        not os.path.isfile(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        and len(config.GOOGLE_SERVICE_ACCOUNT_JSON.strip()) < 50
    ):
        logger.warning(
            "⚠️  GOOGLE_SERVICE_ACCOUNT_JSON is not set — the !export command "
            "will fail. Set it to a file path (local) or raw/base64 JSON (Railway)."
        )


def main() -> None:
    _validate_config()

    if not config.DISCORD_BOT_TOKEN:
        logger.critical("DISCORD_BOT_TOKEN is not set. Set it in Railway variables.")
        raise SystemExit(1)

    try:
        bot.run(config.DISCORD_BOT_TOKEN)
    except discord.PrivilegedIntentsRequired:
        logger.critical(
            "Privileged intents required but not enabled. Go to the Discord "
            "Developer Portal → Bot → enable 'Message Content Intent' and "
            "'Server Members Intent'."
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.critical("Bot failed to start: %s", exc, exc_info=True)
        raise


if __name__ == "__main__":
    main()
