"""Load and validate settings from environment variables / `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


# Repo root (the folder containing `bot/`). Relative paths resolve against it, so the bot
# behaves the same no matter which directory it's launched from (e.g. a startup task).
ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    owner_id: int
    tz: ZoneInfo
    day_cutoff_hour: int
    db_path: Path
    poll_seconds: int
    missed_max_hours: int
    agenda_channel_id: int      # 0 disables the daily agenda
    agenda_hour: int
    agenda_minute: int
    snooze_minutes: int


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _int(name: str, default: int | None = None, *, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is not set.")
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if lo is not None and value < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {value}.")
    if hi is not None and value > hi:
        raise ConfigError(f"{name} must be <= {hi}, got {value}.")
    return value


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    tz_name = os.getenv("TIMEZONE", "America/New_York").strip()
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"Unknown TIMEZONE {tz_name!r}. On Windows, make sure the 'tzdata' package is installed."
        ) from exc

    return Config(
        token=_require("DISCORD_TOKEN"),
        guild_id=_int("GUILD_ID"),
        owner_id=_int("OWNER_ID"),
        tz=tz,
        day_cutoff_hour=_int("DAY_CUTOFF_HOUR", 4, lo=0, hi=23),
        db_path=_resolve_path(os.getenv("DB_PATH", "data/reminders.db").strip()),
        poll_seconds=_int("POLL_SECONDS", 30, lo=5, hi=300),
        missed_max_hours=_int("MISSED_MAX_HOURS", 12, lo=0),
        agenda_channel_id=_int("AGENDA_CHANNEL_ID", 0, lo=0),
        agenda_hour=_int("AGENDA_HOUR", 10, lo=0, hi=23),
        agenda_minute=_int("AGENDA_MINUTE", 0, lo=0, hi=59),
        snooze_minutes=_int("SNOOZE_MINUTES", 10, lo=1, hi=1440),
    )
