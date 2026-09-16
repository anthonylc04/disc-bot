from datetime import time

import pytest

from bot.timeparse import TimeParseError, parse_time


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3pm", time(15, 0)),
        ("3 pm", time(15, 0)),
        ("3PM", time(15, 0)),
        ("3p", time(15, 0)),
        ("3p.m.", time(15, 0)),
        ("3:30pm", time(15, 30)),
        ("3:30 PM", time(15, 30)),
        ("9am", time(9, 0)),
        ("9:05 a.m.", time(9, 5)),
        ("12am", time(0, 0)),
        ("12pm", time(12, 0)),
        ("12:30am", time(0, 30)),
        ("15:00", time(15, 0)),
        ("09:00", time(9, 0)),
        ("9:00", time(9, 0)),
        ("0:30", time(0, 30)),
        ("1530", time(15, 30)),
        ("930am", time(9, 30)),
        ("0930", time(9, 30)),
        ("15", time(15, 0)),
        ("23", time(23, 0)),
        ("noon", time(12, 0)),
        ("Midnight", time(0, 0)),
        ("  4pm  ", time(16, 0)),
    ],
)
def test_parses(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["3", "12", "0", "9"])
def test_bare_small_hour_is_ambiguous(text):
    with pytest.raises(TimeParseError, match="ambiguous"):
        parse_time(text)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "25:00", "24", "3:60pm", "13pm", "0am", "3:5pm", "12345", "3 o'clock", "3:30:00"],
)
def test_rejects_garbage(text):
    with pytest.raises(TimeParseError):
        parse_time(text)
