"""Chat: send / reply, react, scheduled messages, private messages."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import json

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import chat_channels, messages as messages_coll, private_messages as private_messages_coll
from app.security import require_login
from app.services.account_helpers import load_account_token_and_proxy
from app.services.discord_api import add_reaction, get_or_create_dm_channel, send_message, send_message_with_files, trigger_typing

MAX_FILE_BYTES = 8 * 1024 * 1024  # Discord non-Nitro limit

# ── Send queue: limits concurrent sends to avoid event-loop saturation ───────
_send_sem = asyncio.Semaphore(3)
_pending_sends: set[asyncio.Task] = set()
_MAX_PENDING = 20


async def _send_with_typing(
    token: str,
    channel_id: str,
    content: str,
    *,
    reply_to: str | None = None,
    proxy_url: str | None = None,
) -> dict | None:
    """Trigger typing then send. Delay scales with message length (~50 WPM).

    Discord's typing indicator lasts ~8 s, so for longer delays we
    re-trigger typing every 7 s to keep the pencil visible.
    """
    async with _send_sem:
        # 15 chars ~2s, 50 chars ~5s, 150 chars ~10s, 300+ chars ~20s+
        n = len(content)
        if n <= 15:
            base = 2.0
        elif n <= 50:
            base = 2.0 + (n - 15) * (3.0 / 35)
        elif n <= 150:
            base = 5.0 + (n - 50) * (5.0 / 100)
        else:
            base = 10.0 + (n - 150) * (10.0 / 150)
        delay = base * random.uniform(0.85, 1.15)

        await trigger_typing(token, channel_id, proxy_url=proxy_url)
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, 7.0)
            await asyncio.sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                await trigger_typing(token, channel_id, proxy_url=proxy_url)

        return await send_message(token, channel_id, content, reply_to=reply_to, proxy_url=proxy_url)


router = APIRouter(
    prefix="/api/chat",
    dependencies=[Depends(require_login)],
    tags=["chat"],
)
logger = logging.getLogger(__name__)


# ── Send ──────────────────────────────────────────────────────────────────────
class SendBody(BaseModel):
    account_id: str
    channel_id: str
    content: str = Field(..., min_length=1, max_length=2000)
    reply_to: str | None = None


@router.post("/send")
async def send(body: SendBody, user: str = Depends(require_login)) -> dict:
    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    # Clean up finished tasks
    for t in [t for t in _pending_sends if t.done()]:
        _pending_sends.discard(t)
    if len(_pending_sends) >= _MAX_PENDING:
        return {"sent": False, "error": "too_many_pending"}

    async def _bg() -> None:
        try:
            await _send_with_typing(
                token, body.channel_id, body.content,
                reply_to=body.reply_to, proxy_url=proxy_url,
            )
        except Exception:  # noqa: BLE001
            logger.warning("send bg failed channel=%s", body.channel_id)

    task = asyncio.create_task(_bg(), name="chat-send")
    _pending_sends.add(task)
    task.add_done_callback(_pending_sends.discard)
    return {"sent": True}


class DuplicateBody(BaseModel):
    account_id: str
    channel_ids: list[str]
    content: str = Field(..., min_length=1, max_length=2000)


@router.post("/duplicate")
async def duplicate(body: DuplicateBody, user: str = Depends(require_login)) -> dict:
    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    async def _bg_send() -> None:
        for i, cid in enumerate(body.channel_ids):
            try:
                await send_message(token, cid, body.content, proxy_url=proxy_url)
            except Exception:  # noqa: BLE001
                logger.warning("duplicate: failed channel %s", cid)
            if i < len(body.channel_ids) - 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))

    asyncio.create_task(_bg_send(), name="duplicate-send")
    return {"ok": True, "queued": len(body.channel_ids)}


# ── React (single) ────────────────────────────────────────────────────────────
class ReactBody(BaseModel):
    account_id: str
    channel_id: str
    message_id: str
    emoji: str


@router.post("/react")
async def react(body: ReactBody, user: str = Depends(require_login)) -> dict:
    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved
    ok = await add_reaction(token, body.channel_id, body.message_id, body.emoji, proxy_url=proxy_url)
    return {"ok": ok}


# ── Bulk React (Utils section) ────────────────────────────────────────────────
class BulkReactBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    channel_id: str
    message_id: str
    emoji: str
    delay_min: float = Field(0, ge=0, le=60)
    delay_max: float = Field(0, ge=0, le=60)


@router.post("/react-bulk")
async def react_bulk(body: BulkReactBody, user: str = Depends(require_login)) -> dict:
    """Add the same reaction from N accounts — fire-and-forget, survives page refresh."""

    async def _bg() -> None:
        async def _react_one(acc_id: str, idx: int) -> None:
            try:
                if idx > 0 and body.delay_max > 0:
                    await asyncio.sleep(random.uniform(body.delay_min, body.delay_max))
                resolved = await load_account_token_and_proxy(acc_id, owner=user)
                if resolved is None:
                    return
                _, token, proxy_url = resolved
                await add_reaction(token, body.channel_id, body.message_id, body.emoji, proxy_url=proxy_url)
            except Exception:  # noqa: BLE001
                logger.warning("react failed acc=%s emoji=%s", acc_id, body.emoji)

        await asyncio.gather(*(_react_one(a, i) for i, a in enumerate(body.account_ids)))

    asyncio.create_task(_bg(), name="react-bulk")
    return {"ok": True, "queued": len(body.account_ids)}


# ── Send with file ────────────────────────────────────────────────────────────
@router.post("/send-with-file")
async def send_with_file(
    account_id: str = Form(...),
    channel_id: str = Form(...),
    content: str = Form(""),
    reply_to: str | None = Form(None),
    files: list[UploadFile] = File(default_factory=list),
    user: str = Depends(require_login),
) -> dict:
    if not files and not content.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Either content or a file is required")
    resolved = await load_account_token_and_proxy(account_id, owner=user)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved
    blobs: list[tuple[str, bytes, str | None]] = []
    for f in files:
        blob = await f.read()
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{f.filename}: too large (max 8 MB)")
        blobs.append((f.filename or "file", blob, f.content_type))

    # Show typing indicator before sending (1.5 s minimum for file-only uploads)
    typing_base = max(1.5, min(8.0, len(content) * 0.07))
    await trigger_typing(token, channel_id, proxy_url=proxy_url)
    await asyncio.sleep(typing_base * random.uniform(0.8, 1.2))

    if not blobs:
        msg = await send_message(token, channel_id, content, reply_to=reply_to, proxy_url=proxy_url)
    else:
        msg = await send_message_with_files(token, channel_id, content, blobs, reply_to=reply_to, proxy_url=proxy_url)
    if msg is None:
        return {"sent": False}
    return {"sent": True, "message_id": msg.get("id"), "files": len(blobs)}


# ── Scheduled messages ────────────────────────────────────────────────────────
from app.database import db as _db  # noqa: E402


def scheduled_messages():
    return _db()["scheduled_messages"]


class ScheduleBody(BaseModel):
    account_id: str
    channel_id: str
    content: str = Field(..., min_length=1, max_length=2000)
    reply_to: str | None = None
    scheduled_at: str  # ISO datetime string


@router.post("/schedule")
async def schedule_message(
    body: ScheduleBody,
    user: str = Depends(require_login),
) -> dict:
    """Save a message to be sent at `scheduled_at` (ISO UTC datetime)."""
    try:
        ts = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid scheduled_at format (ISO 8601 required)")
    if ts <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scheduled_at must be in the future")
    res = await scheduled_messages().insert_one({
        "owner": user,
        "account_id": body.account_id,
        "channel_id": body.channel_id,
        "content": body.content,
        "reply_to": body.reply_to,
        "scheduled_at": ts,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return {"id": str(res.inserted_id), "scheduled_at": ts.isoformat()}


@router.get("/scheduled")
async def list_scheduled(user: str = Depends(require_login)) -> list[dict]:
    out: list[dict] = []
    async for m in scheduled_messages().find({"owner": user, "status": "pending"}).sort("scheduled_at", 1):
        out.append({
            "id": str(m["_id"]),
            "account_id": m.get("account_id"),
            "channel_id": m.get("channel_id"),
            "content": m.get("content", ""),
            "scheduled_at": m["scheduled_at"].isoformat() if m.get("scheduled_at") else None,
        })
    return out


@router.delete("/scheduled/{msg_id}")
async def cancel_scheduled(
    msg_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(msg_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")
    res = await scheduled_messages().update_one(
        {"_id": ObjectId(msg_id), "owner": user, "status": "pending"},
        {"$set": {"status": "cancelled"}},
    )
    if res.matched_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found or already sent")
    return {"cancelled": True}


# ── Saved chat channels ───────────────────────────────────────────────────────
class ChatChannelBody(BaseModel):
    channel_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1, max_length=64)


@router.get("/channels")
async def list_chat_channels(user: str = Depends(require_login)) -> list[dict]:
    out: list[dict] = []
    async for ch in chat_channels().find({"owner": user}).sort("label", 1):
        out.append({"id": str(ch["_id"]), "channel_id": ch["channel_id"], "label": ch.get("label", "")})
    return out


@router.post("/channels")
async def save_chat_channel(
    body: ChatChannelBody,
    user: str = Depends(require_login),
) -> dict:
    res = await chat_channels().insert_one({
        "owner": user,
        "channel_id": body.channel_id.strip(),
        "label": body.label,
    })
    return {"id": str(res.inserted_id), "channel_id": body.channel_id, "label": body.label}


@router.delete("/channels/{ch_id}")
async def delete_chat_channel(
    ch_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(ch_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")
    await chat_channels().delete_one({"_id": ObjectId(ch_id), "owner": user})
    return {"deleted": True}


# ── Topic messages ────────────────────────────────────────────────────────────
@router.get("/topic/{topic_id}/stream")
async def stream_topic_messages(
    topic_id: str,
    request: Request,
    user: str = Depends(require_login),
) -> StreamingResponse:
    """SSE stream — pushes new messages to the browser the moment they arrive
    via the Gateway listener. Sends a keepalive comment every 20 s so proxies
    don't kill the connection."""
    from app.services.topic_listener import subscribe, unsubscribe

    q = subscribe(topic_id)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keeps the connection alive through proxies
        finally:
            unsubscribe(topic_id, q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer SSE
        },
    )


