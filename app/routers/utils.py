"""Utils: AI identity, join server, 2FA setup + code retrieval."""
from __future__ import annotations

import logging

import pyotp
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database import discords
from app.security import decrypt, encrypt, require_login
from app.services import gateway_pool
from app.services.account_helpers import load_account_token_and_proxy
from app.services.ai_client import generate_identity
from app.services.auth_recovery import full_verify_account
from app.services.discord_api import (
    check_needs_verification,
    check_username as _check_username,
    enable_mfa,
    join_invite,
    patch_profile,
)

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
        acc, token, proxy_url = resolved

        # Password is required by Discord when changing username
        try:
            password = decrypt(acc["password"])
        except (ValueError, KeyError):
            password = None

        # Discord requires an active gateway session for profile edits (10020 Unknown Session).
        # Connect (or reuse existing connection) before making the PATCH request.
        conn = await gateway_pool.get_or_create(acc_id)
        if conn is None:
            logger.warning("change_identity: could not open gateway for %s — trying anyway", acc_id)

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
            password=password,
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


# ── Custom identity (manual fields) ─────────────────────────────────────────
class CheckUsernameBody(BaseModel):
    account_id: str
    username: str = Field(..., min_length=2, max_length=32)


@router.post("/identity/check-username")
async def check_username_endpoint(body: CheckUsernameBody) -> dict:
    """POST /users/@me/pomelo-attempt — returns {taken: bool}."""
    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    _, token, proxy_url = resolved
    result = await _check_username(token, body.username, proxy_url=proxy_url)
    return result


class CustomIdentityBody(BaseModel):
    account_id: str
    username: str | None = Field(None, min_length=2, max_length=32)
    global_name: str | None = Field(None, max_length=32)
    bio: str | None = Field(None, max_length=190)


@router.post("/identity/custom")
async def set_custom_identity(body: CustomIdentityBody) -> dict:
    """Apply manually-entered identity fields to a single account."""
    if not body.username and body.global_name is None and body.bio is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one field required")

    resolved = await load_account_token_and_proxy(body.account_id)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    acc, token, proxy_url = resolved

    try:
        password = decrypt(acc["password"])
    except (ValueError, KeyError):
        password = None

    # Ensure a gateway session exists before patching (Discord requires it).
    conn = await gateway_pool.get_or_create(body.account_id)
    if conn is None:
        logger.warning("set_custom_identity: no gateway for %s — trying anyway", body.account_id)

    out = await patch_profile(
        token,
        username=body.username or None,
        global_name=body.global_name,
        bio=body.bio,
        password=password,
        proxy_url=proxy_url,
    )
    if out is None:
        return {"ok": False, "error": "discord_patch_failed"}

    update: dict = {}
    if body.username:
        update["username"] = body.username
    if body.global_name is not None:
        update["name"] = body.global_name
    if body.bio is not None:
        update["bio"] = body.bio
    if update:
        await discords().update_one({"_id": ObjectId(body.account_id)}, {"$set": update})

    return {"ok": True}


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
        acc, token, proxy_url = resolved
        logger.info("join_server acc=%s has_proxy=%s proxy_url_prefix=%s", acc_id, bool(proxy_url), (proxy_url or "")[:30])

        async def _try_join(tok: str) -> dict | None:
            c = await gateway_pool.get_or_create(acc_id)
            return await join_invite(tok, code, session_id=c.session_id if c else None, proxy_url=proxy_url)

        out = await _try_join(token)

        # 40002 — account needs email verification; verify via IMAP then retry
        if isinstance(out, dict) and out.get("_needs_verification"):
            logger.info("join_server: account %s needs email verification (40002)", acc_id)
            try:
                mail_password = decrypt(acc["password"])
            except (ValueError, KeyError):
                results.append({"account_id": acc_id, "ok": False, "error": "password_unreadable"})
                continue
            new_token = await full_verify_account(token, acc["email"], mail_password, proxy_url=proxy_url)
            if not new_token:
                results.append({"account_id": acc_id, "ok": False, "error": "verify_failed"})
                continue
            if new_token != token:
                await discords().update_one(
                    {"_id": ObjectId(acc_id)},
                    {"$set": {"discord_token": encrypt(new_token), "token_valid": True}},
                )
                token = new_token
            out = await _try_join(token)

        ok = bool(out) and not (isinstance(out, dict) and out.get("_needs_verification"))
        if ok:
            await discords().update_one({"_id": ObjectId(acc_id)}, {"$set": {"joined_server": True}})
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


# ── Activity (gateway PRESENCE_UPDATE) ──────────────────────────────────────
class ActivityBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    activity_type: int = Field(0, ge=0, le=5)  # 0=Playing,1=Streaming,2=Listening,3=Watching,5=Competing
    activity_name: str = Field(..., min_length=1, max_length=128)


@router.post("/activity")
async def set_activity(body: ActivityBody) -> dict:
    """Set the same activity on N accounts via persistent gateway connections."""
    results: list[dict] = []
    for acc_id in body.account_ids:
        ok = await gateway_pool.set_activity(acc_id, body.activity_type, body.activity_name)
        results.append({"account_id": acc_id, "ok": ok})
    return {"results": results}


class ClearActivityBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)


@router.post("/activity/clear")
async def clear_activity(body: ClearActivityBody) -> dict:
    results: list[dict] = []
    for acc_id in body.account_ids:
        ok = await gateway_pool.clear_activity(acc_id)
        results.append({"account_id": acc_id, "ok": ok})
    return {"results": results}


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
