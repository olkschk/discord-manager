"""Discord account API: bulk-add, delete, assign proxies, validate."""
from __future__ import annotations

import asyncio
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, Form, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.database import discords, mails, private_messages as private_messages_coll, proxies as proxies_coll
from app.models.account import parse_account_line
from app.security import decrypt, encrypt, require_login
from app.services.auth_recovery import (
    extract_token_from_url,
    fetch_latest_link,
    find_and_authorize_ip,
    forgot_password,
    full_verify_account,
    needs_email_verification,
    reset_password_with_token,
)
from app.services.discord_api import build_proxy_url, login_with_password, validate_token
from app.services.imap_client import fetch_latest_html, imap_host_for

VALID_GROUPS = {
    "Massovka1", "Massovka2", "Massovka3",
    "Chatter1", "Chatter2", "Chatter3",
    "Infl1", "Infl2", "Infl3",
}

router = APIRouter(
    prefix="/api/accounts",
    dependencies=[Depends(require_login)],
    tags=["accounts"],
)
logger = logging.getLogger(__name__)


@router.post("/add")
async def add_accounts(
    payload: str = Form(...),
    user: str = Depends(require_login),
) -> dict:
    """Multi-add. Body field `payload` contains one `email:pass:token` per line."""
    # Early sanity check: if the first non-empty line has no '@' in the email
    # position, the user likely pasted proxy lines by mistake.
    first = next((l.strip() for l in payload.splitlines() if l.strip()), "")
    if first:
        first_segment = first.split(":")[0]
        if "@" not in first_segment:
            # Looks like ip:port:... or plain non-email text
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Выглядит как прокси-строки (ip:port:...) — вставь их в раздел Proxies",
            )

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
            "owner": user,
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
async def assign_proxies(user: str = Depends(require_login)) -> dict:
    """For each of the owner's accounts without a proxy, attach an unused proxy
    that also belongs to this owner. Stops when proxies run out."""
    assigned = 0
    cursor = discords().find({"owner": user, "proxy_id": None})
    async for acc in cursor:
        free = await proxies_coll().find_one_and_update(
            {"owner": user, "assigned": False},
            {"$set": {"assigned": True}},
        )
        if free is None:
            break
        await discords().update_one(
            {"_id": acc["_id"]},
            {"$set": {"proxy_id": free["_id"]}},
        )
        assigned += 1

    logger.info("Assigned %d proxies for owner=%s", assigned, user)
    return {"assigned": assigned}


@router.post("/validate-all")
async def validate_all(user: str = Depends(require_login)) -> dict:
    """Validate every account owned by this user that has a proxy."""
    valid, invalid = 0, 0
    cursor = discords().find({"owner": user, "proxy_id": {"$ne": None}})
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
            update["avatar"] = data.get("avatar")
            if not acc.get("name"):
                update["name"] = data.get("global_name") or data.get("username")
        await discords().update_one({"_id": acc["_id"]}, {"$set": update})
        if is_valid:
            valid += 1
        else:
            invalid += 1

    logger.info("Validation done: valid=%d invalid=%d owner=%s", valid, invalid, user)
    return {"valid": valid, "invalid": invalid}


@router.post("/{account_id}/validate")
async def validate_one(
    account_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
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
        update["discord_user_id"] = data.get("id")
        update["avatar"] = data.get("avatar")
        if not acc.get("name"):
            update["name"] = data.get("global_name") or data.get("username")
    await discords().update_one({"_id": acc["_id"]}, {"$set": update})
    return {"valid": is_valid}


@router.patch("/{account_id}/group")
async def set_group(
    account_id: str,
    body: dict,
    user: str = Depends(require_login),
) -> dict:
    group = body.get("group", "Massovka1")
    if group not in VALID_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group")
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    await discords().update_one(
        {"_id": ObjectId(account_id), "owner": user},
        {"$set": {"group": group}},
    )
    return {"ok": True, "group": group}


@router.post("/bulk-group")
async def bulk_set_group(
    body: dict,
    user: str = Depends(require_login),
) -> dict:
    """Set the same group for multiple accounts at once."""
    account_ids: list[str] = body.get("account_ids", [])
    group: str = body.get("group", "Massovka1")
    if group not in VALID_GROUPS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group")
    valid_ids = [ObjectId(aid) for aid in account_ids if ObjectId.is_valid(aid)]
    if not valid_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No valid account IDs")
    result = await discords().update_many(
        {"_id": {"$in": valid_ids}, "owner": user},
        {"$set": {"group": group}},
    )
    return {"ok": True, "updated": result.modified_count, "group": group}


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    user: str = Depends(require_login),
) -> dict:
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    if acc.get("proxy_id"):
        await proxies_coll().update_one(
            {"_id": acc["proxy_id"]}, {"$set": {"assigned": False}}
        )
    email = acc.get("email", "")
    deleted_pms = await private_messages_coll().delete_many({"to": email})
    await discords().delete_one({"_id": ObjectId(account_id)})
    logger.info(
        "Deleted account %s (email=%s, private_messages=%d, owner=%s)",
        account_id, email, deleted_pms.deleted_count, user,
    )
    return {"deleted": True}


