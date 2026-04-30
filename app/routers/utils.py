"""Utils: AI identity, join server, 2FA setup + code retrieval."""
from __future__ import annotations

import logging

import pyotp
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database import discords
from app.security import decrypt, encrypt, require_login
from app.services.account_helpers import load_account_token_and_proxy
from app.services.ai_client import generate_identity
from app.services.discord_api import enable_mfa, join_invite, patch_profile

router = APIRouter(
    prefix="/api/utils",
    dependencies=[Depends(require_login)],
    tags=["utils"],
)
logger = logging.getLogger(__name__)


# ── Identity ─────────────────────────────────────────────────────────────────
class IdentityBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)


@router.post("/identity")
async def change_identity(body: IdentityBody) -> dict:
    """Generate a fresh AI identity for each account and PATCH it to Discord."""
    results: list[dict] = []
    for acc_id in body.account_ids:
        resolved = await load_account_token_and_proxy(acc_id)
        if resolved is None:
            results.append({"account_id": acc_id, "ok": False, "error": "unreadable"})
            continue
        _, token, proxy_url = resolved

        try:
            ident = await generate_identity()
        except Exception as exc:  # noqa: BLE001 — surface AI failures per-account
            logger.exception("Identity gen failed for %s", acc_id)
            results.append(
                {"account_id": acc_id, "ok": False, "error": f"ai:{exc.__class__.__name__}"}
            )
            continue

        out = await patch_profile(
            token,
            username=ident["username"],
            global_name=ident["global_name"] or None,
            bio=ident["bio"] or None,
            proxy_url=proxy_url,
        )
        if out is None:
            results.append(
                {"account_id": acc_id, "ok": False, "error": "discord_patch_failed", "identity": ident}
            )
            continue

        await discords().update_one(
            {"_id": ObjectId(acc_id)},
            {
                "$set": {
                    "username": ident["username"],
                    "name": ident["global_name"],
                    "bio": ident["bio"],
                }
            },
        )
        results.append({"account_id": acc_id, "ok": True, "identity": ident})

    return {"results": results}


# ── Join server ──────────────────────────────────────────────────────────────
class JoinServerBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    invite: str  # bare invite code or full discord.gg/<code> URL


@router.post("/join-server")
async def join_server(body: JoinServerBody) -> dict:
    code = body.invite.strip().rstrip("/").rsplit("/", 1)[-1]
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty invite code")

    results: list[dict] = []
    for acc_id in body.account_ids:
        resolved = await load_account_token_and_proxy(acc_id)
        if resolved is None:
            results.append({"account_id": acc_id, "ok": False, "error": "unreadable"})
            continue
        _, token, proxy_url = resolved

        out = await join_invite(token, code, proxy_url=proxy_url)
        ok = out is not None
        if ok:
            await discords().update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"joined_server": True}},
            )
        results.append({"account_id": acc_id, "ok": ok})

    return {"results": results}


# ── 2FA ──────────────────────────────────────────────────────────────────────
class TwoFASetupBody(BaseModel):
    account_id: str


@router.post("/2fa/setup")
async def setup_two_fa(body: TwoFASetupBody) -> dict:
    """Generate a TOTP secret, enable MFA on Discord, persist secret + backup codes."""
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    acc, token, proxy_url = resolved

    if acc.get("two_fa_secret"):
        try:
            current = pyotp.TOTP(decrypt(acc["two_fa_secret"])).now()
        except ValueError:
            current = None
        return {"ok": True, "already_enabled": True, "code": current}

    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()

    try:
        password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Cannot decrypt account password"
        )

    out = await enable_mfa(token, secret, code, password, proxy_url=proxy_url)
    if out is None:
        return {"ok": False, "error": "discord_enable_failed"}

    backup_codes = [c.get("code") for c in (out.get("backup_codes") or []) if c.get("code")]
    new_token = out.get("token") or token

    update: dict = {
        "two_fa_secret": encrypt(secret),
        "two_fa_backup_codes": backup_codes,
    }
    if new_token != token:
        update["discord_token"] = encrypt(new_token)

    await discords().update_one({"_id": ObjectId(body.account_id)}, {"$set": update})
    return {"ok": True, "secret": secret, "code": code, "backup_codes": backup_codes}


@router.get("/2fa/{account_id}/code")
async def get_two_fa_code(account_id: str) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None or not acc.get("two_fa_secret"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "2FA not set up for this account")
    try:
        secret = decrypt(acc["two_fa_secret"])
    except ValueError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Cannot decrypt 2FA secret"
        )
    return {"code": pyotp.TOTP(secret).now()}
