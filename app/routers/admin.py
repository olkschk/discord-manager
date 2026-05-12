"""Admin-only API: panel user management (create / list / delete)."""
from __future__ import annotations

import logging

import bcrypt
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.database import users
from app.security import require_login

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

ADMIN_LOGIN = "admin"


def _require_admin(user: str = Depends(require_login)) -> str:
    if user != ADMIN_LOGIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


# ── Page ─────────────────────────────────────────────────────────────────────
@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    user: str = Depends(_require_admin),
) -> HTMLResponse:
    panel_users: list[dict] = []
    async for u in users().find().sort("login", 1):
        panel_users.append({
            "id": str(u["_id"]),
            "login": u["login"],
            "is_self": u["login"] == ADMIN_LOGIN,
        })
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"user": user, "active": "admin", "panel_users": panel_users},
    )


# ── API ───────────────────────────────────────────────────────────────────────
class CreateUserBody(BaseModel):
    login: str = Field(..., min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: str = Field(..., min_length=8, max_length=128)


@router.post("/api/admin/users")
async def create_user(
    body: CreateUserBody,
    _: str = Depends(_require_admin),
) -> dict:
    existing = await users().find_one({"login": body.login})
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Login already taken")

    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    res = await users().insert_one({"login": body.login, "password": hashed})
    logger.info("Admin created panel user %r", body.login)
    return {"id": str(res.inserted_id), "login": body.login}


@router.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    _: str = Depends(_require_admin),
) -> dict:
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid id")

    target = await users().find_one({"_id": ObjectId(user_id)})
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target["login"] == ADMIN_LOGIN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete admin account")

    await users().delete_one({"_id": ObjectId(user_id)})
    logger.info("Admin deleted panel user %r", target["login"])
    return {"deleted": True}
