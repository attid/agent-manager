"""Entrypoint for running the Telegram Secretary bot: python -m kuzya.telegram"""

import asyncio
import logging
import sys

from kuzya.telegram.bot import run_bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
