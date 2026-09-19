"""Agenda rendering, check-off/snooze behaviour, and the v1 -> v2 database migration."""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.agenda import DONE, FIRED, PENDING, build_embed, describe, is_pending, state, summary
from bot.db import SCHEMA_VERSION, Database
from bot.notifier import collect_due, mark_sent
from bot.scheduling import moment_for, planning_window

NY = ZoneInfo("America/New_York")
UID = 111
CUTOFF = 4
T0 = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)  # 10 AM EDT, Sat 9/19
DAY = date(2026, 9, 19)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def add(db, *, event_in, notif=0, task="Gym", desc=None, now=T0, user_id=UID):
    event_at = now + event_in
    return db.add(
        user_id=user_id,
        task=task,
        description=desc,
        event_at=event_at,
        notif_minutes=notif,
        warning_at=event_at - timedelta(minutes=notif) if notif else None,
        warning_skipped=False,
        now=now,
    )


def day_items(db, plan_day=DAY):
    start, end = planning_window(plan_day, NY, CUTOFF)
    return db.for_day(UID, start, end)


# --- migration ------------------------------------------------------------------------

V1_SCHEMA = """
CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, task TEXT NOT NULL,
    description TEXT, event_at INTEGER NOT NULL, notif_minutes INTEGER NOT NULL DEFAULT 0,
    warning_at INTEGER, warning_sent_at INTEGER, event_sent_at INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','done','cancelled','missed')),
    created_at INTEGER NOT NULL
);
PRAGMA user_version = 1;
"""


def test_migrates_v1_database_keeping_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO reminders (user_id, task, event_at, created_at) VALUES (?,?,?,?)",
        (UID, "old task", int(T0.timestamp()) + 3600, int(T0.timestamp())),
    )
    conn.commit()
    conn.close()

    d = Database(path)
    (v,) = d._conn.execute("PRAGMA user_version").fetchone()
    assert v == SCHEMA_VERSION

    kept = d.upcoming(UID)
    assert [r.task for r in kept] == ["old task"]
    assert kept[0].completed_at is None
    assert d.get_agenda(DAY) is None  # agendas table exists and is empty
    d.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    Database(path).close()
    d = Database(path)  # reopening an up-to-date database must not re-run migrations
    (v,) = d._conn.execute("PRAGMA user_version").fetchone()
    assert v == SCHEMA_VERSION
    d.close()


# --- completing and snoozing -----------------------------------------------------------

def test_complete_stops_it_firing(db):
    r = add(db, event_in=timedelta(hours=1), notif=10)
    done = db.complete(r.id, UID, T0)
    assert done is not None and done.completed_at == T0 and done.status == "done"
    # Neither the warning nor the event should go out afterwards.
    assert collect_due(db, T0 + timedelta(hours=2), NY, 12) == []


def test_complete_rejects_other_users_and_repeats(db):
    r = add(db, event_in=timedelta(hours=1))
    assert db.complete(r.id, 999, T0) is None
    assert db.complete(r.id, UID, T0) is not None
    assert db.complete(r.id, UID, T0) is None


def test_complete_after_it_fired(db):
    r = add(db, event_in=timedelta(minutes=5))
    later = T0 + timedelta(minutes=6)
    for item in collect_due(db, later, NY, 12):
        mark_sent(db, item, later)
    done = db.complete(r.id, UID, later + timedelta(minutes=1))
    assert done is not None and done.completed_at is not None


def test_snooze_pushes_event_and_reopens(db):
    r = add(db, event_in=timedelta(minutes=5), notif=2)
    fired_at = T0 + timedelta(minutes=5)
    for item in collect_due(db, fired_at, NY, 12):
        mark_sent(db, item, fired_at)
    assert db.get(r.id).status == "done"

    snoozed = db.snooze(r.id, UID, 10, fired_at)
    assert snoozed is not None
    assert snoozed.status == "active"
    assert snoozed.event_at == fired_at + timedelta(minutes=10)
    assert snoozed.warning_at is None  # no second warning

    assert collect_due(db, fired_at + timedelta(minutes=5), NY, 12) == []
    again = collect_due(db, fired_at + timedelta(minutes=10), NY, 12)
    assert [i.kind for i in again] == ["event"]


def test_snooze_refuses_completed(db):
    r = add(db, event_in=timedelta(hours=1))
    db.complete(r.id, UID, T0)
    assert db.snooze(r.id, UID, 10, T0) is None


def test_snooze_refuses_cancelled(db):
    r = add(db, event_in=timedelta(hours=1))
    db.cancel(r.id, UID)
    assert db.snooze(r.id, UID, 10, T0) is None


# --- day query -------------------------------------------------------------------------

