"""Discord account API: bulk-add, delete, assign proxies, validate."""
from __future__ import annotations

import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, Form, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.database import discords, mails, proxies as proxies_coll
from app.models.account import parse_account_line
from app.security import decrypt, encrypt, require_login
from app.services.discord_api import build_proxy_url, validate_token

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
