"""Utils: AI identity, join server, 2FA setup + code retrieval."""
from __future__ import annotations

import logging
import random

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
    set_custom_status,
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
async def change_identity(
    body: IdentityBody,
    user: str = Depends(require_login),
) -> dict:
    """Generate a fresh AI identity for each account and PATCH it to Discord (up to 5 concurrent)."""
    import asyncio

    sem = asyncio.Semaphore(5)
    results: list[dict] = []

    async def _process(acc_id: str) -> dict:
        async with sem:
            resolved = await load_account_token_and_proxy(acc_id, owner=user)
            if resolved is None:
                return {"account_id": acc_id, "ok": False, "error": "unreadable"}
            acc, token, proxy_url = resolved
            try:
                password = decrypt(acc["password"])
            except (ValueError, KeyError):
                password = None
            await gateway_pool.get_or_create(acc_id)
            try:
                ident = await generate_identity()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Identity gen failed for %s", acc_id)
                return {"account_id": acc_id, "ok": False, "error": f"ai:{exc.__class__.__name__}"}
            out = await patch_profile(
                token,
                username=ident["username"],
                global_name=ident["global_name"] or None,
                bio=ident["bio"] or None,
                password=password,
                proxy_url=proxy_url,
            )
            if out is None:
                return {"account_id": acc_id, "ok": False, "error": "discord_patch_failed", "identity": ident}
            await discords().update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"username": ident["username"], "name": ident["global_name"], "bio": ident["bio"]}},
            )
            return {"account_id": acc_id, "ok": True, "identity": ident}

    results = list(await asyncio.gather(*(_process(a) for a in body.account_ids)))
    return {"results": results}


# ── Custom identity (manual fields) ─────────────────────────────────────────
class CheckUsernameBody(BaseModel):
    account_id: str
    username: str = Field(..., min_length=2, max_length=32)


@router.post("/identity/check-username")
async def check_username_endpoint(
    body: CheckUsernameBody,
    user: str = Depends(require_login),
) -> dict:
    """POST /users/@me/pomelo-attempt — returns {taken: bool}."""
    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
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
    avatar_base64: str | None = None
    custom_status: str | None = Field(None, max_length=128)