def test_for_day_covers_cutoff_to_cutoff_and_hides_cancelled(db):
    add(db, event_in=timedelta(hours=1), task="morning")
    db.add(  # 1 AM the next calendar day still belongs to this planning day
        user_id=UID, task="late night", description=None,
        event_at=moment_for(DAY, datetime(2026, 1, 1, 1, 0).time(), NY, CUTOFF),
        notif_minutes=0, warning_at=None, warning_skipped=False, now=T0,
    )
    tomorrow = db.add(
        user_id=UID, task="tomorrow", description=None,
        event_at=moment_for(date(2026, 9, 20), datetime(2026, 1, 1, 9, 0).time(), NY, CUTOFF),
        notif_minutes=0, warning_at=None, warning_skipped=False, now=T0,
    )
    cancelled = add(db, event_in=timedelta(hours=2), task="cancelled")
    db.cancel(cancelled.id, UID)

    tasks = [r.task for r in day_items(db)]
    assert tasks == ["morning", "late night"]
    assert tomorrow.task not in tasks


def test_for_day_keeps_completed_and_missed(db):
    r = add(db, event_in=timedelta(hours=1), task="done one")
    db.complete(r.id, UID, T0)
    assert [x.task for x in day_items(db)] == ["done one"]


# --- rendering ---------------------------------------------------------------------------

def test_state_icons(db):
    upcoming = add(db, event_in=timedelta(hours=1), task="later")
    past = add(db, event_in=timedelta(minutes=-30), task="earlier", now=T0 - timedelta(hours=1))
    finished = add(db, event_in=timedelta(hours=2), task="checked")
    db.complete(finished.id, UID, T0)

    assert state(db.get(upcoming.id), T0) == PENDING
    assert state(db.get(past.id), T0) == FIRED
    assert state(db.get(finished.id), T0) == DONE


def test_is_pending_excludes_completed_only(db):
    a = add(db, event_in=timedelta(hours=1))
    b = add(db, event_in=timedelta(hours=2))
    db.complete(b.id, UID, T0)
    assert is_pending(db.get(a.id), T0)
    assert not is_pending(db.get(b.id), T0)


def test_describe_marks_done_with_strikethrough(db):
    a = add(db, event_in=timedelta(hours=1), task="Gym", notif=15)
    add(db, event_in=timedelta(hours=2), task="Dentist", desc="bring card")
    db.complete(a.id, UID, T0)

    text = describe(day_items(db), T0, NY)
    assert "~~**11:00 AM** — Gym~~" in text
    assert "⬜ `#2` **12:00 PM** — Dentist" in text
    assert "bring card" in text
    assert "15m warning" not in text  # completed items drop the warning note


def test_describe_empty():
    assert "Nothing scheduled" in describe([], T0, NY)


def test_describe_truncates_huge_days(db):
    for i in range(200):
        add(db, event_in=timedelta(minutes=i + 1), task=f"task {i} " + "x" * 80)
    text = describe(day_items(db), T0, NY)
    assert len(text) <= 4096
    assert "more" in text


def test_summary_counts(db):
    a = add(db, event_in=timedelta(hours=1))
    add(db, event_in=timedelta(hours=2))
    db.complete(a.id, UID, T0)
    assert summary(day_items(db)) == (1, 2)


def test_embed_shape(db):
    add(db, event_in=timedelta(hours=1), task="Gym")
    done = add(db, event_in=timedelta(hours=3), task="Read")
    db.complete(done.id, UID, T0)

    embed = build_embed(day_items(db), DAY, T0, NY)
    assert embed.title == "📋 Saturday, Sep 19"
    assert embed.footer.text == "1/2 done"
    assert embed.fields[0].name == "Next up"
    assert "Gym" in embed.fields[0].value
    assert len(embed) <= 6000


def test_embed_turns_green_when_everything_is_done(db):
    r = add(db, event_in=timedelta(hours=1))
    db.complete(r.id, UID, T0)
    import discord

    assert build_embed(day_items(db), DAY, T0, NY).colour == discord.Colour.green()
    assert build_embed([], DAY, T0, NY).colour == discord.Colour.blurple()


# --- agenda records -----------------------------------------------------------------------

def test_agenda_record_roundtrip_and_replace(db):
    assert db.get_agenda(DAY) is None
    db.set_agenda(DAY, 555, 777, T0)
    rec = db.get_agenda(DAY)
    assert (rec.plan_day, rec.channel_id, rec.message_id) == (DAY, 555, 777)

    db.set_agenda(DAY, 555, 888, T0)  # reposting replaces, never duplicates
    assert db.get_agenda(DAY).message_id == 888

    db.clear_agenda(DAY)
    assert db.get_agenda(DAY) is None


def test_complete_refuses_cancelled(db):
    r = add(db, event_in=timedelta(hours=1))
    db.cancel(r.id, UID)
    assert db.complete(r.id, UID, T0) is None


def test_complete_works_on_a_missed_reminder(db):
    r = add(db, event_in=timedelta(hours=1))
    long_after = T0 + timedelta(hours=20)
    for item in collect_due(db, long_after, NY, 12):
        mark_sent(db, item, long_after)
    assert db.get(r.id).status == "missed"
    assert db.complete(r.id, UID, long_after) is not None
