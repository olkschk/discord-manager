"""Chat: send / reply, react, list stored topic messages."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.database import messages as messages_coll
from app.security import require_login
from app.services.account_helpers import load_account_token_and_proxy
from app.services.discord_api import add_reaction, send_message, send_message_with_files

# Discord limit for non-Nitro accounts (per attachment, total uploads).
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB

router = APIRouter(
    prefix="/api/chat",
    dependencies=[Depends(require_login)],
    tags=["chat"],
)
logger = logging.getLogger(__name__)


class SendBody(BaseModel):
    account_id: str
    channel_id: str
    content: str = Field(..., min_length=1, max_length=2000)
    reply_to: str | None = None


@router.post("/send")
async def send(body: SendBody) -> dict:
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    msg = await send_message(
        token, body.channel_id, body.content, reply_to=body.reply_to, proxy_url=proxy_url
    )
    if msg is None:
        return {"sent": False}
    return {"sent": True, "message_id": msg.get("id")}


class DuplicateBody(BaseModel):
    account_id: str
    channel_ids: list[str]
    content: str = Field(..., min_length=1, max_length=2000)


@router.post("/duplicate")
async def duplicate(body: DuplicateBody) -> dict:
    """Send the same content to N channels using one account."""
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    results: list[dict] = []
    for cid in body.channel_ids:
        msg = await send_message(token, cid, body.content, proxy_url=proxy_url)
        results.append({"channel_id": cid, "ok": msg is not None})
    return {"results": results}


class ReactBody(BaseModel):
    account_id: str
    channel_id: str
    message_id: str
    emoji: str  # raw unicode emoji or 'name:id' for custom emoji


@router.post("/react")
async def react(body: ReactBody) -> dict:
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved

    ok = await add_reaction(
        token, body.channel_id, body.message_id, body.emoji, proxy_url=proxy_url
    )
    return {"ok": ok}


@router.post("/send-with-file")
async def send_with_file(
    account_id: str = Form(...),
    channel_id: str = Form(...),
    content: str = Form(""),
    reply_to: str | None = Form(None),
    files: list[UploadFile] = File(default_factory=list),
) -> dict:
    """Multipart variant of /send — accepts attachments.

    If no file is supplied, falls back to the JSON path so the front-end can
    use this single endpoint for both cases.
    """
    if not files and not content.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Either content or at least one file is required"
        )

    resolved = await load_account_token_and_proxy(account_id)
    if resolved is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Account not found or token unreadable"
        )
    _, token, proxy_url = resolved

    blobs: list[tuple[str, bytes, str | None]] = []
    for f in files:
        blob = await f.read()
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"{f.filename}: too large ({len(blob)} bytes; Discord limit is 8 MB)",
            )
        blobs.append((f.filename or "file", blob, f.content_type))

    if not blobs:
        msg = await send_message(
            token, channel_id, content, reply_to=reply_to, proxy_url=proxy_url
        )
    else:
        msg = await send_message_with_files(
            token, channel_id, content, blobs, reply_to=reply_to, proxy_url=proxy_url
        )

    if msg is None:
        return {"sent": False}
    return {"sent": True, "message_id": msg.get("id"), "files": len(blobs)}


@router.get("/topic/{topic_id}/messages")
async def list_topic_messages(topic_id: str) -> list[dict]:
    """Last 100 stored messages for a topic (populated by Phase-3 monitoring loop)."""
    cursor = messages_coll().find({"topic": topic_id}).sort("timestamp", -1).limit(100)
    out: list[dict] = []
    async for m in cursor:
        out.append(
            {
                "id": str(m["_id"]),
                "text": m.get("text", ""),
                "image": m.get("image"),
                "from": m.get("from") or m.get("sender"),
                "timestamp": m["timestamp"].isoformat() if m.get("timestamp") else None,
            }
        )
    return list(reversed(out))  # oldest-first for natural reading order
