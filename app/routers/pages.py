"""HTML page renders (server-side templates)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import discords, proxies as proxies_coll, topics
from app.security import require_login

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find().sort("_id", -1):
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
            }
        )

    total_accounts = await discords().count_documents({})
    total_proxies = await proxies_coll().count_documents({})
    assigned_proxies = await proxies_coll().count_documents({"assigned": True})
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


@router.get("/voice", response_class=HTMLResponse)
async def voice_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find({"token_valid": True}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
                "joined_voice": acc.get("joined_voice", False),
                "voice_channel_id": acc.get("voice_channel_id"),
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
    accounts: list[dict] = []
    async for acc in discords().find({"token_valid": True}).sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or acc["email"],
            }
        )
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"user": user, "active": "chat", "accounts": accounts},
    )


@router.get("/utils", response_class=HTMLResponse)
async def utils_page(
    request: Request,
    user: str = Depends(require_login),
) -> HTMLResponse:
    accounts: list[dict] = []
    async for acc in discords().find().sort("_id", -1):
        accounts.append(
            {
                "id": str(acc["_id"]),
                "email": acc["email"],
                "username": acc.get("username") or "—",
                "name": acc.get("name") or "—",
                "bio": acc.get("bio") or "",
                "token_valid": acc.get("token_valid", False),
                "has_2fa": acc.get("two_fa_secret") is not None,
                "joined_server": acc.get("joined_server", False),
                "is_donor": acc.get("is_donor", False),
            }
        )

    monitored_topics: list[dict] = []
    async for t in topics().find().sort("_id", -1):
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
