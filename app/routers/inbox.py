"""Account inbox view — fetch recent Discord-verification emails over IMAP."""
from __future__ import annotations

import imaplib
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.database import discords, mails
from app.security import decrypt, require_login
from app.services.imap_client import fetch_recent, imap_host_for

router = APIRouter(
    prefix="/api/accounts",
    dependencies=[Depends(require_login)],
    tags=["inbox"],
)
logger = logging.getLogger(__name__)


@router.get("/{account_id}/inbox")
async def get_inbox(
    account_id: str,
    limit: int = Query(10, ge=1, le=50),
    only_discord: bool = Query(True),
) -> dict:
    """Read the most recent inbox entries for the email tied to this Discord
    account. Returns parsed Discord verification codes / links when present.

    Errors are returned in-band (`{ok: False, error: "..."}`) rather than as
    HTTP errors so the UI can display them inline without bespoke handling.
    """
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid account id")
    acc = await discords().find_one({"_id": ObjectId(account_id)})
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")

    email_address = acc["email"]

    # Prefer the dedicated `mails` row (canonical), fall back to the discord doc.
    mail_doc = await mails().find_one({"email": email_address})
    enc_pw = (mail_doc or acc).get("password")
    if not enc_pw:
        return {"ok": False, "error": "no_password_stored"}
    try:
        password = decrypt(enc_pw)
    except (ValueError, TypeError):
        return {"ok": False, "error": "password_unreadable"}

    if imap_host_for(email_address) is None:
        domain = email_address.split("@", 1)[1] if "@" in email_address else ""
        return {"ok": False, "error": "no_imap_host", "domain": domain}

    try:
        entries = await fetch_recent(
            email_address, password, limit=limit, only_discord=only_discord
        )
    except imaplib.IMAP4.error as exc:
        logger.warning("IMAP login/fetch failed for %s: %s", email_address, exc)
        return {"ok": False, "error": f"imap_login_failed: {exc}"}
    except OSError as exc:
        logger.warning("IMAP network error for %s: %s", email_address, exc)
        return {"ok": False, "error": f"imap_network: {exc}"}

    return {"ok": True, "entries": entries}
