"""Discord account API: bulk-add, delete, assign proxies, validate."""
from __future__ import annotations

import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, Form, HTTPException, status
from pymongo.errors import DuplicateKeyError

import asyncio

from app.database import discords, mails, proxies as proxies_coll
from app.models.account import parse_account_line
from app.security import decrypt, encrypt, require_login
from app.services.auth_recovery import (
    fetch_latest_link,
    follow_verify_link,
    forgot_password,
    needs_email_verification,
    reset_password_with_token,
    extract_token_from_url,
)
from app.services.discord_api import build_proxy_url, login_with_password, mfa_totp, validate_token

router = APIRouter(
    prefix="/api/accounts",
    dependencies=[Depends(require_login)],
    tags=["accounts"],
)
logger = logging.getLogger(__name__)


@router.post("/add")
async def add_accounts(payload: str = Form(...)) -> dict:
    """Multi-add. Body field `payload` contains one `mail:pass:token` per line."""
    added, skipped = 0, 0
    errors: list[str] = []

    for raw_line in payload.splitlines():
        parsed = parse_account_line(raw_line)
        if parsed is None:
            if raw_line.strip():
                errors.append(f"Bad format: {raw_line[:60]!r}")
                skipped += 1
            continue

        email, password, token = parsed
        enc_pw = encrypt(password)
        enc_token = encrypt(token)

        doc = {
            "email": email,
            "password": enc_pw,
            "discord_token": enc_token,
            "proxy_id": None,
            "two_fa_backup_codes": None,
            "two_fa_secret": None,
            "token_valid": False,
            "joined_voice": False,
            "joined_stream": False,
            "joined_server": False,
            "name": None,
            "username": None,
            "bio": None,
        }
        try:
            await discords().insert_one(doc)
            await mails().update_one(
                {"email": email},
                {"$setOnInsert": {"email": email, "password": enc_pw}},
                upsert=True,
            )
            added += 1
        except DuplicateKeyError:
            errors.append(f"Duplicate email: {email}")
            skipped += 1
        except Exception as exc:  # noqa: BLE001 — surface unknown errors per-line
            logger.exception("Failed to insert account %s", email)
            errors.append(f"{email}: {exc.__class__.__name__}")
            skipped += 1

    return {"added": added, "skipped": skipped, "errors": errors}


@router.post("/assign-proxies")
async def assign_proxies() -> dict:
    """For each account without a proxy, attach an unused one. Stops when proxies run out."""
    assigned = 0
    cursor = discords().find({"proxy_id": None})
    async for acc in cursor:
        free = await proxies_coll().find_one_and_update(
            {"assigned": False},
            {"$set": {"assigned": True}},
        )
        if free is None:
            break
        await discords().update_one(
            {"_id": acc["_id"]},
            {"$set": {"proxy_id": free["_id"]}},
        )
        assigned += 1

    logger.info("Assigned %d proxies", assigned)
    return {"assigned": assigned}


@router.post("/validate-all")
async def validate_all() -> dict:
    """Validate every account that has a proxy. Updates `token_valid`."""
    valid, invalid = 0, 0
    cursor = discords().find({"proxy_id": {"$ne": None}})
    async for acc in cursor:
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        proxy_url: str | None = None
        if proxy is not None:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                logger.warning("Bad proxy ciphertext for proxy %s", proxy.get("_id"))

        try:
            token = decrypt(acc["discord_token"])
        except ValueError:
            await discords().update_one({"_id": acc["_id"]}, {"$set": {"token_valid": False}})
            invalid += 1
            continue

        is_valid, data = await validate_token(token, proxy_url)
        update: dict = {"token_valid": is_valid}
        if is_valid and data:
            update["username"] = data.get("username")
            update["discord_user_id"] = data.get("id")
            if not acc.get("name"):
                update["name"] = data.get("global_name") or data.get("username")
        await discords().update_one({"_id": acc["_id"]}, {"$set": update})
        if is_valid:
            valid += 1
        else:
            invalid += 1

    logger.info("Validation done: valid=%d invalid=%d", valid, invalid)
    return {"valid": valid, "invalid": invalid}


@router.post("/{account_id}/validate")
async def validate_one(account_id: str) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy is not None:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    try:
        token = decrypt(acc["discord_token"])
    except ValueError:
        await discords().update_one({"_id": acc["_id"]}, {"$set": {"token_valid": False}})
        return {"valid": False, "reason": "ciphertext"}

    is_valid, data = await validate_token(token, proxy_url)
    update: dict = {"token_valid": is_valid}
    if is_valid and data:
        update["username"] = data.get("username")
        if not acc.get("name"):
            update["name"] = data.get("global_name") or data.get("username")
    await discords().update_one({"_id": acc["_id"]}, {"$set": update})
    return {"valid": is_valid}


@router.delete("/{account_id}")
async def delete_account(account_id: str) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    if acc.get("proxy_id"):
        await proxies_coll().update_one(
            {"_id": acc["proxy_id"]}, {"$set": {"assigned": False}}
        )
    await discords().delete_one({"_id": ObjectId(account_id)})
    logger.info("Deleted account %s (email=%s)", account_id, acc.get("email"))
    return {"deleted": True}


