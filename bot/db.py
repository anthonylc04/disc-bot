"""SQLite storage for reminders.

All timestamps are stored as integer Unix epoch seconds (UTC). Comparisons are then plain
integer comparisons, with no timezone or string-format pitfalls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    task             TEXT    NOT NULL,
    description      TEXT,
    event_at         INTEGER NOT NULL,           -- when the event happens
    notif_minutes    INTEGER NOT NULL DEFAULT 0, -- minutes of warning requested
    warning_at       INTEGER,                    -- when to send the warning (NULL = none)
    warning_sent_at  INTEGER,                    -- set once the warning is sent or skipped
    event_sent_at    INTEGER,                    -- set once the at-time message is sent
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'done', 'cancelled', 'missed')),
    created_at       INTEGER NOT NULL,
    completed_at     INTEGER                     -- set when you check it off yourself
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, event_at);

-- One posted agenda message per planning day, so it can be edited in place.
CREATE TABLE IF NOT EXISTS agendas (
    plan_day    TEXT    PRIMARY KEY,   -- ISO date of the planning day, e.g. '2026-09-19'
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
"""

_MIGRATIONS = {
    2: [
        "ALTER TABLE reminders ADD COLUMN completed_at INTEGER",
        """
        CREATE TABLE IF NOT EXISTS agendas (
            plan_day    TEXT    PRIMARY KEY,
            channel_id  INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            created_at  INTEGER NOT NULL
        )
        """,
    ],
}


def to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return int(dt.timestamp())


def from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    task: str
    description: str | None
    event_at: datetime
    notif_minutes: int
    warning_at: datetime | None
    warning_sent_at: datetime | None
    event_sent_at: datetime | None
    status: str
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Reminder":
        opt = lambda v: from_epoch(v) if v is not None else None  # noqa: E731
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            task=row["task"],
            description=row["description"],
            event_at=from_epoch(row["event_at"]),
            notif_minutes=row["notif_minutes"],
            warning_at=opt(row["warning_at"]),
            warning_sent_at=opt(row["warning_sent_at"]),
            event_sent_at=opt(row["event_sent_at"]),
            status=row["status"],
            created_at=from_epoch(row["created_at"]),
            completed_at=opt(row["completed_at"]),
        )


