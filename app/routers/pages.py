"""HTML page renders (server-side templates)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import discords, proxies as proxies_coll, topics
from app.security import decrypt, require_login

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _safe_decrypt(value: str | None) -> str:
    """Decrypt an encrypted field; return empty string on failure or None."""
    if not value:
        return ""
    try:
        return decrypt(value)
    except Exception:
        return ""


def _avatar_url(acc: dict) -> str:
    """Build a Discord CDN avatar URL from a DB document.

    Priority:
    1. Custom avatar hash → cdn.discordapp.com/avatars/{uid}/{hash}.png/gif
    2. No custom avatar but uid known → default colored avatar (index from uid)
    3. Neither → empty string (template shows '?' placeholder)
    """
    uid = acc.get("discord_user_id", "")
    avatar = acc.get("avatar")
    if avatar and uid:
        ext = "gif" if avatar.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.{ext}?size=64"
    if uid:
        # Default Discord avatar: index = (user_id >> 22) % 6
        try:
            idx = (int(uid) >> 22) % 6
        except (ValueError, TypeError):
            idx = 0
        return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"
    return ""


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find({"owner": user}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or "—",
                "name": acc.get("name") or "—",
                "token_valid": acc.get("token_valid", False),
                "has_proxy": acc.get("proxy_id") is not None,
                "joined_server": acc.get("joined_server", False),
                "joined_voice": acc.get("joined_voice", False),
                "joined_stream": acc.get("joined_stream", False),
                "has_2fa": acc.get("two_fa_secret") is not None,
                "is_donor": acc.get("is_donor", False),
                "group": acc.get("group", "Massovka1"),
                "password": _safe_decrypt(acc.get("password")),
                "discord_token": _safe_decrypt(acc.get("discord_token")),
                "status": acc.get("status", "online"),
                "avatar_url": _avatar_url(acc),
            }
        )

    total_accounts = await discords().count_documents({"owner": user})
    total_proxies = await proxies_coll().count_documents({"owner": user})
    assigned_proxies = await proxies_coll().count_documents({"owner": user, "assigned": True})
    free_proxies = total_proxies - assigned_proxies

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "active": "accounts",
            "accounts": accounts,
            "stats": {
                "total_accounts": total_accounts,
                "total_proxies": total_proxies,
                "assigned_proxies": assigned_proxies,
                "free_proxies": free_proxies,
            },
        },
    )


@router.get("/stage", response_class=HTMLResponse)
async def stage_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find({"owner": user, "token_valid": True, "joined_server": True}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
                "name": acc.get("name") or acc.get("username") or acc["email"],
                "joined_voice": acc.get("joined_voice", False),
                "voice_channel_id": acc.get("voice_channel_id"),
            }
        )
    return templates.TemplateResponse(
        request,
        "stage.html",
        {"user": user, "active": "stage", "accounts": accounts},
    )


@router.get("/voice", response_class=HTMLResponse)
async def voice_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find({"owner": user, "token_valid": True, "joined_server": True}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
                "name": acc.get("name") or acc.get("username") or acc["email"],
                "group": acc.get("group", "Massovka1"),
                "joined_voice": acc.get("joined_voice", False),
                "voice_channel_id": acc.get("voice_channel_id"),
                "voice_guild_id": acc.get("voice_guild_id"),
                "voice_muted": acc.get("voice_muted", True),
            }
        )
    return templates.TemplateResponse(
        request,
        "voice.html",
        {"user": user, "active": "voice", "accounts": accounts},
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    # Accounts for topic chat: joined_server + token_valid
    accounts: list[dict] = []
    async for acc in discords().find({"owner": user, "token_valid": True, "joined_server": True}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
                "name": acc.get("name") or acc.get("username") or acc["email"],
                "group": acc.get("group", "Massovka1"),
                "avatar_url": _avatar_url(acc),
            }
        )
    # All token-valid accounts for DM reply map (DMs can arrive on any account)
    all_accounts: list[dict] = []
    async for acc in discords().find({"owner": user, "token_valid": True}).sort("_id", -1):
        all_accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
            }
        )
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"user": user, "active": "chat", "accounts": accounts, "all_accounts": all_accounts},
    )


@router.get("/utils", response_class=HTMLResponse)
async def utils_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find({"owner": user}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or "—",
                "name": acc.get("name") or acc.get("username") or "—",
                "bio": acc.get("bio") or "",
                "avatar_url": _avatar_url(acc),
                "token_valid": acc.get("token_valid", False),
                "has_2fa": acc.get("two_fa_secret") is not None,
                "joined_server": acc.get("joined_server", False),
                "is_donor": acc.get("is_donor", False),
            }
        )

    monitored_topics: list[dict] = []
    async for t in topics().find({"owner": user}).sort("_id", -1):
        monitored_topics.append(
            {"id": str(t["_id"]), "channel_id": t["channel_id"], "label": t.get("label")}
        )

    return templates.TemplateResponse(
        request,
        "utils.html",
        {
            "user": user,
            "active": "utils",
            "accounts": accounts,
            "monitored_topics": monitored_topics,
        },
    )
