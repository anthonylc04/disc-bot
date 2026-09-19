"""The interactive parts: agenda dropdown, refresh button, Done/Snooze on reminder DMs.

Discord objects are faked, so these run offline. They cover the wiring between a click and
the database, which is where the bugs live.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import pytest

from bot.client import PlannerBot
from bot.config import Config
from bot.db import Database
from bot.planner import Planner
from bot.views import AgendaView, ReminderDoneButton, ReminderSnoozeButton, reminder_view

NY = ZoneInfo("America/New_York")
OWNER = 111
GUILD = 123


def make_config(**overrides):
    base = dict(
        token="x", guild_id=GUILD, owner_id=OWNER, tz=NY, day_cutoff_hour=4,
        db_path=Path(":memory:"), poll_seconds=30, missed_max_hours=12,
        agenda_channel_id=0, agenda_hour=10, agenda_minute=0, snooze_minutes=10,
    )
    base.update(overrides)
    return Config(**base)


class FakeResponse:
    def __init__(self):
        self.calls = []

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.calls.append(("send", content, embed, view))

    async def edit_message(self, content=None, embed=None, view=None):
        self.calls.append(("edit", content, embed, view))

    async def defer(self, ephemeral=False):
        self.calls.append(("defer", None, None, None))


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, ephemeral=False):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, bot, user_id=OWNER, message_content="🔔 Time for **Gym**!"):
        self.client = bot
        self.user = type("U", (), {"id": user_id})()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = type("M", (), {"content": message_content})()


@pytest.fixture
def planner():
    """A bot with the cog loaded and an in-memory database, without touching Discord."""
    async def build():
        bot = PlannerBot(make_config())
        bot.db = Database(":memory:")
        Planner.cog_load = lambda self: asyncio.sleep(0)  # don't start the loop
        cog = Planner(bot, bot.db, bot.config)
        await bot.add_cog(cog, guilds=[discord.Object(id=GUILD)])
        return bot, cog

    bot, cog = asyncio.run(build())
    yield bot, cog
    bot.db.close()


def run(coro):
    return asyncio.run(coro)


def add(cog, when: str, task: str, notif: int = 0, desc=None, day="today"):
    bot = cog.bot
    interaction = FakeInteraction(bot)
    command = cog.today if day == "today" else cog.tmrw
    run(command.callback(cog, interaction, when, task, notif, desc))
    return interaction


# --- agenda view ---------------------------------------------------------------------


def test_agenda_view_has_stable_custom_ids():
    view = AgendaView()
    assert [c.custom_id for c in view.children] == ["agenda:done", "agenda:refresh"]
    assert view.timeout is None  # persistent


def test_registration_view_is_accepted_when_nothing_is_pending(planner):
    bot, _ = planner
    bot.add_view(AgendaView())  # what setup_hook does; must not raise on an empty dropdown
    assert len(bot.persistent_views) == 1


def test_dropdown_lists_pending_tasks(planner):
    _, cog = planner
    add(cog, "11:58pm", "Read")
    add(cog, "11:59pm", "Gym", 10)
    _, view = cog.render_agenda()
    assert [(o.label, o.value) for o in view.children[0].options] == [
        ("11:58 PM — Read", "1"),
        ("11:59 PM — Gym", "2"),
    ]


def test_selecting_checks_off_and_updates_the_embed(planner):
    bot, cog = planner
    add(cog, "11:58pm", "Read")
    add(cog, "11:59pm", "Gym")
    _, view = cog.render_agenda()
    select = view.children[0]
    select._values = ["1"]

    interaction = FakeInteraction(bot)
    run(select.callback(interaction))

    kind, _, embed, _ = interaction.response.calls[0]
    assert kind == "edit"
    assert embed.footer.text == "1/2 done"
    assert interaction.followup.messages == ["✅ Checked off: Read"]
    assert bot.db.get(1).completed_at is not None


def test_selecting_several_at_once(planner):
    bot, cog = planner
    add(cog, "11:57pm", "Read")
    add(cog, "11:58pm", "Gym")
    _, view = cog.render_agenda()
    select = view.children[0]
    select._values = ["1", "2"]
    run(select.callback(FakeInteraction(bot)))
    assert cog.render_agenda()[0].footer.text == "2/2 done"


def test_refresh_button_rerenders(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")
    _, view = cog.render_agenda()
    interaction = FakeInteraction(bot)
    run(view.children[1].callback(interaction))
    assert interaction.response.calls[0][0] == "edit"
    assert interaction.response.calls[0][2].footer.text == "0/1 done"


def test_strangers_are_refused_and_change_nothing(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")
    _, view = cog.render_agenda()
    select = view.children[0]
    select._values = ["1"]

    interaction = FakeInteraction(bot, user_id=999)
    run(select.callback(interaction))
    assert interaction.response.calls[0][1] == "Not your planner."
    assert bot.db.get(1).completed_at is None


# --- reminder DM buttons --------------------------------------------------------------


def test_reminder_view_custom_ids_carry_the_id():
    assert [c.custom_id for c in reminder_view(7).children] == ["rem:done:7", "rem:snooze:7"]


def test_done_button_rebuilt_from_custom_id_completes(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")

    button = ReminderDoneButton(1)  # as reconstructed after a restart
    interaction = FakeInteraction(bot)
    run(button.callback(interaction))

    kind, content, _, view = interaction.response.calls[0]
    assert kind == "edit" and view is None
    assert content.startswith("~~🔔 Time for **Gym**!~~")
    assert bot.db.get(1).completed_at is not None


def test_done_button_twice_says_already_closed(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")
    run(ReminderDoneButton(1).callback(FakeInteraction(bot)))

    interaction = FakeInteraction(bot)
    run(ReminderDoneButton(1).callback(interaction))
    assert "already closed out" in interaction.response.calls[0][1]


def test_snooze_button_pushes_the_reminder(planner):
    bot, cog = planner
    add(cog, "9am", "Snoozer", day="tomorrow")
    before = bot.db.get(1).event_at

    interaction = FakeInteraction(bot)
    run(ReminderSnoozeButton(1).callback(interaction))

    after = bot.db.get(1).event_at
    assert after > datetime.now(timezone.utc)
    assert after != before
    assert "Snoozed to" in interaction.response.calls[0][1]
    assert bot.db.get(1).status == "active"


def test_snooze_after_completing_is_refused(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")
    bot.db.complete(1, OWNER, datetime.now(timezone.utc))

    interaction = FakeInteraction(bot)
    run(ReminderSnoozeButton(1).callback(interaction))
    assert interaction.followup.messages == ["That reminder is already closed out."]


# --- /agenda command -------------------------------------------------------------------


def test_agenda_command_without_a_channel_replies_privately(planner):
    bot, cog = planner
    add(cog, "11:59pm", "Gym")
    interaction = FakeInteraction(bot)
    run(cog.agenda.callback(cog, interaction))
    kind, _, embed, view = interaction.response.calls[0]
    assert kind == "send"
    assert embed.title.startswith("📋")
    assert isinstance(view, AgendaView)


def test_commands_registered(planner):
    bot, _ = planner
    names = sorted(c.name for c in bot.tree.get_commands(guild=discord.Object(id=GUILD)))
    assert names == ["agenda", "cancel", "list", "tmrw", "today"]
