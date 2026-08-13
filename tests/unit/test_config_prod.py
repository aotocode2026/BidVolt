"""生产配置 fail-fast（Issue #3：漏配/弱默认值禁止启动）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

STRONG_DB = "postgresql+asyncpg://bidvolt:StrongPassw0rd!@127.0.0.1:5432/bidvolt"


def test_production_rejects_dev_defaults():
    with pytest.raises(ValidationError):
        Settings(
            bidvolt_env="production",
            database_url="postgresql+asyncpg://bidvolt:bidvolt@127.0.0.1:5432/bidvolt",
            jwt_secret="dev-only-secret-change-me",
            bidvolt_internal_token="",
            virus_scan_required=False,
        )


def test_production_rejects_short_jwt_secret():
    with pytest.raises(ValidationError):
        Settings(
            bidvolt_env="production",
            database_url=STRONG_DB,
            jwt_secret="short",
            bidvolt_internal_token="t" * 32,
            virus_scan_required=True,
        )


def test_production_rejects_placeholder_internal_token():
    with pytest.raises(ValidationError):
        Settings(
            bidvolt_env="production",
            database_url=STRONG_DB,
            jwt_secret="s" * 32,
            bidvolt_internal_token="change-me",
            virus_scan_required=True,
        )


def test_production_rejects_sqlite():
    with pytest.raises(ValidationError):
        Settings(
            bidvolt_env="production",
            database_url="sqlite+aiosqlite:///./prod.db",
            jwt_secret="s" * 32,
            bidvolt_internal_token="t" * 32,
            virus_scan_required=True,
        )


def test_production_rejects_closed_clamav():
    with pytest.raises(ValidationError):
        Settings(
            bidvolt_env="production",
            database_url=STRONG_DB,
            jwt_secret="s" * 32,
            bidvolt_internal_token="t" * 32,
            virus_scan_required=False,
        )


def test_production_accepts_strong_config():
    settings = Settings(
        bidvolt_env="production",
        database_url=STRONG_DB,
        jwt_secret="s" * 32,
        bidvolt_internal_token="t" * 32,
        virus_scan_required=True,
    )
    assert settings.jwt_secret == "s" * 32


def test_dev_mode_allows_weak_defaults():
    """本地开发/测试保持宽松（默认 bidvolt_env=dev）。"""
    settings = Settings(
        database_url="postgresql+asyncpg://bidvolt:bidvolt@127.0.0.1:5432/bidvolt",
        jwt_secret="dev-only-secret-change-me",
    )
    assert settings.bidvolt_env == "dev"
