"""Entry point: `python -m bot`."""

from __future__ import annotations

import logging
import sys

import discord

from .client import PlannerBot
from .config import ConfigError, load_config


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    discord.utils.setup_logging(level=logging.INFO)
    bot = PlannerBot(config)
    bot.run(config.token, log_handler=None)  # logging already configured above


if __name__ == "__main__":
    main()
