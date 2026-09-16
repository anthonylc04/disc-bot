from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.scheduling import (
    Day,
    ScheduleError,
    format_local,
    planning_date,
    planning_window,
    resolve,
)

NY = ZoneInfo("America/New_York")
CUTOFF = 4


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY)


def run(day, clock, notif=0, *, now):
    return resolve(day, clock, notif, now=now, tz=NY, cutoff_hour=CUTOFF)


# --- planning day ----------------------------------------------------------------


def test_planning_date_daytime_is_calendar_date():
    assert planning_date(local(2026, 9, 15, 10), NY, CUTOFF) == date(2026, 9, 15)


def test_planning_date_after_midnight_before_cutoff_is_previous_day():
    assert planning_date(local(2026, 9, 16, 1), NY, CUTOFF) == date(2026, 9, 15)


def test_planning_date_at_cutoff_rolls_over():
    assert planning_date(local(2026, 9, 16, 4), NY, CUTOFF) == date(2026, 9, 16)


def test_planning_date_accepts_utc_now():
    # 03:00 UTC on 9/16 is 11 PM EDT on 9/15
    assert planning_date(datetime(2026, 9, 16, 3, tzinfo=timezone.utc), NY, CUTOFF) == date(2026, 9, 15)


# --- resolve ---------------------------------------------------------------------


def test_today_afternoon():
    r = run(Day.TODAY, time(15, 0), now=local(2026, 9, 15, 10))
    assert r.event_at == local(2026, 9, 15, 15)
    assert r.event_at.tzinfo == timezone.utc


def test_tmrw_afternoon():
    r = run(Day.TOMORROW, time(15, 0), now=local(2026, 9, 15, 10))
    assert r.event_at == local(2026, 9, 16, 15)


def test_tmrw_at_1am_means_the_coming_day_not_the_day_after():
    # The night-owl case the cutoff exists for.
    r = run(Day.TOMORROW, time(15, 0), now=local(2026, 9, 16, 1))
    assert r.event_at == local(2026, 9, 16, 15)


def test_today_late_night_time_maps_to_next_calendar_day():
    # At 10 PM Tuesday, "/today 1am" means tonight: Wed 1 AM.
    r = run(Day.TODAY, time(1, 0), now=local(2026, 9, 15, 22))
    assert r.event_at == local(2026, 9, 16, 1)


def test_today_2am_while_awake_at_1am():
    r = run(Day.TODAY, time(2, 0), now=local(2026, 9, 16, 1))
    assert r.event_at == local(2026, 9, 16, 2)


def test_midnight_today_is_end_of_planning_day():
    r = run(Day.TODAY, time(0, 0), now=local(2026, 9, 15, 20))
    assert r.event_at == local(2026, 9, 16, 0)


def test_today_past_time_rejected_with_tmrw_hint():
    with pytest.raises(ScheduleError, match="/tmrw"):
        run(Day.TODAY, time(9, 0), now=local(2026, 9, 15, 10))


def test_today_afternoon_after_midnight_is_past():
    # At 1 AM Wed you're still in Tuesday; Tuesday 3 PM is gone.
    with pytest.raises(ScheduleError):
        run(Day.TODAY, time(15, 0), now=local(2026, 9, 16, 1))


def test_exactly_now_is_rejected():
    with pytest.raises(ScheduleError):
        run(Day.TODAY, time(10, 0), now=local(2026, 9, 15, 10))


def test_warning_time():
    r = run(Day.TODAY, time(15, 0), 10, now=local(2026, 9, 15, 10))
    assert r.warning_at == local(2026, 9, 15, 14, 50)
    assert not r.warning_skipped


def test_no_warning_when_notif_zero():
    r = run(Day.TODAY, time(15, 0), 0, now=local(2026, 9, 15, 10))
    assert r.warning_at is None
    assert not r.warning_skipped


def test_warning_skipped_when_already_inside_window():
    r = run(Day.TODAY, time(10, 5), 10, now=local(2026, 9, 15, 10))
    assert r.warning_at is None
    assert r.warning_skipped
    assert r.event_at == local(2026, 9, 15, 10, 5)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        resolve(Day.TODAY, time(15), 0, now=datetime(2026, 9, 15, 10), tz=NY, cutoff_hour=CUTOFF)


# --- DST ---------------------------------------------------------------------------


def test_dst_fall_back_day_keeps_wall_clock():
    # 2026-11-01: clocks fall back at 2 AM. A 3 PM reminder must be 3 PM EST (20:00 UTC).
    r = run(Day.TOMORROW, time(15, 0), now=local(2026, 10, 31, 12))
    assert r.event_at == datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
    assert format_local(r.event_at, NY) == "Sun 11/1 3:00 PM EST"


def test_dst_spring_forward_day_keeps_wall_clock():
    # 2026-03-08: clocks spring forward at 2 AM. 3 PM EDT is 19:00 UTC.
    r = run(Day.TOMORROW, time(15, 0), now=local(2026, 3, 7, 12))
    assert r.event_at == datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc)


def test_dst_nonexistent_time_resolves_to_a_real_instant():
    # 2:30 AM doesn't exist on 2026-03-08; it must still produce a valid future instant.
    r = run(Day.TODAY, time(2, 30), now=local(2026, 3, 7, 23))
    assert r.event_at > datetime(2026, 3, 8, 4, tzinfo=timezone.utc)


def test_planning_window_spans_cutoff_to_cutoff():
    start, end = planning_window(date(2026, 9, 15), NY, CUTOFF)
    assert start == local(2026, 9, 15, 4)
    assert end == local(2026, 9, 16, 4)
    assert end - start == timedelta(hours=24)


# --- formatting ---------------------------------------------------------------------


def test_format_local():
    assert format_local(local(2026, 9, 16, 15, 30), NY) == "Wed 9/16 3:30 PM EDT"
    assert format_local(local(2026, 9, 16, 0, 5), NY) == "Wed 9/16 12:05 AM EDT"
    assert format_local(local(2026, 9, 16, 12, 0), NY) == "Wed 9/16 12:00 PM EDT"
