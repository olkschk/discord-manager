"""CLI: create or update a panel user.

Usage:
    python create_user.py <login> <password>

Examples:
    python create_user.py admin secretpass123
    python create_user.py user2 anotherpass

If the login already exists the password is updated (upsert).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "discord_manager")


async def create_user(login: str, password: str) -> None:
    import bcrypt
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB]
    result = await db["users"].update_one(
        {"login": login},
        {"$set": {"login": login, "password": hashed}},
        upsert=True,
    )
    client.close()

    if result.upserted_id:
        logger.info("Created user %r", login)
    else:
        logger.info("Updated password for existing user %r", login)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python create_user.py <login> <password>")
        sys.exit(1)

    login_arg, password_arg = sys.argv[1], sys.argv[2]

    if len(password_arg) < 8:
        print("Error: password must be at least 8 characters")
        sys.exit(1)

    asyncio.run(create_user(login_arg, password_arg))
