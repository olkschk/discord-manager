"""Background monitors — donor topic watcher + per-account DM watcher.

Both run as long-running asyncio tasks supervised by the FastAPI lifespan.

- TopicMonitor: every N seconds, the active donor account fetches the last 100
  messages of each registered topic and replaces the cached set in `messages`.
- DMMonitor: every M seconds, every valid account fetches its 1-on-1 DM channels
  and appends new messages to `private_messages` (deduped by Discord message id).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

from app.config import get_settings
from app.database import (
    discords,
    messages as messages_coll,
    private_messages as private_messages_coll,
    proxies as proxies_coll,
    topics,
)
from app.security import decrypt
from app.services.discord_api import _headers, build_proxy_url

logger = logging.getLogger(__name__)


def _parse_iso(ts: str | None) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


async def _account_proxy_url(acc: dict) -> str | None:
    proxy_id = acc.get("proxy_id")
    if proxy_id is None:
        return None
    proxy = await proxies_coll().find_one({"_id": proxy_id})
    if proxy is None:
        return None
    try:
        return build_proxy_url(
            proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
        )
    except ValueError:
        return None


# ── Topic monitor ────────────────────────────────────────────────────────────
async def _topic_cycle() -> None:
    settings = get_settings()
    donor = await discords().find_one({"is_donor": True, "token_valid": True})
    if donor is None:
        logger.debug("topic monitor: no active donor (need is_donor=true & token_valid=true)")
        return

    try:
        token = decrypt(donor["discord_token"])
    except ValueError:
        logger.warning("topic monitor: donor token unreadable")
        return

    proxy_url = await _account_proxy_url(donor)
    headers = _headers(token)
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    base = settings.discord_api_base

    topic_list: list[str] = []
    async for t in topics().find():
        topic_list.append(t["channel_id"])
    if not topic_list:
        return

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for tid in topic_list:
            url = f"{base}/channels/{tid}/messages?limit=100"
            try:
                async with session.get(url, headers=headers, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        logger.info("topic %s fetch status=%s", tid, resp.status)
                        continue
                    msgs: list[dict[str, Any]] = await resp.json()
            except (ClientError, TimeoutError) as exc:
                logger.warning("topic %s fetch error: %s", tid, exc)
                continue

            # Upsert each message (don't delete — Gateway listener may have saved newer ones)
            for m in msgs[:100]:
                msg_id = m.get("id")
                if not msg_id:
                    continue
                attachments = m.get("attachments") or []
                image = attachments[0].get("url") if attachments else None
                author = m.get("author") or {}
                author_id = author.get("id", "")
                avatar = author.get("avatar")
                avatar_url = (
                    f"https://cdn.discordapp.com/avatars/{author_id}/{avatar}.png?size=64"
                    if avatar
                    else f"https://cdn.discordapp.com/embed/avatars/{int(author_id or 0) % 5}.png"
                )
                ref = m.get("referenced_message")
                reply_to_author = reply_to_content = None
                if ref:
                    ra = ref.get("author") or {}
                    reply_to_author = ra.get("global_name") or ra.get("username")
                    reply_to_content = (ref.get("content") or "📎 Attachment" if ref.get("attachments") else ref.get("content") or "")[:100]

                doc = {
                    "discord_message_id": msg_id,
                    "mid": int(msg_id),
                    "text": m.get("content", ""),
                    "image": image,
                    "from": author.get("global_name") or author.get("username") or "?",
                    "author_id": author_id,
                    "avatar_url": avatar_url,
                    "reply_to_author": reply_to_author,
                    "reply_to_content": reply_to_content,
                    "topic": tid,
                    "timestamp": _parse_iso(m.get("timestamp")),
                }
                await messages_coll().update_one(
                    {"discord_message_id": msg_id, "topic": tid},
                    {"$set": doc},
                    upsert=True,
                )

            # Trim to 100 newest by snowflake
            count = await messages_coll().count_documents({"topic": tid})
            if count > 100:
                oldest = [d["_id"] async for d in messages_coll().find(
                    {"topic": tid}, {"_id": 1}
                ).sort("mid", 1).limit(count - 100)]
                if oldest:
                    await messages_coll().delete_many({"_id": {"$in": oldest}})

            logger.info("topic monitor REST: synced msgs for topic=%s (total=%d)", tid, min(count, 100))


async def _topic_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await _topic_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("topic monitor cycle failed")
        await asyncio.sleep(settings.monitor_topic_interval)


# ── DM monitor ───────────────────────────────────────────────────────────────
async def _dm_cycle() -> None:
    settings = get_settings()
    timeout = ClientTimeout(total=settings.discord_http_timeout)
    base = settings.discord_api_base

    async for acc in discords().find({"token_valid": True}):
        try:
            token = decrypt(acc["discord_token"])
        except ValueError:
            continue
        proxy_url = await _account_proxy_url(acc)
        headers = _headers(token)
        my_username = acc.get("username")
        my_user_id = acc.get("discord_user_id")

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                channels_url = f"{base}/users/@me/channels"
                async with session.get(channels_url, headers=headers, proxy=proxy_url) as resp:
                    if resp.status != 200:
                        logger.info(
                            "dm channels list status=%s for %s", resp.status, acc.get("email")
                        )
                        continue
                    channels = await resp.json()

                # Type 1 = 1:1 DM, type 3 = group DM. Spec is private DMs only.
                for ch in channels:
                    if ch.get("type") != 1:
                        continue
                    cid = ch["id"]
                    msg_url = f"{base}/channels/{cid}/messages?limit=50"
                    async with session.get(msg_url, headers=headers, proxy=proxy_url) as resp:
                        if resp.status != 200:
                            continue
                        msgs = await resp.json()

                    for m in msgs:
                        author = m.get("author") or {}
                        if my_user_id and author.get("id") == my_user_id:
                            continue
                        if not my_user_id and my_username and author.get("username") == my_username:
                            continue

                        msg_id = m.get("id")
                        existing = await private_messages_coll().find_one(
                            {"discord_message_id": msg_id, "to": acc["email"]}
                        )
                        if existing is not None:
                            continue

                        attachments = m.get("attachments") or []
                        image = attachments[0].get("url") if attachments else None
                        await private_messages_coll().insert_one(
                            {
                                "text": m.get("content", ""),
                                "image": image,
                                "from": author.get("username") or "?",
                                "from_id": author.get("id"),          # Discord user_id for replies
                                "to": acc["email"],
                                "dm_channel_id": cid,                 # channel to reply in
                                "is_read": False,
                                "discord_message_id": msg_id,
                                "timestamp": _parse_iso(m.get("timestamp")),
                            }
                        )
        except (ClientError, TimeoutError) as exc:
            logger.warning("dm fetch error for %s: %s", acc.get("email"), exc)


async def _dm_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await _dm_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("dm monitor cycle failed")
        await asyncio.sleep(settings.monitor_dm_interval)


# ── Scheduled message sender ─────────────────────────────────────────────────
async def _scheduler_cycle() -> None:
    import random
    from datetime import datetime, timezone
    from bson import ObjectId
    from app.database import db as _db
    from app.services.account_helpers import load_account_token_and_proxy
    from app.services.discord_api import send_message, trigger_typing

    now = datetime.now(timezone.utc)
    coll = _db()["scheduled_messages"]

    async for task in coll.find({"status": "pending", "scheduled_at": {"$lte": now}}):
        acc_id = task.get("account_id", "")
        resolved = await load_account_token_and_proxy(acc_id)
        if resolved is None:
            await coll.update_one({"_id": task["_id"]}, {"$set": {"status": "failed", "error": "unreadable"}})
            continue
        _, token, proxy_url = resolved

        # Typing simulation before the scheduled send
        content = task["content"]
        typing_delay = max(1.5, min(8.0, len(content) * 0.07)) * random.uniform(0.8, 1.2)
        await trigger_typing(token, task["channel_id"], proxy_url=proxy_url)
        await asyncio.sleep(typing_delay)

        msg = await send_message(
            token, task["channel_id"], content,
            reply_to=task.get("reply_to"), proxy_url=proxy_url,
        )
        if msg:
            await coll.update_one({"_id": task["_id"]}, {"$set": {"status": "sent", "sent_at": now}})
            logger.info("scheduler: sent message %s to channel %s", task["_id"], task["channel_id"])
        else:
            await coll.update_one({"_id": task["_id"]}, {"$set": {"status": "failed"}})
            logger.warning("scheduler: failed to send message %s", task["_id"])


async def _scheduler_loop() -> None:
    while True:
        try:
            await _scheduler_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("scheduler cycle failed")
        await asyncio.sleep(10)  # check every 10 seconds


# ── Lifecycle ────────────────────────────────────────────────────────────────
_tasks: dict[str, asyncio.Task] = {}


def start() -> None:
    settings = get_settings()
    if not settings.monitor_enabled:
        logger.info("monitors disabled (MONITOR_ENABLED=false)")
        return
    if "topic" not in _tasks or _tasks["topic"].done():
        _tasks["topic"] = asyncio.create_task(_topic_loop(), name="topic-monitor")
    if "dm" not in _tasks or _tasks["dm"].done():
        _tasks["dm"] = asyncio.create_task(_dm_loop(), name="dm-monitor")
    if "scheduler" not in _tasks or _tasks["scheduler"].done():
        _tasks["scheduler"] = asyncio.create_task(_scheduler_loop(), name="scheduler")
    logger.info("monitors started: %s", list(_tasks.keys()))


async def stop() -> None:
    for task in list(_tasks.values()):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _tasks.clear()
    logger.info("monitors stopped")
