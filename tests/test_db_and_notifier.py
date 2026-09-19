from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.db import SCHEMA_VERSION, Database
from bot.notifier import collect_due, mark_sent

NY = ZoneInfo("America/New_York")
UID = 111
T0 = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)  # 10 AM EDT


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def add(db, *, event_in, notif=0, task="Gym", desc=None, now=T0, skipped=False):
    event_at = now + event_in
    warning_at = event_at - timedelta(minutes=notif) if notif and not skipped else None
    return db.add(
        user_id=UID,
        task=task,
        description=desc,
        event_at=event_at,
        notif_minutes=notif,
        warning_at=warning_at,
        warning_skipped=skipped,
        now=now,
    )


def send_all(db, now, missed_max_hours=12):
    """Simulate a loop tick where every send succeeds."""
    items = collect_due(db, now, NY, missed_max_hours)
    for item in items:
        mark_sent(db, item, now)
    return items


# --- db ------------------------------------------------------------------------------


def test_add_and_get_roundtrip(db):
    r = add(db, event_in=timedelta(hours=5), notif=10, desc="leg day")
    got = db.get(r.id)
    assert got == r
    assert got.event_at == T0 + timedelta(hours=5)
    assert got.warning_at == T0 + timedelta(hours=5, minutes=-10)
    assert got.status == "active"


def test_schema_version_set(db):
    (v,) = db._conn.execute("PRAGMA user_version").fetchone()
    assert v == SCHEMA_VERSION


def test_file_db_persists_across_reopen(tmp_path):
    path = tmp_path / "sub" / "r.db"
    d = Database(path)
    add(d, event_in=timedelta(hours=1))
    d.close()
    d2 = Database(path)
    assert len(d2.upcoming(UID)) == 1
    d2.close()


def test_upcoming_ordered_and_windowed(db):
    late = add(db, event_in=timedelta(hours=8), task="late")
    early = add(db, event_in=timedelta(hours=2), task="early")
    assert [r.task for r in db.upcoming(UID)] == ["early", "late"]
    window = db.upcoming(UID, start=T0, end=T0 + timedelta(hours=4))
    assert [r.id for r in window] == [early.id]
    assert late.id not in [r.id for r in window]


def test_cancel_only_own_active(db):
    r = add(db, event_in=timedelta(hours=1))
    assert db.cancel(r.id, user_id=999) is None
    cancelled = db.cancel(r.id, UID)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert db.cancel(r.id, UID) is None  # already cancelled
    assert db.upcoming(UID) == []


# --- notifier --------------------------------------------------------------------------


def test_nothing_due_before_time(db):
    add(db, event_in=timedelta(hours=1), notif=10)
    assert send_all(db, T0 + timedelta(minutes=30)) == []


def test_warning_then_event_each_sent_once(db):
    r = add(db, event_in=timedelta(hours=1), notif=10)

    at_warning = T0 + timedelta(minutes=50)
    items = send_all(db, at_warning)
    assert [i.kind for i in items] == ["warning"]
    assert "starts in 10 min" in items[0].text
    assert send_all(db, at_warning + timedelta(seconds=30)) == []  # not re-sent

    at_event = T0 + timedelta(hours=1)
    items = send_all(db, at_event)
    assert [i.kind for i in items] == ["event"]
    assert "Time for **Gym**" in items[0].text
    assert db.get(r.id).status == "done"
    assert send_all(db, at_event + timedelta(minutes=1)) == []


def test_notif_zero_sends_only_event(db):
    add(db, event_in=timedelta(hours=1), notif=0)
    assert [i.kind for i in send_all(db, T0 + timedelta(hours=1))] == ["event"]


def test_failed_send_is_retried_next_tick(db):
    add(db, event_in=timedelta(hours=1))
    at_event = T0 + timedelta(hours=1)
    assert len(collect_due(db, at_event, NY, 12)) == 1  # collected but NOT marked (send failed)
    assert len(collect_due(db, at_event + timedelta(seconds=30), NY, 12)) == 1


def test_offline_through_warning_and_event_sends_only_late_event(db):
    r = add(db, event_in=timedelta(hours=1), notif=10)
    back_online = T0 + timedelta(hours=1, minutes=20)
    items = send_all(db, back_online)
    assert [i.kind for i in items] == ["event"]
    assert "sent late" in items[0].text
    assert db.get(r.id).warning_sent_at is not None


def test_offline_through_warning_only_sends_warning_with_real_minutes_left(db):
    add(db, event_in=timedelta(hours=1), notif=15)
    back_online = T0 + timedelta(minutes=55)  # warning was due at :45
    items = send_all(db, back_online)
    assert [i.kind for i in items] == ["warning"]
    assert "starts in 5 min" in items[0].text


def test_long_outage_marks_missed_silently(db):
    r = add(db, event_in=timedelta(hours=1), notif=10)
    items = send_all(db, T0 + timedelta(hours=20), missed_max_hours=12)
    assert items == []
    assert db.get(r.id).status == "missed"


def test_cancelled_reminder_never_fires(db):
    r = add(db, event_in=timedelta(hours=1), notif=10)
    db.cancel(r.id, UID)
    assert send_all(db, T0 + timedelta(hours=2)) == []


def test_skipped_warning_never_fires(db):
    add(db, event_in=timedelta(minutes=5), notif=10, skipped=True)
    items = send_all(db, T0 + timedelta(minutes=5))
    assert [i.kind for i in items] == ["event"]


def test_description_included(db):
    add(db, event_in=timedelta(hours=1), desc="bring shoes")
    items = send_all(db, T0 + timedelta(hours=1))
    assert "> bring shoes" in items[0].text
