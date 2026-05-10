"""One-time migration: stamp owner='admin' on all existing documents.

Run once after deploying the multi-tenancy changes:
    python migrate_add_owner.py

Safe to re-run — uses $set so already-stamped docs are not duplicated.
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OWNER = "admin"
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "discord_manager")

# Collections that need owner — messages are shared, mails/users are global
COLLECTIONS = [
    "discords",
    "proxies",
    "topics",
    "chat_channels",
    "templates",
    "scheduled_messages",
    "voice_channels",
]


async def migrate() -> None:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB]

    for coll_name in COLLECTIONS:
        coll = db[coll_name]
        result = await coll.update_many(
            {"owner": {"$exists": False}},
            {"$set": {"owner": OWNER}},
        )
        logger.info(
            "%-25s  matched=%d  modified=%d",
            coll_name, result.matched_count, result.modified_count,
        )

    client.close()
    logger.info("Migration complete — all existing docs stamped with owner=%r", OWNER)


if __name__ == "__main__":
    asyncio.run(migrate())
