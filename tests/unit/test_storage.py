from __future__ import annotations

from pathlib import Path

import pytest

from app.services.storage import StorageProvider


def test_save_open_roundtrip(tmp_path):
    provider = StorageProvider(root=tmp_path)
    saved = provider.save(b"data-1", tenant_id=7, original_name="a.txt")
    assert saved["bucket"] == "enterprise_7"
    assert saved["sha256"]
    assert provider.open(saved["bucket"], saved["object_key"]).read_bytes() == b"data-1"


def test_save_dedup_by_content(tmp_path):
    provider = StorageProvider(root=tmp_path)
    a = provider.save(b"same", tenant_id=7, original_name="a.txt")
    b = provider.save(b"same", tenant_id=7, original_name="b.txt")
    assert a["object_key"] == b["object_key"]


def test_tenant_isolated_buckets(tmp_path):
    provider = StorageProvider(root=tmp_path)
    a = provider.save(b"x", tenant_id=1, original_name="a.txt")
    b = provider.save(b"x", tenant_id=2, original_name="a.txt")
    assert a["bucket"] != b["bucket"]


def test_sanitize_name():
    assert StorageProvider.sanitize_name("../../evil.txt") == "evil.txt"
    assert StorageProvider.sanitize_name("a/b.txt") == "b.txt"
    assert "/" not in StorageProvider.sanitize_name("a\\b.txt")


def test_open_rejects_traversal(tmp_path):
    provider = StorageProvider(root=tmp_path)
    with pytest.raises(ValueError):
        provider.open("enterprise_1", "../../secret.txt")


def test_signed_download_roundtrip_and_expiry(monkeypatch):
    import time

    from urllib.parse import parse_qs, urlparse

    url = StorageProvider.sign_download(file_id=7, tenant_id=3, expires_in=300)
    assert url.startswith("/api/v1/files/7/signed?exp=")
    assert "sig=" in url

    qs = parse_qs(urlparse(url).query)
    exp, sig = int(qs["exp"][0]), qs["sig"][0]
    assert StorageProvider.verify_signed(7, 3, exp, sig) is True
    assert StorageProvider.verify_signed(7, 4, exp, sig) is False  # 租户不符
    assert StorageProvider.verify_signed(8, 3, exp, sig) is False  # 文件不符

    monkeypatch.setattr("app.services.storage.time.time", lambda: exp + 301)
    assert StorageProvider.verify_signed(7, 3, exp, sig) is False  # 过期
