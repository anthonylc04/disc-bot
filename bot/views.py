"""Interactive components: the agenda's checklist controls and the buttons on reminder DMs.

All custom_ids are stable strings, and every view is registered at startup, so the buttons
on yesterday's messages keep working after the bot restarts.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from .agenda import MAX_SELECT_OPTIONS, is_pending
from .db import Reminder
from .scheduling import format_clock

if TYPE_CHECKING:
    from .client import PlannerBot

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _guard(interaction: discord.Interaction) -> bool:
    """True if this isn't the owner (and a refusal has been sent)."""
    bot: "PlannerBot" = interaction.client  # type: ignore[assignment]
    if interaction.user.id == bot.config.owner_id:
        return False
    await interaction.response.send_message("Not your planner.", ephemeral=True)
    return True


def _planner(interaction: discord.Interaction):
    return interaction.client.get_cog("Planner")


# --- agenda message ---------------------------------------------------------------


class DoneSelect(discord.ui.Select):
    """Dropdown of everything still open today; picking entries checks them off."""

    def __init__(self, pending: list[Reminder], tz) -> None:
        options = [
            discord.SelectOption(
                label=f"{format_clock(r.event_at, tz)} — {r.task}"[:100],
                value=str(r.id),
                description=(r.description or None) and r.description[:100],
            )
            for r in pending[:MAX_SELECT_OPTIONS]
        ] or [discord.SelectOption(label="(nothing to check off)", value="0")]

        super().__init__(
            custom_id="agenda:done",
            placeholder="Mark done…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _guard(interaction):
            return
        bot: "PlannerBot" = interaction.client  # type: ignore[assignment]
        now = _utcnow()
        names = []
        for raw in self.values:
            if raw == "0":
                continue
            r = bot.db.complete(int(raw), bot.config.owner_id, now)
            if r is not None:
                names.append(r.task)

        planner = _planner(interaction)
        embed, view = planner.render_agenda(now=now)
        await interaction.response.edit_message(embed=embed, view=view)
        if names:
            await interaction.followup.send(f"✅ Checked off: {', '.join(names)}", ephemeral=True)


class RefreshButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            custom_id="agenda:refresh", label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _guard(interaction):
            return
        embed, view = _planner(interaction).render_agenda(now=_utcnow())
        await interaction.response.edit_message(embed=embed, view=view)


class AgendaView(discord.ui.View):
    def __init__(self, pending: list[Reminder] | None = None, tz=None) -> None:
        super().__init__(timeout=None)  # persistent: never expires
        self.add_item(DoneSelect(pending or [], tz))
        self.add_item(RefreshButton())


# --- reminder DMs -----------------------------------------------------------------


class ReminderDoneButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"rem:done:(?P<id>\d+)"
):
    """Done button on a reminder DM. The id travels in the custom_id, so it survives restarts."""

    def __init__(self, reminder_id: int) -> None:
        self.reminder_id = reminder_id
        super().__init__(
            discord.ui.Button(
                label="Done",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"rem:done:{reminder_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _guard(interaction):
            return
        bot: "PlannerBot" = interaction.client  # type: ignore[assignment]
        r = bot.db.complete(self.reminder_id, bot.config.owner_id, _utcnow())
        original = interaction.message.content if interaction.message else ""
        if r is None:
            await interaction.response.edit_message(content=f"{original}\n*(already closed out)*", view=None)
        else:
            await interaction.response.edit_message(content=f"~~{original}~~\n✅ Done.", view=None)
            await _planner(interaction).refresh_agenda()


class ReminderSnoozeButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"rem:snooze:(?P<id>\d+)"
):
    def __init__(self, reminder_id: int) -> None:
        self.reminder_id = reminder_id
        super().__init__(
            discord.ui.Button(
                label="Snooze",
                emoji="💤",
                style=discord.ButtonStyle.secondary,
                custom_id=f"rem:snooze:{reminder_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _guard(interaction):
            return
        bot: "PlannerBot" = interaction.client  # type: ignore[assignment]
        minutes = bot.config.snooze_minutes
        r = bot.db.snooze(self.reminder_id, bot.config.owner_id, minutes, _utcnow())
        if r is None:
            await interaction.response.edit_message(view=None)
            await interaction.followup.send("That reminder is already closed out.", ephemeral=True)
            return
        when = format_clock(r.event_at, bot.config.tz)
        await interaction.response.edit_message(content=f"💤 Snoozed to {when}.", view=None)
        await _planner(interaction).refresh_agenda()


def reminder_view(reminder_id: int) -> discord.ui.View:
    """The Done / Snooze buttons attached to a reminder DM."""
    view = discord.ui.View(timeout=None)
    view.add_item(ReminderDoneButton(reminder_id))
    view.add_item(ReminderSnoozeButton(reminder_id))
    return view


def is_pending_for_select(r: Reminder, now: datetime) -> bool:
    return is_pending(r, now)
