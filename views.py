"""
Discord UI views for the admin approval workflow.

The ApprovalView is *persistent* (timeout=None) so button clicks keep working
even after the bot restarts, as long as the view is re-registered on startup.
"""

import logging
from typing import TYPE_CHECKING

import discord

from config import ADMIN_ROLE_IDS, calculate_bonus, get_event_config, get_event_unit
from parsers import Participant, build_participant_display

if TYPE_CHECKING:
    from main import BonusBot

logger = logging.getLogger(__name__)


def _is_admin(member: discord.Member) -> bool:
    """Return True if the member has any of the configured admin roles."""
    if not ADMIN_ROLE_IDS:
        # If no roles are configured, fall back to server administrator permission.
        return member.guild_permissions.administrator
    member_role_ids = {role.id for role in member.roles}
    return bool(member_role_ids & set(ADMIN_ROLE_IDS))


class ApprovalView(discord.ui.View):
    """Persistent view with Approve / Reject buttons for a single submission."""

    def __init__(self, submission_id: int, bot: "BonusBot") -> None:
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.bot = bot

        # Assign deterministic custom_ids so the view can be re-registered
        # after a restart and still match incoming interactions.
        self.approve_button.custom_id = f"bonus:approve:{submission_id}"
        self.reject_button.custom_id = f"bonus:reject:{submission_id}"

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    @discord.ui.button(
        label="Approve",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="bonus:approve:placeholder",  # overwritten in __init__
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_decision(interaction, approved=True)

    @discord.ui.button(
        label="Reject",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="bonus:reject:placeholder",  # overwritten in __init__
    )
    async def reject_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_decision(interaction, approved=False)

    # ------------------------------------------------------------------
    # Shared decision handler
    # ------------------------------------------------------------------
    async def _handle_decision(
        self, interaction: discord.Interaction, *, approved: bool
    ) -> None:
        # Only respond to interactions from members we can permission-check.
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This action can only be used inside a server.", ephemeral=True
            )
            return

        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to approve or reject submissions.",
                ephemeral=True,
            )
            return

        submission = self.bot.get_submission(self.submission_id)
        if submission is None:
            await interaction.response.send_message(
                "This submission could no longer be found.", ephemeral=True
            )
            return

        if submission.get("status") != "pending":
            await interaction.response.send_message(
                f"This submission has already been {submission.get('status', 'handled')}.",
                ephemeral=True,
            )
            return

        admin_name = interaction.user.display_name

        # 1. Disable the buttons on the admin embed (local state).
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        status_label = "Approved" if approved else "Rejected"
        status_emoji = "✅" if approved else "❌"

        # 2. Acknowledge the interaction immediately so slow operations
        #    (reaction updates, etc.) don't hit the 3s limit.
        await interaction.response.defer()

        try:
            # 3. Update the original submission message reaction.
            await self._update_submission_reaction(submission, approved)

            # 4. Execute side-effects.
            if approved:
                # Calculate the bonus for each participant and persist it.
                event_type = submission.get("event_type", "")
                meta = submission.get("meta", {})
                num_participants = len(submission.get("participants", []))
                unit = get_event_unit(event_type)
                for p_dict in submission.get("participants", []):
                    kills = int(p_dict.get("kills", 0))
                    p_dict["bonus"] = calculate_bonus(
                        event_type, kills, num_participants, meta
                    )
                    p_dict["unit"] = unit
                await self.bot.process_approval(submission, interaction.user)
            else:
                await self.bot.process_rejection(submission)

            # 5. Persist the new status.
            submission["status"] = "approved" if approved else "rejected"
            submission["handled_by"] = str(interaction.user.id)
            self.bot.save_submissions()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Side-effect failed for submission %d: %s", self.submission_id, exc
            )
            await interaction.followup.send(
                f"⚠️ The {'approval' if approved else 'rejection'} was recorded, but "
                f"a follow-up step failed: {exc}",
                ephemeral=True,
            )
            # Still mark the submission so the buttons stay disabled.
            submission["status"] = "approved" if approved else "rejected"
            submission["handled_by"] = str(interaction.user.id)
            self.bot.save_submissions()

        # 6. Edit the admin message to reflect the decision (disables buttons).
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed is not None:
            embed.set_footer(
                text=f"{status_emoji} {status_label} by {admin_name}",
                icon_url=interaction.user.display_avatar.url,
            )
            embed.color = (
                discord.Color.green() if approved else discord.Color.red()
            )
            # On approval, refresh the participants field to include each
            # player's calculated bonus.
            if approved:
                participants = [
                    Participant.from_dict(p) for p in submission.get("participants", [])
                ]
                cfg = get_event_config(submission.get("event_type", ""))
                kill_based_modes = {"per_kill", "state_control"}
                show_kills = cfg["bonus_mode"] in kill_based_modes if cfg else True
                try:
                    embed.set_field_at(
                        index=2,
                        name="Participants & Bonus",
                        value=build_participant_display(
                            participants, show_bonus=True, show_kills=show_kills
                        ),
                        inline=False,
                    )
                except (IndexError, discord.HTTPException):
                    pass

        await interaction.edit_original_response(view=self, embed=embed)

        # 7. Send a confirmation to the admin (ephemeral follow-up).
        await interaction.followup.send(
            f"Submission **{status_label.lower()}** successfully.", ephemeral=True
        )

        logger.info(
            "Submission %d %s by %s (%d)",
            self.submission_id,
            status_label.lower(),
            admin_name,
            interaction.user.id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _update_submission_reaction(
        self, submission: dict, approved: bool
    ) -> None:
        """Replace the ⏳ reaction on the original message with ✅ or ❌."""
        try:
            channel = self.bot.get_channel(int(submission["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(submission["channel_id"]))

            message = await channel.fetch_message(self.submission_id)

            # Remove the pending reaction (added by the bot).
            try:
                await message.remove_reaction("⏳", self.bot.user)
            except discord.NotFound:
                pass  # reaction already gone

            # Add the outcome reaction.
            await message.add_reaction("✅" if approved else "❌")
        except Exception as exc:  # noqa: BLE001 - we want to log, not crash
            logger.warning(
                "Could not update reaction on submission %d: %s",
                self.submission_id,
                exc,
            )
