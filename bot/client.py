"""Bot setup: database, cog registration, and slash-command sync."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import Config
from .db import Database
from .planner import Planner

log = logging.getLogger(__name__)


class PlannerBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        # Default intents include no privileged ones; slash commands and DMs don't need them.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.config = config
        self.db: Database | None = None

    async def setup_hook(self) -> None:
        self.db = Database(self.config.db_path)
        log.info("Database ready at %s", self.config.db_path)

        # Register commands to your server only: changes show up instantly
        # (global commands can take a while to propagate).
        guild = discord.Object(id=self.config.guild_id)
        await self.add_cog(Planner(self, self.db, self.config), guilds=[guild])
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d command(s) to guild %s: %s", len(synced), guild.id, ", ".join(c.name for c in synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        await super().close()
        if self.db is not None:
            self.db.close()