@router.get("/{account_id}/credential")
async def get_credential(
    account_id: str,
    field: str = "password",
    user: str = Depends(require_login),
) -> dict:
    """Decrypt and return a single credential field on demand (password or token)."""
    if field not in ("password", "discord_token"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid field")
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        value = decrypt(acc[field])
    except (ValueError, KeyError):
        return {"value": ""}
    return {"value": value}


@router.post("/{account_id}/login-by-mail")
async def login_by_mail(
    account_id: str,
    user: str = Depends(require_login),
) -> dict:
    """Re-login with stored email+password."""
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    try:
        password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Password ciphertext unreadable")

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

    two_fa_secret: str | None = None
    if acc.get("two_fa_secret"):
        try:
            two_fa_secret = decrypt(acc["two_fa_secret"])
        except ValueError:
            pass

    async def _do_login() -> dict | None:
        return await login_with_password(
            acc["email"], password,
            two_fa_secret=two_fa_secret,
            proxy_url=proxy_url,
        )

    out = await _do_login()
    if out is None:
        return {"ok": False, "error": "request_failed"}

    token = out.get("token")
    recovery_steps: list[str] = []

    if not token and needs_email_verification(out):
        recovery_steps.append("verify_email_required")
        await asyncio.sleep(7)
        if not await find_and_authorize_ip(acc["email"], password, proxy_url=proxy_url):
            return {"ok": False, "error": "verify_link_failed", "steps": recovery_steps}
        recovery_steps.append("verify_link_followed")
        retry = await _do_login()
        if retry is None:
            return {"ok": False, "error": "retry_after_verify_failed", "steps": recovery_steps}
        out = retry
        token = out.get("token")

    if not token and out.get("mfa"):
        return {"ok": False, "error": "mfa_required_but_no_secret", "steps": recovery_steps}
    if not token:
        if out.get("captcha_key"):
            return {"ok": False, "error": "captcha_required", "steps": recovery_steps}
        return {"ok": False, "error": "unknown"}

    await discords().update_one(
        {"_id": ObjectId(account_id)},
        {"$set": {"discord_token": encrypt(token), "token_valid": True}},
    )
    logger.info("Re-logged in %s (steps=%s, owner=%s)", acc.get("email"), recovery_steps, user)
    return {"ok": True, "steps": recovery_steps}


@router.post("/{account_id}/reset-password")
async def reset_password_endpoint(
    account_id: str,
    body: dict,
    user: str = Depends(require_login),
) -> dict:
    """Trigger Discord password reset via email link."""
    new_password = (body or {}).get("new_password", "")
    if not new_password or len(new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "new_password must be at least 8 chars")
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")

    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    try:
        old_password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Mail password ciphertext unreadable")

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

    link: str | None = None
    for _ in range(6):
        await asyncio.sleep(7)
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
    await discords().update_one({"_id": ObjectId(account_id)}, {"$set": {"password": enc_new}})
    await mails().update_one({"email": acc["email"]}, {"$set": {"password": enc_new}})

    new_token = out.get("token") if isinstance(out, dict) else None
    if new_token:
        await discords().update_one(
            {"_id": ObjectId(account_id)},
            {"$set": {"discord_token": encrypt(new_token), "token_valid": True}},
        )

    logger.info("Reset password for %s (owner=%s)", acc.get("email"), user)
    return {"ok": True, "rotated_token": bool(new_token)}


@router.post("/{account_id}/verify-email")
async def verify_email_endpoint(
    account_id: str,
    user: str = Depends(require_login),
) -> dict:
    """Handle the 'Verification Required' Discord screen."""
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    try:
        token = decrypt(acc["discord_token"])
        password = decrypt(acc["password"])
    except ValueError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Cannot decrypt credentials")

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

    new_token = await full_verify_account(token, acc["email"], password, proxy_url=proxy_url)
    if not new_token:
        return {"ok": False, "error": "verify_failed — check IMAP or try Inbox to see the email"}

    if new_token != token:
        await discords().update_one(
            {"_id": ObjectId(account_id)},
            {"$set": {"discord_token": encrypt(new_token), "token_valid": True}},
        )
    logger.info("Email verified for %s (owner=%s)", acc.get("email"), user)
    return {"ok": True}


@router.get("/{account_id}/inbox/latest-html")
async def get_latest_email_html(
    account_id: str,
    only_discord: bool = False,
    user: str = Depends(require_login),
) -> dict:
    """Fetch the newest email from the account's mailbox and return full HTML."""
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id), "owner": user})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    mail_doc = await mails().find_one({"email": acc["email"]})
    enc_pw = (mail_doc or acc).get("password")
    try:
        password = decrypt(enc_pw)
    except (ValueError, TypeError):
        return {"ok": False, "error": "password_unreadable"}

    if imap_host_for(acc["email"]) is None:
        return {"ok": False, "error": "no_imap_host"}

    import imaplib
    try:
        result = await fetch_latest_html(acc["email"], password, only_discord=only_discord)
    except imaplib.IMAP4.error as exc:
        return {"ok": False, "error": f"imap_login_failed: {exc}"}
    except OSError as exc:
        return {"ok": False, "error": f"imap_network: {exc}"}

    if result is None:
        return {"ok": False, "error": "no_emails_found"}
    return {"ok": True, **result}
