"""Rendering for the daily agenda message (the embed with the checklist)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import discord

from .db import Reminder
from .scheduling import format_clock

MAX_DESCRIPTION = 4096  # Discord's embed description limit
MAX_SELECT_OPTIONS = 25  # Discord's dropdown limit

DONE = "✅"
FIRED = "🔔"
PENDING = "⬜"
MISSED = "⬛"


def state(r: Reminder, now: datetime) -> str:
    """Which icon a reminder gets on the agenda."""
    if r.completed_at is not None:
        return DONE
    if r.status == "missed":
        return MISSED
    return FIRED if r.event_at <= now else PENDING


def is_pending(r: Reminder, now: datetime) -> bool:
    """Can it still be checked off? (Fired-but-not-done counts; you may still do it.)"""
    return r.completed_at is None and r.status in ("active", "done", "missed")


def line(r: Reminder, now: datetime, tz: ZoneInfo) -> str:
    icon = state(r, now)
    body = f"**{format_clock(r.event_at, tz)}** — {r.task}"
    if icon == DONE:
        body = f"~~{body}~~"
    extra = f" *({r.notif_minutes}m warning)*" if r.notif_minutes and icon == PENDING else ""
    text = f"{icon} `#{r.id}` {body}{extra}"
    if r.description and icon != DONE:
        text += f"\n　　*{r.description}*"
    return text


def describe(reminders: list[Reminder], now: datetime, tz: ZoneInfo) -> str:
    if not reminders:
        return "*Nothing scheduled. Add something with `/today` or `/tmrw`.*"

    lines: list[str] = []
    length = 0
    for i, r in enumerate(reminders):
        text = line(r, now, tz)
        footer = f"\n*…and {len(reminders) - i} more*"
        if length + len(text) + len(footer) + 1 > MAX_DESCRIPTION:
            lines.append(footer.strip())
            break
        lines.append(text)
        length += len(text) + 1
    return "\n".join(lines)


def summary(reminders: list[Reminder]) -> tuple[int, int]:
    done = sum(1 for r in reminders if r.completed_at is not None)
    return done, len(reminders)


def build_embed(
    reminders: list[Reminder], plan_day: date, now: datetime, tz: ZoneInfo
) -> discord.Embed:
    done, total = summary(reminders)
    all_done = total > 0 and done == total

    embed = discord.Embed(
        title=f"📋 {plan_day:%A}, {plan_day:%b} {plan_day.day}",
        description=describe(reminders, now, tz),
        colour=discord.Colour.green() if all_done else discord.Colour.blurple(),
        timestamp=now,
    )

    upcoming = [r for r in reminders if r.completed_at is None and r.event_at > now]
    if upcoming:
        nxt = upcoming[0]
        embed.add_field(
            name="Next up",
            value=f"{nxt.task} at {format_clock(nxt.event_at, tz)} (<t:{int(nxt.event_at.timestamp())}:R>)",
            inline=False,
        )

    embed.set_footer(text=f"{done}/{total} done" if total else "Nothing scheduled")
    return embed
