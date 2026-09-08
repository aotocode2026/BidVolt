"""Issue #32：招标公告 URL 导入逐附件下载（API 契约 + worker 编排）。"""

from __future__ import annotations

import asyncio
import io
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.constants import TaskType
from app.models.task import Task
from app.services import task_service, tender_service
from app.services.tender_crawler import (
    AttachmentDownloadError,
    AttachmentLink,
    CrawlerError,
    RenderedDocument,
)

TEST_DB_URL = "sqlite+aiosqlite:///./.test_bidvolt.db"
ALLOWED_URL = "https://sgccetp.com.cn/portal/#/doc/doci-bid/1"


def _register(client, email="ti@test.com", name="导入测试企业"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": name},
    )
    assert r.status_code == 201
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _make_project(client, h):
    r = client.post("/api/v1/projects", json={"name": "导入项目"}, headers=h)
    assert r.status_code == 201
    return r.json()["project_id"]


class _FakeFetcher:
    def __init__(self, url: str) -> None:
        self.url = url

    async def __aenter__(self) -> _FakeFetcher:
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def fetch_and_discover(self) -> RenderedDocument:
        return RenderedDocument(
            html="<html><head><title>某招标公告</title></head><body>公告正文内容</body></html>",
            title="某招标公告",
            final_url=self.url,
            links=[
                AttachmentLink(text="下载公告文件", href="https://sgccetp.com.cn/a.txt"),
                AttachmentLink(
                    text="获取招标文件",
                    href="javascript:void(0)",
                    kind="招标文件",
                    login_candidate=True,
                    is_action=True,
                ),
            ],
        )

    async def download(self, link: AttachmentLink) -> tuple[bytes, str, str]:
        if link.login_candidate:
            raise AttachmentDownloadError(
                "login_required", "附件需登录或无权限下载，已跳过（可手动上传补充）", skip_login=True
            )
        return "附件正文A".encode(), "招标公告.txt", "text/plain"


async def _run_import_task(client, monkeypatch, fetcher_cls=None):
    monkeypatch.setattr(tender_service, "TenderPageFetcher", fetcher_cls or _FakeFetcher)
    engine = create_async_engine(TEST_DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        task = (
            await session.scalars(
                select(Task).order_by(Task.id.desc()).limit(1)
            )
        ).first()
        assert task is not None
        assert task.task_type == TaskType.TENDER_IMPORT
        await task_service.run_task(session, task)
        await session.commit()
    await engine.dispose()


def test_import_url_then_worker_stores_attachments(client, monkeypatch):
    h = _register(client)
    pid = _make_project(client, h)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": ALLOWED_URL},
        headers=h,
    )
    assert r.status_code == 201
    notice_id = r.json()["tender_notice_id"]

    asyncio.run(_run_import_task(client, monkeypatch))

    detail = client.get(
        f"/api/v1/projects/{pid}/tender-notices/{notice_id}", headers=h
    ).json()
    assert detail["status"] == 2
    assert detail["title"] == "某招标公告"
    assert detail["file_id"] is not None
    items = detail["attachments"]
    by_name = {i["filename"]: i for i in items}
    assert by_name["招标公告.html"]["status"] == "accepted"
    assert by_name["招标公告.txt"]["status"] == "accepted"
    assert by_name["招标公告.txt"]["document_role"] == "公告附件"
    assert by_name["获取招标文件"]["status"] == "skipped"
    assert "需登录" in by_name["获取招标文件"]["message"]


def test_zip_attachment_expands_to_batch_items(client, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("材料.txt", "包内文件内容")
    zip_bytes = buf.getvalue()

    class _ZipFetcher(_FakeFetcher):
        async def download(self, link):
            if link.login_candidate:
                raise AttachmentDownloadError(
                    "login_required", "需登录", skip_login=True
                )
            return zip_bytes, "招标公告.zip", "application/zip"

    h = _register(client, email="zip@test.com")
    pid = _make_project(client, h)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": ALLOWED_URL},
        headers=h,
    )
    notice_id = r.json()["tender_notice_id"]

    asyncio.run(_run_import_task(client, monkeypatch, _ZipFetcher))

    detail = client.get(
        f"/api/v1/projects/{pid}/tender-notices/{notice_id}", headers=h
    ).json()
    assert detail["status"] == 2
    items = {i["filename"]: i for i in detail["attachments"]}
    assert items["招标公告.zip"]["status"] == "accepted"
    child = items["材料.txt"]
    assert child["status"] == "expanded"
    assert child["source_archive_file_id"] == items["招标公告.zip"]["file_id"]
    assert child["archive_path"] == "材料.txt"


def test_attachment_error_recorded_and_body_kept(client, monkeypatch):
    class _FailFetcher(_FakeFetcher):
        async def download(self, link):
            raise AttachmentDownloadError("http_error", "附件下载失败：HTTP 500")

    h = _register(client, email="fail@test.com")
    pid = _make_project(client, h)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": ALLOWED_URL},
        headers=h,
    )
    notice_id = r.json()["tender_notice_id"]

    asyncio.run(_run_import_task(client, monkeypatch, _FailFetcher))

    detail = client.get(
        f"/api/v1/projects/{pid}/tender-notices/{notice_id}", headers=h
    ).json()
    assert detail["status"] == 2  # 正文成功，附件失败不影响整体成功
    errors = [i for i in detail["attachments"] if i["status"] == "error"]
    assert len(errors) == 2
    assert all("HTTP 500" in i["message"] for i in errors)


def test_body_failure_marks_notice_failed(client, monkeypatch):
    class _BodyFailFetcher(_FakeFetcher):
        async def fetch_and_discover(self):
            raise CrawlerError("http_error", "公告页面抓取失败：HTTP 500")

    h = _register(client, email="bodyfail@test.com")
    pid = _make_project(client, h)
    r = client.post(
        f"/api/v1/projects/{pid}/tender-notices/import-url",
        json={"url": ALLOWED_URL},
        headers=h,
    )
    notice_id = r.json()["tender_notice_id"]

    asyncio.run(_run_import_task(client, monkeypatch, _BodyFailFetcher))

    detail = client.get(
        f"/api/v1/projects/{pid}/tender-notices/{notice_id}", headers=h
    ).json()
    assert detail["status"] == 3
    assert detail["error_code"] == "http_error"
    assert "HTTP 500" in detail["error_message"]
