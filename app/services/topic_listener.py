"""Real-time topic message listener via Discord Gateway MESSAGE_CREATE events.

Replaces the REST-polling _topic_cycle. The donor account connects to the
Gateway and receives MESSAGE_CREATE events instantly. Messages are saved to
the `messages` collection and trimmed to 100 per channel.

Adapted from discord-farm/disc/bot/listener.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp

from app.config import get_settings
from app.database import discords, messages as messages_coll, proxies as proxies_coll, topics
from app.security import decrypt
from app.services.discord_api import build_proxy_url, _x_super_properties

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Module-level task reference
_task: asyncio.Task | None = None

# ── Pub/sub for SSE clients ───────────────────────────────────────────────────
# channel_id -> set of asyncio.Queue, one per connected browser tab
from collections import defaultdict  # noqa: E402
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe(topic_id: str) -> asyncio.Queue:
    """Register a new SSE listener for a topic. Returns its queue."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[topic_id].add(q)
    return q


def unsubscribe(topic_id: str, q: asyncio.Queue) -> None:
    """Deregister an SSE listener when client disconnects."""
    _subscribers[topic_id].discard(q)


def _build_identify_props() -> dict:
    s = get_settings()
    ua = s.discord_user_agent
    chrome_ver = "147.0.0.0"
    if "Chrome/" in ua:
        try:
            chrome_ver = ua.split("Chrome/")[1].split(" ")[0]
        except IndexError:
            pass
    return {
        "os": "Windows", "browser": "Chrome", "device": "",
        "system_locale": "ru-RU", "has_client_mods": False,
        "browser_user_agent": ua,
        "browser_version": chrome_ver,
        "os_version": "10", "referrer": "", "referring_domain": "",
        "release_channel": "stable",
        "client_build_number": 539147,
        "client_event_source": None,
    }


async def _save_message(data: dict, topic_id: str) -> None:
    """Save a MESSAGE_CREATE payload to the messages collection."""
    author = data.get("author") or {}
    author_id = author.get("id", "")
    avatar = author.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{author_id}/{avatar}.png?size=64"
        if avatar
        else f"https://cdn.discordapp.com/embed/avatars/{int(author_id or 0) % 5}.png"
    )
    attachments = data.get("attachments") or []
    image = attachments[0].get("url") if attachments else None

    # Reply info
    ref = data.get("referenced_message")
    reply_to_author = None
    reply_to_content = None
    if ref:
        ref_a = ref.get("author") or {}
        reply_to_author = ref_a.get("global_name") or ref_a.get("username")
        reply_to_content = ref.get("content", "")[:100]
        if not reply_to_content and ref.get("attachments"):
            reply_to_content = "📎 Attachment"

    msg_doc = {
        "discord_message_id": data["id"],
        "mid": int(data["id"]),
        "text": data.get("content", ""),
        "image": image,
        "from": author.get("global_name") or author.get("username") or "?",
        "author_id": author_id,
        "avatar_url": avatar_url,
        "topic": topic_id,
        "timestamp": _parse_ts(data.get("timestamp")),
        "reply_to_author": reply_to_author,
        "reply_to_content": reply_to_content,
    }

    await messages_coll().update_one(
        {"discord_message_id": msg_doc["discord_message_id"], "topic": topic_id},
        {"$set": msg_doc},
        upsert=True,
    )

    # Keep only last 100 per topic
    count = await messages_coll().count_documents({"topic": topic_id})
    if count > 100:
        oldest_cursor = messages_coll().find(
            {"topic": topic_id}, {"_id": 1}
        ).sort("mid", 1).limit(count - 100)
        ids = [d["_id"] async for d in oldest_cursor]
        if ids:
            await messages_coll().delete_many({"_id": {"$in": ids}})

    # Push to any SSE clients watching this topic
    if _subscribers[topic_id]:
        sse_payload = {
            "id": "",
            "discord_message_id": msg_doc["discord_message_id"],
            "text": msg_doc["text"],
            "image": msg_doc["image"],
            "from": msg_doc["from"],
            "avatar_url": msg_doc["avatar_url"],
            "reply_to_author": msg_doc["reply_to_author"],
            "reply_to_content": msg_doc["reply_to_content"],
            "timestamp": msg_doc["timestamp"].isoformat() if msg_doc.get("timestamp") else None,
        }
        for q in list(_subscribers[topic_id]):
            await q.put(sse_payload)


def _parse_ts(ts: str | None) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


async def _run_listener() -> None:
    """Main gateway loop — reconnects on error."""
    settings = get_settings()

    while True:
        try:
            # Load donor account
            donor = await discords().find_one({"is_donor": True, "token_valid": True})
            if donor is None:
                logger.debug("topic_listener: no donor — waiting 30s")
                await asyncio.sleep(30)
                continue

            try:
                token = decrypt(donor["discord_token"])
            except ValueError:
                logger.warning("topic_listener: donor token unreadable")
                await asyncio.sleep(30)
                continue

            # Load monitored topic channel IDs
            topic_ids: set[str] = set()
            async for t in topics().find():
                topic_ids.add(str(t["channel_id"]))

            if not topic_ids:
                logger.debug("topic_listener: no topics — waiting 30s")
                await asyncio.sleep(30)
                continue

            # Proxy
            proxy_url: str | None = None
            if donor.get("proxy_id"):
                proxy = await proxies_coll().find_one({"_id": donor["proxy_id"]})
                if proxy:
                    try:
                        proxy_url = build_proxy_url(
                            proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                        )
                    except ValueError:
                        proxy_url = None

            logger.info("topic_listener: connecting as %s watching %d topics", donor.get("email"), len(topic_ids))

            ws_kwargs = {"proxy": proxy_url} if proxy_url else {}
            last_seq = None
            hb_task: asyncio.Task | None = None

            connector = aiohttp.TCPConnector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(GATEWAY_URL, **ws_kwargs) as ws:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = json.loads(msg.data)
                        op = payload.get("op")
                        if payload.get("s"):
                            last_seq = payload["s"]

                        if op == 10:  # HELLO
                            interval = payload["d"]["heartbeat_interval"] / 1000

                            async def _hb(ws=ws):
                                nonlocal last_seq
                                while True:
                                    await asyncio.sleep(interval)
                                    try:
                                        await ws.send_json({"op": 1, "d": last_seq})
                                    except Exception:
                                        return

                            hb_task = asyncio.create_task(_hb())
                            await ws.send_json({"op": 2, "d": {
                                "token": token,
                                "capabilities": 16381,
                                "properties": _build_identify_props(),
                                "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
                                "compress": False,
                                "client_state": {"guild_versions": {}},
                            }})

                        elif op == 0:
                            event = payload.get("t")
                            data = payload.get("d", {})

                            if event == "READY":
                                logger.info("topic_listener: READY as %s", data.get("user", {}).get("username"))

                            elif event == "MESSAGE_CREATE":
                                ch_id = str(data.get("channel_id", ""))
                                if ch_id in topic_ids:
                                    await _save_message(data, ch_id)
                                    logger.debug("topic_listener: saved message in %s", ch_id)

                        elif op in (7, 9):
                            break

                    if hb_task:
                        hb_task.cancel()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("topic_listener error: %s — reconnecting in 5s", exc)

        await asyncio.sleep(5)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run_listener(), name="topic-listener")
        logger.info("topic_listener started")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    logger.info("topic_listener stopped")
