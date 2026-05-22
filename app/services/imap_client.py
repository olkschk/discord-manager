"""IMAP reader for Discord verification emails.

Built-in support for Rambler (all properties → imap.rambler.ru:993). Other
domains fall through to `IMAP_DEFAULT_HOST` if set, else fail with a clear error.

The fetcher is sync (`imaplib`) but every public entry point wraps it in
`asyncio.to_thread` so it never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from typing import TypedDict

from app.config import get_settings

logger = logging.getLogger(__name__)


# Domain → (host, port). Add new providers here.
PROVIDER_HOSTS: dict[str, tuple[str, int]] = {
    # Rambler family — all share imap.rambler.ru
    "rambler.ru": ("imap.rambler.ru", 993),
    "myrambler.ru": ("imap.rambler.ru", 993),
    "autorambler.ru": ("imap.rambler.ru", 993),
    "lenta.ru": ("imap.rambler.ru", 993),
    "ro.ru": ("imap.rambler.ru", 993),
    "rambler.com": ("imap.rambler.ru", 993),
    "rambler.org": ("imap.rambler.ru", 993),
    # mail.com family — all share imap.mail.com
    "mail.com": ("imap.mail.com", 993),
    "email.com": ("imap.mail.com", 993),
    "mail.de": ("imap.mail.com", 993),
    "mail2you.com": ("imap.mail.com", 993),
    # GMX family — all share imap.gmx.com
    "gmx.com": ("imap.gmx.com", 993),
    "gmx.net": ("imap.gmx.com", 993),
    "gmx.de": ("imap.gmx.com", 993),
    "gmx.at": ("imap.gmx.com", 993),
    "gmx.ch": ("imap.gmx.com", 993),
    "gmx.us": ("imap.gmx.com", 993),
    "gmx.co.uk": ("imap.gmx.com", 993),
    # FirstMail
    "firstmail.ltd": ("mail.firstmail.ltd", 993),
}

# Code-extraction patterns. Run in order; first match wins.
_CODE_PATTERNS = (
    re.compile(r"verification\s+code[^0-9]{0,40}(\d{6,8})", re.IGNORECASE),
    re.compile(r"\bcode[:\s]+(\d{6,8})\b", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
)

# Discord verification links come from click.discord.com or discord.com directly.
_LINK_PATTERN = re.compile(r"https?://(?:click\.|)discord(?:app)?\.com/[^\s\"'>]+")


class InboxEntry(TypedDict, total=False):
    from_: str
    subject: str
    date: str
    snippet: str
    code: str | None
    link: str | None


def imap_host_for(email_address: str) -> tuple[str, int] | None:
    """Resolve (host, port) for the email's domain. Built-in providers first,
    then `IMAP_DEFAULT_HOST` from env, else None."""
    if "@" not in email_address:
        return None
    domain = email_address.split("@", 1)[1].lower().strip()
    if domain in PROVIDER_HOSTS:
        return PROVIDER_HOSTS[domain]
    settings = get_settings()
    if settings.imap_default_host:
        return (settings.imap_default_host, settings.imap_default_port)
    return None


# ── Decoding helpers ────────────────────────────────────────────────────────
def _decode_header(raw: str | None) -> str:
    if raw is None:
        return ""
    parts: list[str] = []
    for text, charset in decode_header(raw):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                parts.append(text.decode("utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts)


def _extract_text(msg: Message) -> str:
    """Best-effort plain-text extraction. Strips HTML tags as a fallback."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    html = payload.decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        text = payload.decode("utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def _extract_html(msg: Message) -> str:
    """Return the raw HTML body of the email (for full rendering)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    return payload.decode("utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", errors="replace")
    # Fall back to plain text wrapped in pre
    return f"<pre>{_extract_text(msg)}</pre>"


def _fetch_latest_html_sync(
    email_address: str,
    password: str,
    host: str,
    port: int,
    *,
    only_discord: bool,
    timeout: int,
) -> dict | None:
    """Fetch the newest email and return its HTML body + metadata."""
    conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(email_address, password)
        conn.select("INBOX", readonly=True)
        if only_discord:
            typ, data = conn.search(None, "FROM", "discord")
        else:
            typ, data = conn.search(None, "ALL")
        if typ != "OK":
            return None
        ids = (data[0] or b"").split()
        if not ids:
            return None
        raw_id = ids[-1]  # newest
        typ2, msg_data = conn.fetch(raw_id, "(RFC822)")
        if typ2 != "OK" or not msg_data:
            return None
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2:
                msg = email.message_from_bytes(part[1])
                return {
                    "from_": parseaddr(msg.get("From", ""))[1] or msg.get("From", ""),
                    "subject": _decode_header(msg.get("Subject")),
                    "date": msg.get("Date", ""),
                    "html": _extract_html(msg),
                }
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return None


async def fetch_latest_html(
    email_address: str,
    password: str,
    *,
    only_discord: bool = False,
) -> dict | None:
    """Fetch the newest email and return its full HTML body. Async wrapper."""
    settings = get_settings()
    host_port = imap_host_for(email_address)
    if host_port is None:
        return None
    host, port = host_port
    return await asyncio.to_thread(
        _fetch_latest_html_sync,
        email_address, password, host, port,
        only_discord=only_discord,
        timeout=settings.imap_timeout,
    )


def extract_code(body: str) -> str | None:
    for pat in _CODE_PATTERNS:
        m = pat.search(body)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    return None


def extract_link(body: str) -> str | None:
    m = _LINK_PATTERN.search(body)
    return m.group(0) if m else None


# ── IMAP fetch (sync, called via asyncio.to_thread) ─────────────────────────
def _fetch_sync(
    email_address: str,
    password: str,
    host: str,
    port: int,
    *,
    limit: int,
    only_discord: bool,
    timeout: int,
) -> list[InboxEntry]:
    out: list[InboxEntry] = []
    conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(email_address, password)
        conn.select("INBOX", readonly=True)

        if only_discord:
            typ, data = conn.search(None, "FROM", "discord")
        else:
            typ, data = conn.search(None, "ALL")
        if typ != "OK":
            return out

        ids = (data[0] or b"").split()
        # newest first → reversed last `limit`
        for raw_id in reversed(ids[-limit:]):
            typ, msg_data = conn.fetch(raw_id, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            for part in msg_data:
                if not (isinstance(part, tuple) and len(part) >= 2):
                    continue
                msg = email.message_from_bytes(part[1])
                from_addr = parseaddr(msg.get("From", ""))[1] or _decode_header(msg.get("From"))
                subject = _decode_header(msg.get("Subject"))
                date = msg.get("Date", "")
                body = _extract_text(msg)
                snippet = re.sub(r"\s+", " ", body).strip()[:280]
                out.append(
                    {
                        "from_": from_addr,
                        "subject": subject,
                        "date": date,
                        "snippet": snippet,
                        "code": extract_code(body),
                        "link": extract_link(body),
                    }
                )
                break
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return out


async def fetch_recent(
    email_address: str,
    password: str,
    *,
    limit: int | None = None,
    only_discord: bool = True,
) -> list[InboxEntry]:
    """Fetch up to `limit` recent inbox entries (newest first). Raises on
    transport / auth failure so the caller can map to specific errors."""
    settings = get_settings()
    host_port = imap_host_for(email_address)
    if host_port is None:
        raise RuntimeError(f"No IMAP host known for {email_address!r}")
    host, port = host_port

    return await asyncio.to_thread(
        _fetch_sync,
        email_address,
        password,
        host,
        port,
        limit=limit if limit is not None else settings.imap_fetch_limit,
        only_discord=only_discord,
        timeout=settings.imap_timeout,
    )
