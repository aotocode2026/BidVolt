from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import JWTError

from app.services.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    validate_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("Abc12345")
    assert hashed != "Abc12345"
    assert verify_password("Abc12345", hashed)
    assert not verify_password("Wrong123", hashed)


def test_access_token_roundtrip():
    token = create_access_token(1, 2, ["file.read", "quote.apply"])
    payload = decode_token(token)
    assert payload["sub"] == "1"
    assert payload["ent"] == 2
    assert payload["type"] == "access"
    assert "quote.apply" in payload["perms"]


def test_tampered_token_rejected():
    token = create_access_token(1, 2, [])
    with pytest.raises(JWTError):
        decode_token(token + "x")


def test_refresh_token_hash_and_validate():
    raw, token_hash = create_refresh_token()
    assert hash_refresh_token(raw) == token_hash
    future = datetime.now(timezone.utc) + timedelta(days=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert validate_refresh_token(raw, token_hash, future)
    assert not validate_refresh_token(raw, token_hash, past)
    assert not validate_refresh_token("other", token_hash, future)
