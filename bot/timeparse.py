"""Parse loosely formatted clock times like "3pm", "3:30 PM", "15:00", "noon".

Pure functions only — no Discord or database imports — so this is easy to test.
"""

from __future__ import annotations

import re
from datetime import time


class TimeParseError(ValueError):
    """Raised when a time string can't be understood. The message is user-facing."""


_WORDS = {
    "noon": time(12, 0),
    "midday": time(12, 0),
    "midnight": time(0, 0),
}

# Examples matched: 3pm, 3 pm, 3p, 3:30pm, 03:30 a.m., 15:00, 1530, 930am, 15
_PATTERN = re.compile(
    r"""
    ^
    (?:
        (?P<h_colon>\d{1,2}) : (?P<m_colon>\d{2})     # 3:30, 15:00
      | (?P<compact>\d{3,4})                          # 930, 1530
      | (?P<h_only>\d{1,2})                           # 3, 15
    )
    \s*
    (?P<meridiem>a\.?m?\.?|p\.?m?\.?)?                # am, a.m., a, pm, p.m., p
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

HELP = 'Try formats like "3pm", "3:30pm", "15:00", or "noon".'


def parse_time(text: str) -> time:
    """Return the wall-clock time described by ``text``.

    Rules:
    - With am/pm: 12-hour clock (1-12).
    - With a colon or 3-4 digits and no am/pm: 24-hour clock ("9:00" is 9 AM, "21:00" is 9 PM).
    - A bare hour 13-23 is unambiguous 24-hour ("15" is 3 PM).
    - A bare hour 0-12 without am/pm is rejected as ambiguous ("3" could be 3 AM or 3 PM).
    """
    raw = text
    s = text.strip().lower()
    if not s:
        raise TimeParseError(f"No time given. {HELP}")

    if s in _WORDS:
        return _WORDS[s]

    m = _PATTERN.match(s)
    if not m:
        raise TimeParseError(f"Couldn't read {raw!r} as a time. {HELP}")

    meridiem = m.group("meridiem")
    is_pm = meridiem is not None and meridiem.startswith("p")

    if m.group("h_colon") is not None:
        hour, minute = int(m.group("h_colon")), int(m.group("m_colon"))
    elif m.group("compact") is not None:
        digits = m.group("compact")
        hour, minute = int(digits[:-2]), int(digits[-2:])
    else:
        hour, minute = int(m.group("h_only")), 0
        if meridiem is None and hour <= 12:
            raise TimeParseError(
                f"{raw!r} is ambiguous — AM or PM? Use \"{hour}am\", \"{hour}pm\", or 24-hour like \"{hour:02d}:00\"."
            )

    if minute > 59:
        raise TimeParseError(f"Minutes must be 00-59 in {raw!r}.")

    if meridiem is not None:
        if not 1 <= hour <= 12:
            raise TimeParseError(f"With am/pm the hour must be 1-12 in {raw!r}.")
        hour = hour % 12 + (12 if is_pm else 0)
    elif hour > 23:
        raise TimeParseError(f"Hour must be 0-23 in {raw!r}.")

    return time(hour, minute)
