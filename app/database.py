"""MongoDB connection lifecycle and collection accessors."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)


class _MongoState:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


state = _MongoState()


async def connect() -> None:
    settings = get_settings()
    state.client = AsyncIOMotorClient(settings.mongo_uri)
    state.db = state.client[settings.mongo_db]
    await state.client.admin.command("ping")
    logger.info("Connected to MongoDB at %s/%s", settings.mongo_uri, settings.mongo_db)
    await _ensure_indexes()


async def disconnect() -> None:
    if state.client is not None:
        state.client.close()
        state.client = None
        state.db = None
        logger.info("MongoDB connection closed")


def db() -> AsyncIOMotorDatabase:
    if state.db is None:
        raise RuntimeError("MongoDB is not connected — call connect() first")
    return state.db


# Collection accessors — keep names central so we never typo elsewhere.
def mails() -> "AsyncIOMotorCollection":
    return db()["mails"]


def discords() -> "AsyncIOMotorCollection":
    return db()["discords"]


def proxies() -> "AsyncIOMotorCollection":
    return db()["proxies"]


def messages() -> "AsyncIOMotorCollection":
    return db()["messages"]


def private_messages() -> "AsyncIOMotorCollection":
    return db()["private_messages"]


def users() -> "AsyncIOMotorCollection":
    return db()["users"]


def templates() -> "AsyncIOMotorCollection":
    return db()["templates"]


def topics() -> "AsyncIOMotorCollection":
    return db()["topics"]


def voice_channels() -> "AsyncIOMotorCollection":
    return db()["voice_channels"]


def chat_channels() -> "AsyncIOMotorCollection":
    return db()["chat_channels"]


async def _ensure_indexes() -> None:
    await mails().create_index("email", unique=True)
    await discords().create_index("email", unique=True)
    await proxies().create_index([("ip", 1), ("port", 1)], unique=True)
    await users().create_index("login", unique=True)
    await messages().create_index([("topic", 1), ("timestamp", -1)])
    await messages().create_index("discord_message_id", sparse=True)
    await private_messages().create_index([("to", 1), ("is_read", 1)])
    await private_messages().create_index(
        [("to", 1), ("discord_message_id", 1)], unique=True, sparse=True
    )
    await topics().create_index("channel_id", unique=True)
    logger.debug("MongoDB indexes ensured")
