"""Decide which notifications are due and what they say.

Kept separate from Discord so the rules (late sends, skipped warnings, missed reminders)
can be unit-tested with a plain database and a fake clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .db import Database, Reminder
from .scheduling import format_clock

# A notification sent more than this long after it was due is labeled as late.
LATE_GRACE = timedelta(minutes=2)

Kind = Literal["warning", "event"]


@dataclass(frozen=True)
class Outgoing:
    reminder: Reminder
    kind: Kind
    text: str


def _with_desc(text: str, r: Reminder) -> str:
    return f"{text}\n> {r.description}" if r.description else text


def warning_text(r: Reminder, now: datetime, tz: ZoneInfo) -> str:
    minutes_left = max(1, math.ceil((r.event_at - now).total_seconds() / 60))
    return _with_desc(
        f"⏰ **{r.task}** starts in {minutes_left} min (at {format_clock(r.event_at, tz)}).", r
    )


def event_text(r: Reminder, now: datetime, tz: ZoneInfo) -> str:
    if now - r.event_at > LATE_GRACE:
        text = f"🔔 **{r.task}** was at {format_clock(r.event_at, tz)} — sent late (bot was offline)."
    else:
        text = f"🔔 Time for **{r.task}**!"
    return _with_desc(text, r)


def collect_due(db: Database, now: datetime, tz: ZoneInfo, missed_max_hours: int) -> list[Outgoing]:
    """Return messages to send now. Silently resolves warnings/events that should not be sent.

    Rules:
    - A warning whose event has already started is skipped; the event message covers it.
    - An event more than ``missed_max_hours`` overdue is marked 'missed' without a message,
      so a long outage doesn't flood your DMs.
    - Everything else due is returned; the caller marks each one sent after delivery succeeds.
    """
    out: list[Outgoing] = []
    missed_cutoff = timedelta(hours=missed_max_hours)

    for r in db.due_warnings(now):
        if r.event_at <= now:
            db.mark_warning_sent(r.id, now)
            continue
        out.append(Outgoing(r, "warning", warning_text(r, now, tz)))

    for r in db.due_events(now):
        if now - r.event_at > missed_cutoff:
            db.mark_event_sent(r.id, now, status="missed")
            continue
        out.append(Outgoing(r, "event", event_text(r, now, tz)))

    return out


def mark_sent(db: Database, item: Outgoing, now: datetime) -> None:
    if item.kind == "warning":
        db.mark_warning_sent(item.reminder.id, now)
    else:
        db.mark_event_sent(item.reminder.id, now)