@router.post("/{account_id}/login-by-mail")
async def login_by_mail(account_id: str) -> dict:
    """Re-login with stored email+password. If MFA is challenged and we hold the
    2FA secret, complete the TOTP exchange. Captcha + email-verification flows
    are *not* implemented — those return an error and require manual recovery.
    """
    import pyotp

    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    try:
        password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Password ciphertext unreadable"
        )

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy is not None:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    async def _do_login() -> dict | None:
        return await login_with_password(acc["email"], password, proxy_url=proxy_url)

    out = await _do_login()
    if out is None:
        return {"ok": False, "error": "request_failed"}

    token = out.get("token")
    recovery_steps: list[str] = []

    # New-device email verification — fetch the link Discord mailed and GET it.
    if not token and needs_email_verification(out):
        recovery_steps.append("verify_email_required")
        link = await fetch_latest_link(acc["email"], password, must_contain="authorize-ip")
        if not link:
            # Fallback: search for any Discord click link
            link = await fetch_latest_link(acc["email"], password)
        if not link:
            return {"ok": False, "error": "verify_email_link_not_found", "steps": recovery_steps}
        recovery_steps.append(f"verify_link_found: {link[:60]}")
        # Follow the click.discord.com redirect, extract token, POST /auth/authorize-ip
        if not await follow_verify_link(link, proxy_url=proxy_url):
            return {"ok": False, "error": "verify_link_failed", "steps": recovery_steps}
        recovery_steps.append("verify_link_followed")
        retry = await _do_login()
        if retry is None:
            return {"ok": False, "error": "retry_after_verify_failed", "steps": recovery_steps}
        out = retry
        token = out.get("token")

    if not token and out.get("mfa"):
        ticket = out.get("ticket")
        if not ticket:
            return {"ok": False, "error": "mfa_no_ticket", "steps": recovery_steps}
        if not acc.get("two_fa_secret"):
            return {"ok": False, "error": "mfa_required_but_no_secret", "steps": recovery_steps}
        try:
            secret = decrypt(acc["two_fa_secret"])
        except ValueError:
            return {"ok": False, "error": "two_fa_secret_unreadable", "steps": recovery_steps}
        code = pyotp.TOTP(secret).now()
        mfa_out = await mfa_totp(
            ticket, code,
            login_instance_id=out.get("login_instance_id"),
            proxy_url=proxy_url,
        )
        if mfa_out is None:
            return {"ok": False, "error": "mfa_failed", "steps": recovery_steps}
        token = mfa_out.get("token")

    if not token:
        if out.get("captcha_key"):
            return {"ok": False, "error": "captcha_required", "steps": recovery_steps}
        return {"ok": False, "error": "unknown"}

    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"discord_token": encrypt(token), "token_valid": True}},
    )
    logger.info("Re-logged in %s via /auth/login (steps=%s)", acc.get("email"), recovery_steps)
    return {"ok": True, "steps": recovery_steps}


@router.post("/{account_id}/reset-password")
async def reset_password_endpoint(account_id: str, body: dict) -> dict:
    """Trigger Discord password reset via email link.

    Body: {"new_password": str}.
    Flow: POST /auth/forgot → wait → IMAP fetch reset link → extract token →
    POST /auth/reset → re-encrypt new password into mails + discords.
    """
    new_password = (body or {}).get("new_password", "")
    if not new_password or len(new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "new_password must be at least 8 chars")
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")

    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    try:
        old_password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Mail password ciphertext unreadable"
        )

    proxy_url: str | None = None
    if acc.get("proxy_id"):
        proxy = await proxies_coll().find_one({"_id": acc["proxy_id"]})
        if proxy is not None:
            try:
                proxy_url = build_proxy_url(
                    proxy["ip"], proxy["port"], proxy["login"], decrypt(proxy["password"])
                )
            except ValueError:
                proxy_url = None

    if not await forgot_password(acc["email"], proxy_url=proxy_url):
        return {"ok": False, "error": "forgot_request_failed"}

    # Discord typically delivers the reset email within seconds. Poll IMAP
    # a few times to keep the operator from racing the email.
    link: str | None = None
    for _ in range(6):
        await asyncio.sleep(5)
        link = await fetch_latest_link(acc["email"], old_password, must_contain="reset")
        if link:
            break
    if not link:
        return {"ok": False, "error": "reset_link_not_found"}

    token = extract_token_from_url(link)
    if not token:
        return {"ok": False, "error": "reset_token_not_in_link", "link": link[:80]}

    out = await reset_password_with_token(token, new_password, proxy_url=proxy_url)
    if out is None:
        return {"ok": False, "error": "reset_request_failed"}

    enc_new = encrypt(new_password)
    await discords().update_one(
        {"_id": ObjectId(account_id)}, {"$set": {"password": enc_new}}
    )
    await mails().update_one(
        {"email": acc["email"]}, {"$set": {"password": enc_new}}
    )

    new_token = out.get("token") if isinstance(out, dict) else None
    if new_token:
        await discords().update_one(
            {"_id": ObjectId(account_id)},
            {"$set": {"discord_token": encrypt(new_token), "token_valid": True}},
        )

    logger.info("Reset password for %s", acc.get("email"))
    return {"ok": True, "rotated_token": bool(new_token)}