@router.get("/topic/{topic_id}/messages")
async def list_topic_messages(topic_id: str) -> list[dict]:
    cursor = messages_coll().find({"topic": topic_id}).sort("mid", -1).limit(100)
    out: list[dict] = []
    async for m in cursor:
        out.append({
            "id": str(m["_id"]),
            "discord_message_id": m.get("discord_message_id"),
            "text": m.get("text", ""),
            "image": m.get("image"),
            "from": m.get("from") or m.get("sender"),
            "avatar_url": m.get("avatar_url"),
            "reply_to_author": m.get("reply_to_author"),
            "reply_to_content": m.get("reply_to_content"),
            "timestamp": m["timestamp"].isoformat() if m.get("timestamp") else None,
        })
    out.reverse()
    return out


# ── DM monitor account selection ──────────────────────────────────────────────
@router.get("/private/monitored-accounts")
async def list_dm_monitored(user: str = Depends(require_login)) -> list[dict]:
    """Return all accounts with their dm_monitor flag."""
    from app.database import discords
    out: list[dict] = []
    async for acc in discords().find({"owner": user, "token_valid": True}).sort("_id", -1):
        out.append({
            "id": str(acc["_id"]),
            "email": acc["email"],
            "name": acc.get("name") or acc.get("username") or acc["email"],
            "group": acc.get("group", "Massovka1"),
            "dm_monitor": acc.get("dm_monitor", False),
        })
    return out