@router.post("/identity/custom")
async def set_custom_identity(
    body: CustomIdentityBody,
    user: str = Depends(require_login),
) -> dict:
    """Apply manually-entered identity fields to a single account."""
    has_profile = body.username or body.global_name is not None or body.bio is not None or body.avatar_base64
    has_status = body.custom_status is not None
    if not has_profile and not has_status:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one field required")

    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found or token unreadable")
    acc, token, proxy_url = resolved

    # Profile fields (username / display name / bio / avatar)
    if has_profile:
        try:
            password = decrypt(acc["password"])
        except (ValueError, KeyError):
            password = None

        conn = await gateway_pool.get_or_create(body.account_id)
        if conn is None:
            logger.warning("set_custom_identity: no gateway for %s — trying anyway", body.account_id)

        out = await patch_profile(
            token,
            username=body.username or None,
            global_name=body.global_name,
            bio=body.bio,
            avatar_base64=body.avatar_base64,
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
        if isinstance(out, dict) and out.get("avatar"):
            update["avatar"] = out["avatar"]
        if update:
            await discords().update_one({"_id": ObjectId(body.account_id)}, {"$set": update})

    # Custom status text (settings-proto endpoint)
    if has_status:
        current_status = acc.get("status", "online")
        res = await set_custom_status(token, body.custom_status or "", status=current_status, proxy_url=proxy_url)
        if not res.get("ok"):
            return {"ok": False, "error": "custom_status_failed"}

    return {"ok": True}


# ── Join server ──────────────────────────────────────────────────────────────
class JoinServerBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    invite: str


@router.post("/join-server")
async def join_server(
    body: JoinServerBody,
    user: str = Depends(require_login),
) -> dict:
    code = body.invite.strip().rstrip("/").rsplit("/", 1)[-1]
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty invite code")

    results: list[dict] = []
    for acc_id in body.account_ids:
        resolved = await load_account_token_and_proxy(acc_id, owner=user)
        if resolved is None:
            results.append({"account_id": acc_id, "ok": False, "error": "unreadable"})
            continue
        acc, token, proxy_url = resolved
        logger.info("join_server acc=%s has_proxy=%s", acc_id, bool(proxy_url))

        async def _try_join(tok: str) -> dict | None:
            c = await gateway_pool.get_or_create(acc_id)
            return await join_invite(tok, code, session_id=c.session_id if c else None, proxy_url=proxy_url)

        async def _run_verify_and_retry(tok: str, reason: str) -> tuple[str, dict | None]:
            logger.info("join_server: %s for %s — running email verification", reason, acc_id)
            try:
                mail_password = decrypt(acc["password"])
            except (ValueError, KeyError):
                return tok, None
            new_tok = await full_verify_account(tok, acc["email"], mail_password, proxy_url=proxy_url)
            if not new_tok:
                return tok, None
            if new_tok != tok:
                await discords().update_one(
                    {"_id": ObjectId(acc_id)},
                    {"$set": {"discord_token": encrypt(new_tok), "token_valid": True}},
                )
            return new_tok, await _try_join(new_tok)

        out = await _try_join(token)

        if isinstance(out, dict) and out.get("_needs_verification"):
            token, out = await _run_verify_and_retry(token, "40002 _needs_verification")
        elif not (bool(out) and not isinstance(out, dict)):
            needs_verify = await check_needs_verification(token, proxy_url=proxy_url)
            if needs_verify:
                token, out = await _run_verify_and_retry(token, "post-failure eligibility check")

        if out is None:
            resend_needed = await check_needs_verification(token, proxy_url=proxy_url)
            if resend_needed:
                token, out = await _run_verify_and_retry(token, "resend fallback")

        ok = bool(out) and not (isinstance(out, dict) and out.get("_needs_verification"))
        if ok:
            await discords().update_one({"_id": ObjectId(acc_id)}, {"$set": {"joined_server": True}})
        results.append({"account_id": acc_id, "ok": ok})

    return {"results": results}


class MarkJoinedBody(BaseModel):
    account_id: str


@router.post("/mark-joined")
async def mark_joined(
    body: MarkJoinedBody,
    user: str = Depends(require_login),
) -> dict:
    """Manually set joined_server=True without actually joining Discord."""
    if not ObjectId.is_valid(body.account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    res = await discords().update_one(
        {"_id": ObjectId(body.account_id), "owner": user},
        {"$set": {"joined_server": True}},
    )
    if res.matched_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return {"ok": True}


# ── 2FA ──────────────────────────────────────────────────────────────────────
class TwoFASetupBody(BaseModel):
    account_id: str


@router.post("/2fa/setup")
async def setup_two_fa(
    body: TwoFASetupBody,
    user: str = Depends(require_login),
) -> dict:
    """Generate a TOTP secret, enable MFA on Discord, persist secret + backup codes."""
    resolved = await load_account_token_and_proxy(body.account_id, owner=user)
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
from app.services.activity_templates import (  # noqa: E402
    GAME_NAMES, GAMES, SPECIAL_ACTIVITIES, SPECIAL_NAMES,
    build_game_activity, build_random_activity,
)


class ActivityBody(BaseModel):
    account_ids: list[str] = Field(..., min_length=1)
    mode: str = Field("random")
    game_name: str | None = None
    start_offset_min: float | None = Field(None, ge=0, le=180)
    start_offset_max: float | None = Field(None, ge=0, le=180)


def _random_offset_ms(body: ActivityBody) -> int | None:
    """Pick a random offset in ms from the user-supplied minute range, or None for default."""
    if body.start_offset_min is None and body.start_offset_max is None:
        return None
    lo = int((body.start_offset_min or 0) * 60_000)
    hi = int((body.start_offset_max or body.start_offset_min or 0) * 60_000)
    if lo > hi:
        lo, hi = hi, lo
    return random.randint(lo, hi)


@router.get("/activity/templates")
async def get_activity_templates() -> dict:
    return {"games": GAME_NAMES, "specials": SPECIAL_NAMES}


@router.post("/activity")
async def set_activity(body: ActivityBody) -> dict:
    """Set a game activity (specific or random) on N accounts."""
    results: list[dict] = []
    for acc_id in body.account_ids:
        start_ms = _random_offset_ms(body)
        if body.mode == "special":
            special = SPECIAL_ACTIVITIES.get(body.game_name)
            act = await build_game_activity(special, start_offset_ms=start_ms)
        elif body.mode == "game":
            game = next((g for g in GAMES if g["name"] == body.game_name), None)
            act = await build_game_activity(game, start_offset_ms=start_ms)
        else:
            act = await build_random_activity(start_offset_ms=start_ms)
        ok = await gateway_pool.set_activity(acc_id, activity=act)
        if ok and body.mode == "random":
            gateway_pool.start_activity_rotation(acc_id, start_offset_ms=start_ms)
        else:
            gateway_pool.stop_activity_rotation(acc_id)
        results.append({"account_id": acc_id, "ok": ok, "activity_name": act.get("name")})
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


class SetStatusBody(BaseModel):
    account_id: str
    status: str = Field(..., pattern="^(online|idle|dnd|invisible)$")


@router.post("/status")
async def set_status(body: SetStatusBody) -> dict:
    """Set presence status (online/idle/dnd/invisible) via gateway PRESENCE_UPDATE."""
    if not ObjectId.is_valid(body.account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    ok = await gateway_pool.set_status(body.account_id, body.status)
    if not ok:
        return {"ok": False, "error": "gateway_unavailable"}
    return {"ok": True}


@router.get("/2fa/{account_id}/code")
async def get_two_fa_code(
    account_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None or not acc.get("two_fa_secret"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "2FA not set up for this account")
    try:
        secret = decrypt(acc["two_fa_secret"])
    except ValueError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Cannot decrypt 2FA secret"
        )
    return {"code": pyotp.TOTP(secret).now()}
