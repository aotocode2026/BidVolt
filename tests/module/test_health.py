from __future__ import annotations


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_info(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "BidVolt API"


def test_demo_assets_not_cached(client):
    """Issue #8：/demo 前端禁止缓存，避免旧版 app.js 被浏览器/代理强缓存导致已修复问题复现。"""
    r = client.get("/demo/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_api_responses_not_cached(client):
    r = client.get("/api/v1/projects")
    assert r.status_code in (200, 401)
    assert r.headers["cache-control"] == "no-store"