class DmMonitorBody(BaseModel):
    account_ids: list[str]
    enabled: bool


@router.post("/private/set-monitor")
async def set_dm_monitor(body: DmMonitorBody, user: str = Depends(require_login)) -> dict:
    """Enable or disable DM monitoring for selected accounts."""
    from app.database import discords
    valid_ids = [ObjectId(aid) for aid in body.account_ids if ObjectId.is_valid(aid)]
    if not valid_ids:
        return {"ok": False, "error": "no valid ids"}
    res = await discords().update_many(
        {"_id": {"$in": valid_ids}, "owner": user},
        {"$set": {"dm_monitor": body.enabled}},
    )
    return {"ok": True, "updated": res.modified_count}


# ── Private messages (DMs) ────────────────────────────────────────────────────
@router.post("/private/refresh")
async def refresh_dms() -> dict:
    """Immediately run one DM monitor cycle without waiting for the interval."""
    from app.services.monitor import _dm_cycle
    try:
        await _dm_cycle()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/private/conversations")
async def list_dm_conversations(user: str = Depends(require_login)) -> list[dict]:
    """List DM conversations grouped by sender, with unread count and last message."""
    from app.database import discords
    # Only show DMs for accounts owned by this user
    owned_emails = [
        acc["email"] async for acc in discords().find({"owner": user}, {"email": 1})
    ]
    if not owned_emails:
        return []
    pipeline = [
        {"$match": {"is_outgoing": {"$ne": True}, "to": {"$in": owned_emails}}},
        {"$sort": {"timestamp": -1}},
        {"$group": {
            "_id": {"from": "$from", "to": "$to"},
            "last_text": {"$first": "$text"},
            "last_ts": {"$first": "$timestamp"},
            "unread": {"$sum": {"$cond": [{"$eq": ["$is_read", False]}, 1, 0]}},
            "dm_channel_id": {"$first": "$dm_channel_id"},
            "from_id": {"$first": "$from_id"},
        }},
        {"$sort": {"last_ts": -1}},
    ]
    out: list[dict] = []
    async for g in private_messages_coll().aggregate(pipeline):
        out.append({
            "from": g["_id"]["from"],
            "to": g["_id"]["to"],
            "last_text": g.get("last_text", ""),
            "last_ts": g["last_ts"].isoformat() if g.get("last_ts") else None,
            "unread": g.get("unread", 0),
            "dm_channel_id": g.get("dm_channel_id"),
            "from_id": g.get("from_id"),
        })
    return out


