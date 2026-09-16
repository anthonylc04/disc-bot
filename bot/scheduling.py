"""Turn "today/tomorrow + a clock time" into an absolute moment, honoring the day cutoff.

The day cutoff makes "today" behave the way a night owl expects. With a 4 AM cutoff:

- At 1:00 AM on Wed 9/16 you're still living in *Tuesday*. `/tmrw 3pm` means Wed 3 PM,
  and `/today 2am` means Wed 2 AM (an hour from now).
- Clock times earlier than the cutoff belong to the *end* of a planning day. So on the
  planning day Tue 9/15, "1am" means Wed 9/16 at 1 AM — the late-night end of Tuesday.

Everything here is pure (no Discord, no database) and takes ``now`` explicitly so it can be tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo


class Day(Enum):
    TODAY = "today"
    TOMORROW = "tomorrow"


class ScheduleError(ValueError):
    """Raised when a reminder can't be scheduled. The message is user-facing."""


@dataclass(frozen=True)
class Resolved:
    event_at: datetime            # aware, UTC
    warning_at: datetime | None   # aware, UTC; None when no warning applies
    warning_skipped: bool         # True if a warning was requested but its time already passed


def planning_date(now: datetime, tz: ZoneInfo, cutoff_hour: int) -> date:
    """The calendar date of the planning day that ``now`` falls in."""
    local = now.astimezone(tz)
    return (local - timedelta(hours=cutoff_hour)).date()


def resolve(
    day: Day,
    clock: time,
    notif_minutes: int,
    *,
    now: datetime,
    tz: ZoneInfo,
    cutoff_hour: int,
) -> Resolved:
    """Compute when the event and its warning should fire.

    Raises ScheduleError if the event time has already passed.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if notif_minutes < 0:
        raise ScheduleError("Warning minutes can't be negative.")

    plan_day = planning_date(now, tz, cutoff_hour)
    if day is Day.TOMORROW:
        plan_day += timedelta(days=1)

    # Times before the cutoff are the late-night tail of the planning day.
    cal_day = plan_day + timedelta(days=1) if clock.hour < cutoff_hour else plan_day

    # Round-trip through UTC so DST gaps/overlaps resolve to a real instant.
    event_local = datetime.combine(cal_day, clock, tzinfo=tz)
    event_at = event_local.astimezone(timezone.utc)

    now_utc = now.astimezone(timezone.utc)
    if event_at <= now_utc:
        hint = " Use /tmrw to schedule it for tomorrow." if day is Day.TODAY else ""
        raise ScheduleError(f"{format_local(event_at, tz)} has already passed.{hint}")

    warning_at: datetime | None = None
    warning_skipped = False
    if notif_minutes > 0:
        candidate = event_at - timedelta(minutes=notif_minutes)
        if candidate <= now_utc:
            warning_skipped = True
        else:
            warning_at = candidate

    return Resolved(event_at=event_at, warning_at=warning_at, warning_skipped=warning_skipped)


def format_local(moment: datetime, tz: ZoneInfo) -> str:
    """e.g. 'Wed 9/16 3:30 PM EDT'. Built by hand because Windows strftime lacks %-I."""
    local = moment.astimezone(tz)
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local:%a} {local.month}/{local.day} {hour12}:{local:%M} {ampm} {local.tzname()}"


def format_clock(moment: datetime, tz: ZoneInfo) -> str:
    """e.g. '3:30 PM'."""
    local = moment.astimezone(tz)
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{hour12}:{local:%M} {ampm}"


def planning_window(plan_day: date, tz: ZoneInfo, cutoff_hour: int) -> tuple[datetime, datetime]:
    """UTC [start, end) of a planning day: from its cutoff hour to the next day's cutoff hour."""
    start = datetime.combine(plan_day, time(cutoff_hour), tzinfo=tz)
    end = datetime.combine(plan_day + timedelta(days=1), time(cutoff_hour), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
