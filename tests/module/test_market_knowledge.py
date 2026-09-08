"""Issue #34：行情内容库 API、AI 提炼、一对多删除。"""

from __future__ import annotations

import asyncio
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.constants import Permission, TaskType
from app.models.auth import AppUser
from app.models.task import Task
from app.services import market_knowledge, task_service

TEST_DB_URL = "sqlite+aiosqlite:///./.test_bidvolt.db"


def _register(client, email="mk@test.com"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": "行情测试企业"},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _grant_admin(email: str) -> None:
    async def run() -> None:
        engine = create_async_engine(TEST_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(AppUser).where(AppUser.email == email))
            assert user is not None
            user.permissions = sorted(Permission.ALL)
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def _run_extract(monkeypatch) -> None:
    from app.services.llm import LLMClient

    monkeypatch.setattr("app.services.llm.llm_enabled", lambda: True)

    async def fake_chat(self, system, user):
        return '{"points": ["要点一：投标前逐条核对资格要求", "要点二：业绩材料按评审要素排列"]}'

    monkeypatch.setattr(LLMClient, "chat", fake_chat)

    async def run() -> None:
        engine = create_async_engine(TEST_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            task = (
                await session.scalars(
                    select(Task)
                    .where(Task.task_type == TaskType.MARKET_KNOWLEDGE_EXTRACT)
                    .order_by(Task.id.desc())
                    .limit(1)
                )
            ).first()
            assert task is not None
            await task_service.run_task(session, task)
        await engine.dispose()

    asyncio.run(run())


def test_upload_requires_admin_and_creates_article(client):
    h = _register(client, email="mk1@test.com")
    r = client.post(
        "/api/v1/market-knowledge/upload",
        files={"file": ("tips.txt", "投标经验：先核对资格。".encode(), "text/plain")},
        headers=h,
    )
    assert r.status_code == 403
    _grant_admin("mk1@test.com")
    r = client.post(
        "/api/v1/market-knowledge/upload",
        files={"file": ("tips.txt", "投标经验：先核对资格。".encode(), "text/plain")},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["category"] == "文档"
    assert body["source_type"] == "upload"
    assert body["extract_status"] == "pending"


def test_import_url_extract_and_points(client, monkeypatch):
    h = _register(client, email="mk2@test.com")
    _grant_admin("mk2@test.com")

    async def fake_fetch(url):
        return {
            "title": "一篇投标经验文章",
            "text": "正文：投标前要逐条核对资格要求；业绩材料按评审要素排列。",
            "html": "<html><head><title>一篇投标经验文章</title></head><body><p>正文内容</p></body></html>",
            "image_urls": [],
        }

    monkeypatch.setattr(market_knowledge, "fetch_article", fake_fetch)
    r = client.post(
        "/api/v1/market-knowledge/import-url",
        json={"url": "https://example.com/article/1"},
        headers=h,
    )
    assert r.status_code == 201
    article = r.json()
    assert article["category"] == "网页文章"
    assert article["source_type"] == "url"

    _run_extract(monkeypatch)

    detail = client.get(f"/api/v1/market-knowledge/{article['article_id']}", headers=h).json()
    assert detail["extract_status"] == "done"
    assert detail["rules_version"] == market_knowledge.MARKET_RULES_VERSION
    assert len(detail["points"]) == 2
    assert "资格" in detail["points"][0]["content"]

    points = client.get("/api/v1/market-knowledge/points", headers=h).json()
    assert points["total"] == 2
    assert points["strategy"] == "all"
    assert [p["article_id"] for p in points["items"]] == [article["article_id"]] * 2


def test_delete_article_cascades_points(client, monkeypatch):
    h = _register(client, email="mk3@test.com")
    _grant_admin("mk3@test.com")

    async def fake_fetch(url):
        return {
            "title": "将被删除的文章",
            "text": "内容",
            "html": "<html><body><p>内容</p></body></html>",
            "image_urls": [],
        }

    monkeypatch.setattr(market_knowledge, "fetch_article", fake_fetch)
    article = client.post(
        "/api/v1/market-knowledge/import-url",
        json={"url": "https://example.com/article/2"},
        headers=h,
    ).json()
    _run_extract(monkeypatch)
    assert client.get("/api/v1/market-knowledge/points", headers=h).json()["total"] == 2

    r = client.delete(f"/api/v1/market-knowledge/{article['article_id']}", headers=h)
    assert r.status_code == 200
    assert client.get("/api/v1/market-knowledge/points", headers=h).json()["total"] == 0
    assert client.get(f"/api/v1/market-knowledge/{article['article_id']}", headers=h).status_code == 404


def test_upload_image_and_search(client):
    from PIL import Image

    h = _register(client, email="mk4@test.com")
    _grant_admin("mk4@test.com")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(buf, format="PNG")
    r = client.post(
        "/api/v1/market-knowledge/upload",
        files={"file": ("示例图片.png", buf.getvalue(), "image/png")},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["category"] == "图片"
    listing = client.get("/api/v1/market-knowledge?q=示例图片", headers=h).json()
    assert listing["total"] == 1
    assert listing["items"][0]["article_id"] == r.json()["article_id"]


def test_re_extract_increments_attempt(client, monkeypatch):
    h = _register(client, email="mk5@test.com")
    _grant_admin("mk5@test.com")

    async def fake_fetch(url):
        return {
            "title": "重试文章",
            "text": "内容",
            "html": "<html><body><p>内容</p></body></html>",
            "image_urls": [],
        }

    monkeypatch.setattr(market_knowledge, "fetch_article", fake_fetch)
    article = client.post(
        "/api/v1/market-knowledge/import-url",
        json={"url": "https://example.com/article/3"},
        headers=h,
    ).json()
    r = client.post(f"/api/v1/market-knowledge/{article['article_id']}/re-extract", headers=h)
    assert r.status_code == 200
    assert r.json()["extract_status"] == "pending"


def test_market_knowledge_visible_across_enterprises(client, monkeypatch):
    """口径修正：行情库为平台共享内容，所有登录用户可见，不按企业分类。"""
    h_a = _register(client, email="mka@test.com")
    _grant_admin("mka@test.com")
    r = client.post(
        "/api/v1/market-knowledge/upload",
        files={"file": ("平台共享经验.txt", "平台共享的投标经验要点".encode(), "text/plain")},
        headers=h_a,
    )
    assert r.status_code == 201
    article_id = r.json()["article_id"]

    h_b = _register(client, email="mkb@test.com")
    listing = client.get("/api/v1/market-knowledge", headers=h_b).json()
    assert listing["total"] == 1
    assert listing["items"][0]["article_id"] == article_id
    assert client.get(f"/api/v1/market-knowledge/{article_id}", headers=h_b).status_code == 200


def test_platform_admin_from_other_enterprise_can_delete(client, monkeypatch):
    h_a = _register(client, email="mkdel-a@test.com")
    _grant_admin("mkdel-a@test.com")
    article = client.post(
        "/api/v1/market-knowledge/upload",
        files={"file": ("待删.txt", "内容".encode(), "text/plain")},
        headers=h_a,
    ).json()

    h_b = _register(client, email="mkdel-b@test.com")
    _grant_admin("mkdel-b@test.com")
    r = client.delete(f"/api/v1/market-knowledge/{article['article_id']}", headers=h_b)
    assert r.status_code == 200
    assert client.get(f"/api/v1/market-knowledge/{article['article_id']}", headers=h_a).status_code == 404