@router.get("/private/messages")
async def get_dm_messages(sender: str, to: str, user: str = Depends(require_login)) -> list[dict]:
    """Last 100 messages in a DM conversation — both incoming and outgoing."""
    from app.database import discords
    owns = await discords().find_one({"owner": user, "email": to})
    if not owns:
        return []
    query = {"$or": [
        {"from": sender, "to": to},
        {"to": to, "dm_peer": sender, "is_outgoing": True},
    ]}
    cursor = private_messages_coll().find(query).sort("timestamp", -1).limit(100)
    out: list[dict] = []
    async for m in cursor:
        out.append({
            "id": str(m["_id"]),
            "text": m.get("text", ""),
            "image": m.get("image"),
            "from": m.get("from"),
            "to": m.get("to"),
            "is_read": m.get("is_read", False),
            "is_outgoing": m.get("is_outgoing", False),
            "timestamp": m["timestamp"].isoformat() if m.get("timestamp") else None,
            "dm_channel_id": m.get("dm_channel_id"),
        })
    out.reverse()
    return out


@router.post("/private/mark-read")
async def mark_dm_read(body: dict) -> dict:
    """Mark all messages from a sender to an account as read."""
    sender = body.get("sender", "")
    to = body.get("to", "")
    await private_messages_coll().update_many(
        {"from": sender, "to": to, "is_read": False},
        {"$set": {"is_read": True}},
    )
    return {"ok": True}


@router.get("/private/unread-count")
async def unread_count() -> dict:
    """Total unread DM count across all accounts."""
    count = await private_messages_coll().count_documents({"is_read": False})
    return {"count": count}


class DMReplyBody(BaseModel):
    account_id: str
    dm_channel_id: str | None = None   # may be missing for old DMs
    sender_username: str | None = None  # used to look up from_id when channel unknown
    to: str | None = None               # account email to identify which account received the DM
    content: str = Field(..., min_length=1, max_length=2000)


@router.post("/private/reply")
async def reply_dm(body: DMReplyBody) -> dict:
    """Send a DM reply.

    If dm_channel_id is missing (old messages stored before Phase-5), we look up
    the sender's Discord user_id from stored private_messages and call
    POST /users/@me/channels to get/create the DM channel automatically.
    """
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    channel_id = body.dm_channel_id

    # Auto-resolve channel when missing
    if not channel_id:
        from_id: str | None = None
        # Try to find from_id in stored messages for this conversation
        if body.sender_username and body.to:
            stored = await private_messages_coll().find_one(
                {"from": body.sender_username, "to": body.to, "from_id": {"$exists": True}},
                sort=[("_id", -1)],
            )
            if stored:
                from_id = stored.get("from_id")

        if not from_id:
            return {"sent": False, "error": "dm_channel_id missing and from_id not stored — wait for next DM monitor cycle"}

        channel_id = await get_or_create_dm_channel(token, from_id, proxy_url=proxy_url)
        if not channel_id:
            return {"sent": False, "error": "could not create DM channel"}

        # Persist for future replies
        if body.sender_username and body.to:
            await private_messages_coll().update_many(
                {"from": body.sender_username, "to": body.to},
                {"$set": {"dm_channel_id": channel_id}},
            )
        logger.info("reply_dm: resolved channel_id=%s for sender=%s", channel_id, body.sender_username)

    msg = await send_message(token, channel_id, body.content, proxy_url=proxy_url)
    if msg is None:
        return {"sent": False}
    if isinstance(msg, dict) and msg.get("_discord_error"):
        code = msg.get("code")
        if code == 50278:
            return {"sent": False, "error": "no_mutual_guilds"}
        return {"sent": False, "error": f"discord_error_{code}"}

    # Persist the outgoing message so it survives page reload
    from app.database import discords as discords_coll
    acc_doc = await discords_coll().find_one({"_id": ObjectId(body.account_id)})
    our_username = (acc_doc or {}).get("username") or "me"
    await private_messages_coll().insert_one({
        "text": body.content,
        "image": None,
        "from": our_username,
        "to": body.to or "",
        "dm_channel_id": channel_id,
        "dm_peer": body.sender_username,  # the external user we're replying to
        "is_outgoing": True,
        "is_read": True,
        "timestamp": datetime.now(timezone.utc),
        "discord_message_id": msg.get("id"),
    })

    return {"sent": True, "message_id": msg.get("id"), "channel_id": channel_id}