@dataclass(frozen=True)
class AgendaRecord:
    """The agenda message posted for one planning day, so it can be edited later."""

    plan_day: date
    channel_id: int
    message_id: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgendaRecord":
        return cls(
            plan_day=date.fromisoformat(row["plan_day"]),
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            created_at=from_epoch(row["created_at"]),
        )


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}). Update the bot."
            )
        if version == 0:
            with self._conn:
                self._conn.executescript(_SCHEMA)
        else:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                with self._conn:
                    for statement in _MIGRATIONS[target]:
                        self._conn.execute(statement)
        if version != SCHEMA_VERSION:
            with self._conn:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    # --- writes -----------------------------------------------------------------

    def add(
        self,
        *,
        user_id: int,
        task: str,
        description: str | None,
        event_at: datetime,
        notif_minutes: int,
        warning_at: datetime | None,
        warning_skipped: bool,
        now: datetime,
    ) -> Reminder:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO reminders
                    (user_id, task, description, event_at, notif_minutes,
                     warning_at, warning_sent_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    task,
                    description,
                    to_epoch(event_at),
                    notif_minutes,
                    to_epoch(warning_at) if warning_at else None,
                    to_epoch(now) if warning_skipped else None,
                    to_epoch(now),
                ),
            )
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    def mark_warning_sent(self, reminder_id: int, now: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET warning_sent_at = ? WHERE id = ? AND warning_sent_at IS NULL",
                (to_epoch(now), reminder_id),
            )

    def mark_event_sent(self, reminder_id: int, now: datetime, *, status: str = "done") -> None:
        """Close out a reminder. Also marks any unsent warning so it never fires afterwards."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE reminders
                SET event_sent_at   = ?,
                    warning_sent_at = COALESCE(warning_sent_at, ?),
                    status          = ?
                WHERE id = ? AND status = 'active'
                """,
                (to_epoch(now), to_epoch(now), status, reminder_id),
            )

    def complete(self, reminder_id: int, user_id: int, now: datetime) -> Reminder | None:
        """Check a reminder off. It stops firing, and the agenda shows it as done.

        Returns the reminder, or None if it isn't yours or was already closed out.
        """
        ts = to_epoch(now)
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE reminders
                SET status          = 'done',
                    completed_at    = ?,
                    warning_sent_at = COALESCE(warning_sent_at, ?),
                    event_sent_at   = COALESCE(event_sent_at, ?)
                WHERE id = ? AND user_id = ?
                  AND completed_at IS NULL
                  AND status IN ('active', 'done', 'missed')  -- a reminder that already
                                                              -- fired can still be ticked off
                """,
                (ts, ts, ts, reminder_id, user_id),
            )
        return self.get(reminder_id) if cur.rowcount else None

    def snooze(self, reminder_id: int, user_id: int, minutes: int, now: datetime) -> Reminder | None:
        """Push an active reminder out by ``minutes`` from now. No new warning is scheduled."""
        new_event = to_epoch(now + timedelta(minutes=minutes))
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE reminders
                SET event_at        = ?,
                    event_sent_at   = NULL,
                    warning_at      = NULL,
                    warning_sent_at = COALESCE(warning_sent_at, ?)
                WHERE id = ? AND user_id = ? AND status IN ('active', 'done') AND completed_at IS NULL
                """,
                (new_event, to_epoch(now), reminder_id, user_id),
            )
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE reminders SET status = 'active' WHERE id = ?", (reminder_id,)
                )
        return self.get(reminder_id) if cur.rowcount else None

    def cancel(self, reminder_id: int, user_id: int) -> Reminder | None:
        """Cancel an active reminder owned by ``user_id``. Returns it, or None if not found/active."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
                (reminder_id, user_id),
            )
        return self.get(reminder_id) if cur.rowcount else None

    # --- reads ------------------------------------------------------------------

    def get(self, reminder_id: int) -> Reminder | None:
        row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return Reminder.from_row(row) if row else None

    def upcoming(
        self, user_id: int, *, start: datetime | None = None, end: datetime | None = None
    ) -> list[Reminder]:
        """Active reminders for a user, ordered by event time, optionally within [start, end)."""
        sql = "SELECT * FROM reminders WHERE user_id = ? AND status = 'active'"
        params: list[object] = [user_id]
        if start is not None:
            sql += " AND event_at >= ?"
            params.append(to_epoch(start))
        if end is not None:
            sql += " AND event_at < ?"
            params.append(to_epoch(end))
        sql += " ORDER BY event_at, id"
        return [Reminder.from_row(r) for r in self._conn.execute(sql, params)]

    def due_warnings(self, now: datetime) -> list[Reminder]:
        rows = self._conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'active'
              AND warning_at IS NOT NULL
              AND warning_sent_at IS NULL
              AND warning_at <= ?
            ORDER BY warning_at, id
            """,
            (to_epoch(now),),
        )
        return [Reminder.from_row(r) for r in rows]

    def due_events(self, now: datetime) -> list[Reminder]:
        rows = self._conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'active'
              AND event_sent_at IS NULL
              AND event_at <= ?
            ORDER BY event_at, id
            """,
            (to_epoch(now),),
        )
        return [Reminder.from_row(r) for r in rows]

    # --- day view and agenda bookkeeping -----------------------------------------

    def for_day(self, user_id: int, start: datetime, end: datetime) -> list[Reminder]:
        """Everything scheduled in [start, end) except cancellations, for the agenda list."""
        rows = self._conn.execute(
            """
            SELECT * FROM reminders
            WHERE user_id = ? AND status != 'cancelled' AND event_at >= ? AND event_at < ?
            ORDER BY event_at, id
            """,
            (user_id, to_epoch(start), to_epoch(end)),
        )
        return [Reminder.from_row(r) for r in rows]

    def get_agenda(self, plan_day: date) -> AgendaRecord | None:
        row = self._conn.execute(
            "SELECT * FROM agendas WHERE plan_day = ?", (plan_day.isoformat(),)
        ).fetchone()
        return AgendaRecord.from_row(row) if row else None

    def set_agenda(self, plan_day: date, channel_id: int, message_id: int, now: datetime) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO agendas (plan_day, channel_id, message_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_day) DO UPDATE SET channel_id = excluded.channel_id,
                                                    message_id = excluded.message_id,
                                                    created_at = excluded.created_at
                """,
                (plan_day.isoformat(), channel_id, message_id, to_epoch(now)),
            )

    def clear_agenda(self, plan_day: date) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM agendas WHERE plan_day = ?", (plan_day.isoformat(),))
