"""Create or reset a web-app login user.

Usage:
    python -m scripts.create_admin <login> <password>
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app import database
from app.logging_config import configure_logging
from app.security import hash_password

logger = logging.getLogger(__name__)


async def _run(login: str, password: str) -> None:
    await database.connect()
    try:
        result = await database.users().update_one(
            {"login": login},
            {"$set": {"login": login, "password": hash_password(password)}},
            upsert=True,
        )
        if result.upserted_id:
            logger.info("Created user %r", login)
        else:
            logger.info("Updated password for user %r", login)
    finally:
        await database.disconnect()


def main() -> None:
    configure_logging()
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
