"""Slash commands and the background reminder loop."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .agenda import build_embed, is_pending
from .config import Config
from .db import Database, Reminder
from .notifier import collect_due, mark_sent
from .scheduling import (
    Day,
    ScheduleError,
    format_clock,
    format_local,
    moment_for,
    planning_date,
    planning_window,
    resolve,
)
from .timeparse import TimeParseError, parse_time
from .views import AgendaView, reminder_view

if TYPE_CHECKING:
    from .client import PlannerBot

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Planner(commands.Cog):
    def __init__(self, bot: "PlannerBot", db: Database, config: Config) -> None:
        self.bot = bot
        self.db = db
        self.config = config
        self._owner: discord.User | None = None

    # --- lifecycle ------------------------------------------------------------

    async def cog_load(self) -> None:
        self.reminder_loop.change_interval(seconds=self.config.poll_seconds)
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # --- helpers --------------------------------------------------------------

    async def _deny_non_owner(self, interaction: discord.Interaction) -> bool:
        """Reply and return True if someone other than the owner used a command."""
        if interaction.user.id == self.config.owner_id:
            return False
        await interaction.response.send_message("This bot only takes commands from its owner.", ephemeral=True)
        return True

    async def _get_owner(self) -> discord.User:
        if self._owner is None:
            self._owner = self.bot.get_user(self.config.owner_id) or await self.bot.fetch_user(
                self.config.owner_id
            )
        return self._owner

    # --- agenda ----------------------------------------------------------------

    def _plan_day(self, now: datetime) -> date:
        return planning_date(now, self.config.tz, self.config.day_cutoff_hour)

    def _day_reminders(self, plan_day: date) -> list[Reminder]:
        start, end = planning_window(plan_day, self.config.tz, self.config.day_cutoff_hour)
        return self.db.for_day(self.config.owner_id, start, end)

    def render_agenda(
        self, *, now: datetime | None = None, plan_day: date | None = None
    ) -> tuple[discord.Embed, AgendaView]:
        """Build the agenda embed and its controls from the current database state."""
        now = now or _utcnow()
        plan_day = plan_day or self._plan_day(now)
        reminders = self._day_reminders(plan_day)
        pending = [r for r in reminders if is_pending(r, now)]
        embed = build_embed(reminders, plan_day, now, self.config.tz)
        return embed, AgendaView(pending, self.config.tz)

    async def refresh_agenda(self, plan_day: date | None = None) -> None:
        """Re-render today's posted agenda message, if there is one."""
        now = _utcnow()
        plan_day = plan_day or self._plan_day(now)
        record = self.db.get_agenda(plan_day)
        if record is None:
            return
        embed, view = self.render_agenda(now=now, plan_day=plan_day)
        try:
            channel = self.bot.get_channel(record.channel_id) or await self.bot.fetch_channel(
                record.channel_id
            )
            message = await channel.fetch_message(record.message_id)
            await message.edit(embed=embed, view=view)
        except discord.NotFound:
            # Message or channel is gone: forget it so a new agenda can be posted.
            log.warning("Agenda message for %s is gone; clearing it.", plan_day)
            self.db.clear_agenda(plan_day)
        except discord.HTTPException:
            log.exception("Could not update the agenda message for %s", plan_day)

    async def post_agenda(self, plan_day: date, *, now: datetime | None = None) -> bool:
        """Post a fresh agenda message for ``plan_day``. Returns True if it went out."""
        if not self.config.agenda_channel_id:
            return False
        now = now or _utcnow()
        embed, view = self.render_agenda(now=now, plan_day=plan_day)
        try:
            channel = self.bot.get_channel(
                self.config.agenda_channel_id
            ) or await self.bot.fetch_channel(self.config.agenda_channel_id)
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.error(
                "No permission to post in the agenda channel (%s). "
                "Give the bot View Channel + Send Messages there.",
                self.config.agenda_channel_id,
            )
            return False
        except discord.HTTPException:
            log.exception("Could not post the agenda for %s", plan_day)
            return False
        self.db.set_agenda(plan_day, message.channel.id, message.id, now)
        log.info("Posted agenda for %s", plan_day)
        return True

    async def _maybe_post_agenda(self, now: datetime) -> None:
        """Post the day's agenda once its scheduled time has arrived."""
        if not self.config.agenda_channel_id:
            return
        plan_day = self._plan_day(now)
        if self.db.get_agenda(plan_day) is not None:
            return
        due_at = moment_for(
            plan_day,
            dtime(self.config.agenda_hour, self.config.agenda_minute),
            self.config.tz,
            self.config.day_cutoff_hour,
        )
        if now >= due_at:
            await self.post_agenda(plan_day, now=now)

    def _describe(self, r: Reminder) -> str:
        tz = self.config.tz
        warn = f", warn {r.notif_minutes}m" if r.notif_minutes else ""
        return f"`#{r.id}` {format_local(r.event_at, tz)} — **{r.task}**{warn}"

    async def _add(
        self,
        interaction: discord.Interaction,
        day: Day,
        time: str,
        task: str,
        notif: int,
        desc: str | None,
    ) -> None:
        if await self._deny_non_owner(interaction):
            return

        now = _utcnow()
        try:
            clock = parse_time(time)
            resolved = resolve(
                day, clock, notif, now=now, tz=self.config.tz, cutoff_hour=self.config.day_cutoff_hour
            )
        except (TimeParseError, ScheduleError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        r = self.db.add(
            user_id=interaction.user.id,
            task=task.strip(),
            description=desc.strip() if desc else None,
            event_at=resolved.event_at,
            notif_minutes=notif,
            warning_at=resolved.warning_at,
            warning_skipped=resolved.warning_skipped,
            now=now,
        )

        tz = self.config.tz
        lines = [
            f"✅ `#{r.id}` **{r.task}** — {format_local(r.event_at, tz)} (<t:{int(r.event_at.timestamp())}:R>)"
        ]
        if resolved.warning_at is not None:
            lines.append(f"Warning: {notif} min before ({format_clock(resolved.warning_at, tz)})")
        elif resolved.warning_skipped:
            lines.append(f"Warning skipped — it's less than {notif} min away.")
        if r.description:
            lines.append(f"> {r.description}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        log.info("Added reminder #%s at %s", r.id, r.event_at.isoformat())
        await self.refresh_agenda()

    # --- commands -------------------------------------------------------------

    @app_commands.command(name="today", description="Add a reminder for today.")
    @app_commands.describe(
        time='When, e.g. "3pm", "3:30pm", "15:00", "noon"',
        task="What to do",
        notif="Minutes of warning before it starts (0 = no warning)",
        desc="Optional details",
    )
    async def today(
        self,
        interaction: discord.Interaction,
        time: str,
        task: app_commands.Range[str, 1, 200],
        notif: app_commands.Range[int, 0, 1440] = 0,
        desc: Optional[app_commands.Range[str, 1, 500]] = None,
    ) -> None:
        await self._add(interaction, Day.TODAY, time, task, notif, desc)

    @app_commands.command(name="tmrw", description="Add a reminder for tomorrow.")
    @app_commands.describe(
        time='When, e.g. "3pm", "3:30pm", "15:00", "noon"',
        task="What to do",
        notif="Minutes of warning before it starts (0 = no warning)",
        desc="Optional details",
    )
    async def tmrw(
        self,
        interaction: discord.Interaction,
        time: str,
        task: app_commands.Range[str, 1, 200],
        notif: app_commands.Range[int, 0, 1440] = 0,
        desc: Optional[app_commands.Range[str, 1, 500]] = None,
    ) -> None:
        await self._add(interaction, Day.TOMORROW, time, task, notif, desc)

    @app_commands.command(name="list", description="Show upcoming reminders.")
    @app_commands.describe(day="Which reminders to show (default: all upcoming)")
    @app_commands.choices(
        day=[
            app_commands.Choice(name="All upcoming", value="all"),
            app_commands.Choice(name="Today", value="today"),
            app_commands.Choice(name="Tomorrow", value="tomorrow"),
        ]
    )
    async def list_(
        self,
        interaction: discord.Interaction,
        day: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if await self._deny_non_owner(interaction):
            return

        scope = day.value if day else "all"
        tz, cutoff = self.config.tz, self.config.day_cutoff_hour
        now = _utcnow()

        if scope == "all":
            reminders = self.db.upcoming(interaction.user.id)
            title = "Upcoming reminders"
            empty = "No upcoming reminders."
        else:
            plan_day = planning_date(now, tz, cutoff)
            if scope == "tomorrow":
                plan_day += timedelta(days=1)
            start, end = planning_window(plan_day, tz, cutoff)
            reminders = self.db.upcoming(interaction.user.id, start=start, end=end)
            label = f"{plan_day:%a} {plan_day.month}/{plan_day.day}"
            title = f"Reminders for {scope} ({label})"
            empty = f"No reminders for {scope} ({label})."

        if not reminders:
            await interaction.response.send_message(empty, ephemeral=True)
            return

        lines = [f"**{title}**"]
        for i, r in enumerate(reminders):
            line = self._describe(r)
            remaining = len(reminders) - i
            footer = f"\n…and {remaining} more"
            if sum(len(x) + 1 for x in lines) + len(line) + len(footer) > DISCORD_MESSAGE_LIMIT:
                lines.append(footer.strip())
                break
            lines.append(line)

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel an upcoming reminder.")
    @app_commands.describe(reminder="Start typing to pick a reminder, or enter its # number")
    async def cancel(self, interaction: discord.Interaction, reminder: int) -> None:
        if await self._deny_non_owner(interaction):
            return

        r = self.db.cancel(reminder, interaction.user.id)
        if r is None:
            await interaction.response.send_message(
                f"❌ No active reminder `#{reminder}`. Use /list to see IDs.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🗑️ Cancelled `#{r.id}` **{r.task}** ({format_local(r.event_at, self.config.tz)}).",
            ephemeral=True,
        )
        await self.refresh_agenda()

    @app_commands.command(name="agenda", description="Post today's agenda checklist now.")
    async def agenda(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return
        now = _utcnow()
        plan_day = self._plan_day(now)

        if not self.config.agenda_channel_id:
            # No channel configured: show it privately instead of failing.
            embed, view = self.render_agenda(now=now, plan_day=plan_day)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        self.db.clear_agenda(plan_day)  # replace today's message with a fresh one
        posted = await self.post_agenda(plan_day, now=now)
        await interaction.followup.send(
            f"📋 Posted today's agenda in <#{self.config.agenda_channel_id}>."
            if posted
            else "❌ Couldn't post the agenda — check the bot's permissions in that channel.",
            ephemeral=True,
        )

    @cancel.autocomplete("reminder")
    async def _cancel_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if interaction.user.id != self.config.owner_id:
            return []
        needle = current.strip().lstrip("#").lower()
        choices: list[app_commands.Choice[int]] = []
        for r in self.db.upcoming(interaction.user.id):
            if needle and needle not in r.task.lower() and needle not in str(r.id):
                continue
            label = f"#{r.id} {format_local(r.event_at, self.config.tz)} — {r.task}"
            choices.append(app_commands.Choice(name=label[:100], value=r.id))
            if len(choices) == 25:  # Discord's maximum
                break
        return choices

    # --- background loop ------------------------------------------------------

    @tasks.loop(seconds=30)  # real interval is set from config in cog_load
    async def reminder_loop(self) -> None:
        # An unhandled exception would stop the loop for good, so catch everything.
        try:
            now = _utcnow()
            await self._maybe_post_agenda(now)

            due = collect_due(self.db, now, self.config.tz, self.config.missed_max_hours)
            if not due:
                return
            owner = await self._get_owner()
            sent_any = False
            for item in due:
                try:
                    await owner.send(item.text, view=reminder_view(item.reminder.id))
                except discord.Forbidden:
                    log.error(
                        "Discord refused the DM. Allow DMs from server members "
                        "(right-click the server -> Privacy Settings). Will retry next tick."
                    )
                    return
                except discord.HTTPException:
                    log.exception("Failed to send reminder #%s; will retry next tick.", item.reminder.id)
                    continue
                mark_sent(self.db, item, now)
                sent_any = True
                log.info("Sent %s for reminder #%s", item.kind, item.reminder.id)
            if sent_any:
                await self.refresh_agenda()
        except Exception:
            log.exception("Reminder loop tick failed")

    @reminder_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()
