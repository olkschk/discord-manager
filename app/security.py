"""Encryption, password hashing, and session helpers."""
from __future__ import annotations

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status

from app.config import get_settings

_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_settings().encryption_key.encode())
    return _fernet


def encrypt(value: str) -> str:
    """Symmetric encryption — used for passwords/tokens we must decrypt later."""
    return _cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Invalid or tampered ciphertext") from exc


def _hash_password_sync(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password_sync(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


async def hash_password(password: str) -> str:
    """One-way hash for web user passwords (runs in thread to avoid blocking event loop)."""
    import asyncio
    return await asyncio.to_thread(_hash_password_sync, password)


async def verify_password(password: str, hashed: str) -> bool:
    import asyncio
    return await asyncio.to_thread(_verify_password_sync, password, hashed)


def require_login(request: Request) -> str:
    """FastAPI dependency: returns the logged-in username or raises 401."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"Location": "/login"},
        )
    return user
