"""密码哈希与 JWT。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import jwt

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _token_payload(
    user_id: int, enterprise_id: int, permissions: list[str], token_type: str, minutes: int
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "sub": str(user_id),
        "ent": enterprise_id,
        "perms": permissions,
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }


def create_access_token(user_id: int, enterprise_id: int, permissions: list[str]) -> str:
    payload = _token_payload(
        user_id, enterprise_id, permissions, "access", settings.access_token_minutes
    )
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def create_refresh_token() -> tuple[str, str]:
    """返回 (明文 token, sha256 哈希)。DB 只存哈希。"""
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_refresh_token(raw: str, token_hash: str, expires_at: datetime) -> bool:
    if hash_refresh_token(raw) != token_hash:
        return False
    if datetime.now(timezone.utc) > ensure_aware(expires_at):
        return False
    return True


def ensure_aware(dt: datetime) -> datetime:
    """SQLite 返回 naive datetime；统一转 aware 再比较。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
