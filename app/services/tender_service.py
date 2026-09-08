"""招标公告 URL 安全导入（Issue #6 P0 + Issue #32 逐附件下载与预览）。

流程（Issue #32 确认口径）：
- 用户输入的页面 URL 允许任意公开网址；仅 http/https，内网/保留地址等 SSRF 目标拒绝；
- 接口“秒回”：同步创建 `TenderNotice`（导入中）+ `UploadBatch` + worker `Task`；
- worker 后台：渲染/抓取公告正文 → 逐附件下载/解包/入库 → 逐文件状态与原因；
- 正文 `document_role=招标公告`；附件 `招标文件/公告附件`，只入本项目材料，绝不写企业资料库；
- 附件失败逐条记录（`error`/`skipped`），成功项保留；“获取招标文件”类需登录附件按
  链接文本 + 401/403 + 登录墙 HTML 判定为 `skipped`。

SSRF/DNS rebinding 防护：仅 http/https；逐跳（含重定向）解析 DNS 并校验所有解析结果
均非内网/保留地址；手动重定向循环（上限 5）；附件 1GB 上限（流式计数）；默认拦截
高风险可执行扩展名；全程审计。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext
from app.config import settings
from app.constants import TaskType
from app.models.file import FileObject, UploadBatch, UploadBatchItem
from app.models.project import Project
from app.models.task import Task
from app.models.tender_notice import TenderNotice
from app.services import file_service
from app.services.tender_crawler import (
    AttachmentDownloadError,
    AttachmentLink,
    CrawlerError,
    RenderedDocument,
    TenderPageFetcher,
)


class TenderImportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise TenderImportError("dns_failed", f"域名解析失败：{host}") from exc
    if not infos:
        raise TenderImportError("dns_failed", f"域名无解析结果：{host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise TenderImportError("blocked_address", f"目标地址 {ip} 为内网/保留地址，已拒绝")


def _validate_url(url: str) -> None:
    """SSRF 校验：仅 http/https，禁内网/保留地址（任意公开网址均可）。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TenderImportError("unsupported_scheme", "仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise TenderImportError("invalid_url", "URL 缺少主机名")
    if parsed.username or parsed.password:
        raise TenderImportError("invalid_url", "URL 不允许携带用户信息")
    _validate_host(host)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def import_tender_notice(
    session: AsyncSession,
    user: UserContext,
    project_id: int,
    url: str,
) -> TenderNotice:
    """创建导入记录 + 导入批次 + 后台任务（秒回，附件由 worker 异步处理）。"""
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == user.enterprise_id,
        )
    )
    if project is None or project.is_deleted:
        raise ValueError("项目不存在或已归档")

    _validate_url(url)

    notice = TenderNotice(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        source_url=url,
        status=1,  # 导入中
    )
    session.add(notice)
    await session.flush()

    batch = UploadBatch(
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        project_id=project_id,
        target="tender_import",
    )
    session.add(batch)
    await session.flush()
    notice.import_batch_id = batch.id

    from app.services.task_service import create_task  # noqa: PLC0415 延迟导入避免循环依赖

    await create_task(
        session,
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        task_type=TaskType.TENDER_IMPORT,
        payload={"notice_id": notice.id, "url": url, "user_id": user.user_id},
        idempotency_key=f"tender_import:{notice.id}",
        priority=5,
    )
    await session.commit()
    return notice


async def run_tender_import(session: AsyncSession, task: Task) -> None:
    """worker handler：渲染/抓取正文 → 逐附件下载/解包/入库 → 更新公告状态。"""
    payload = task.payload or {}
    notice_id = payload.get("notice_id")
    url = payload.get("url") or ""
    user_id = payload.get("user_id") or 0
    if not notice_id:
        raise ValueError("导入任务缺少 notice_id")
    notice = await session.scalar(
        select(TenderNotice).where(
            TenderNotice.id == notice_id,
            TenderNotice.enterprise_id == task.enterprise_id,
        )
    )
    if notice is None:
        raise ValueError("招标公告导入记录不存在")
    user = UserContext(
        user_id=user_id,
        enterprise_id=task.enterprise_id,
        email="",
        permissions=set(),
    )
    # 重试场景：先回到“导入中”，避免残留终态
    if notice.status != 2:
        notice.status = 1
        notice.error_code = None
        notice.error_message = None

    try:
        async with TenderPageFetcher(url) as fetcher:
            doc = await fetcher.fetch_and_discover()
            await _store_body(session, user, notice, doc)
            await _set_progress(session, task, 40, "正文已导入，开始下载附件")
            await _store_attachments(session, user, notice, doc.links, fetcher)
    except CrawlerError as exc:
        notice.status = 3
        notice.error_code = exc.code
        notice.error_message = exc.message
        notice.imported_at = _now()
        # run_task 失败路径会 rollback：先把公告终态单独落库，再抛出以计入任务重试
        await session.commit()
        raise TenderImportError(exc.code, exc.message) from exc
    except TenderImportError:
        notice.status = 3
        notice.imported_at = _now()
        await session.commit()
        raise

    notice.status = 2
    notice.imported_at = _now()
    await _set_progress(session, task, 100, "导入完成")


async def _set_progress(session: AsyncSession, task: Task, percent: int, work: str) -> None:
    task.progress = {
        "phase": TaskType.TENDER_IMPORT,
        "status": "running",
        "percent": percent,
        "current_work": work,
    }
    await session.commit()
    # commit 会清掉事务级 RLS 上下文：重建后继续写业务表
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, task.enterprise_id)


async def _store_body(
    session: AsyncSession,
    user: UserContext,
    notice: TenderNotice,
    doc: RenderedDocument,
) -> None:
    html = doc.html or ""
    if not html.strip():
        raise TenderImportError("empty_body", "公告正文为空")
    data = html.encode("utf-8")
    if len(data) > settings.tender_import_body_max_bytes:
        raise TenderImportError(
            "too_large",
            f"公告正文超过大小上限（{settings.tender_import_body_max_bytes} 字节）",
        )
    fobj = await file_service.process_upload(
        session,
        user,
        data,
        "招标公告.html",
        "project",
        notice.project_id,
        document_role="招标公告",
        max_bytes=settings.tender_import_body_max_bytes,
    )
    notice.file_id = fobj.id
    notice.title = (doc.title or "招标公告").strip()[:500] or "招标公告"
    session.add(
        UploadBatchItem(
            batch_id=notice.import_batch_id,
            enterprise_id=user.enterprise_id,
            filename="招标公告.html",
            file_id=fobj.id,
            status="accepted",
            document_role="招标公告",
            notice_id=notice.id,
            source_url=notice.source_url,
            parse_status=file_service.file_parse_status(fobj.status),
        )
    )


async def _store_attachments(
    session: AsyncSession,
    user: UserContext,
    notice: TenderNotice,
    links: list[AttachmentLink],
    fetcher: TenderPageFetcher,
) -> None:
    for link in links:
        try:
            data, name, _ct = await fetcher.download(link)
        except AttachmentDownloadError as exc:
            session.add(
                UploadBatchItem(
                    batch_id=notice.import_batch_id,
                    enterprise_id=user.enterprise_id,
                    filename=link.text or _basename(link.href),
                    status="skipped" if exc.skip_login else "error",
                    message=exc.message[:2000],
                    document_role=link.kind,
                    notice_id=notice.id,
                    source_url=link.href,
                )
            )
            continue
        try:
            async with session.begin_nested():
                fobj = await file_service.process_upload(
                    session,
                    user,
                    data,
                    name,
                    "project",
                    notice.project_id,
                    document_role=link.kind,
                    max_bytes=settings.tender_import_attachment_max_bytes,
                )
                session.add(
                    UploadBatchItem(
                        batch_id=notice.import_batch_id,
                        enterprise_id=user.enterprise_id,
                        filename=name,
                        file_id=fobj.id,
                        status="accepted",
                        document_role=link.kind,
                        notice_id=notice.id,
                        source_url=link.href,
                        parse_status=file_service.file_parse_status(fobj.status),
                    )
                )
            if fobj.ext == ".zip":
                await _expand_archive(session, user, notice, fobj, link)
        except file_service.DuplicateUploadError as dup:
            session.add(
                UploadBatchItem(
                    batch_id=notice.import_batch_id,
                    enterprise_id=user.enterprise_id,
                    filename=name,
                    file_id=dup.existing.id,
                    status="duplicate",
                    message="内容与已入库文件相同，已跳过重复入库",
                    document_role=link.kind,
                    notice_id=notice.id,
                    source_url=link.href,
                )
            )
        except Exception as exc:  # noqa: BLE001 单附件失败不影响其他附件
            session.add(
                UploadBatchItem(
                    batch_id=notice.import_batch_id,
                    enterprise_id=user.enterprise_id,
                    filename=link.text or _basename(link.href),
                    status="error",
                    message=str(exc)[:2000],
                    document_role=link.kind,
                    notice_id=notice.id,
                    source_url=link.href,
                )
            )


async def _expand_archive(
    session: AsyncSession,
    user: UserContext,
    notice: TenderNotice,
    zip_fobj: FileObject,
    link: AttachmentLink,
) -> None:
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, user.enterprise_id)
    job = await file_service.process_archive(
        session, user, zip_fobj.id, "project", notice.project_id
    )
    # process_archive 内部 commit 会清掉 RLS 上下文：重建后继续写批次项
    await _set_rls_context(session, user.enterprise_id)
    result = job.result or {}
    imported = result.get("imported") or []
    if imported:
        fobjs = (
            await session.scalars(select(FileObject).where(FileObject.id.in_(imported)))
        ).all()
        fmap = {f.id: f for f in fobjs}
        for fid in imported:
            fobj = fmap.get(fid)
            if fobj is None:
                continue
            session.add(
                UploadBatchItem(
                    batch_id=notice.import_batch_id,
                    enterprise_id=user.enterprise_id,
                    filename=fobj.archive_path or fobj.original_name,
                    file_id=fobj.id,
                    status="expanded",
                    document_role=link.kind,
                    notice_id=notice.id,
                    source_url=link.href,
                    source_archive_file_id=zip_fobj.id,
                    archive_path=fobj.archive_path,
                    parse_status=file_service.file_parse_status(fobj.status),
                )
            )
    for fail in result.get("failed") or []:
        session.add(
            UploadBatchItem(
                batch_id=notice.import_batch_id,
                enterprise_id=user.enterprise_id,
                filename=fail.get("name") or "解包条目",
                status="error",
                message=str(fail.get("reason") or "解包失败")[:2000],
                document_role=link.kind,
                notice_id=notice.id,
                source_url=link.href,
                source_archive_file_id=zip_fobj.id,
            )
        )
    for dup in result.get("duplicates") or []:
        session.add(
            UploadBatchItem(
                batch_id=notice.import_batch_id,
                enterprise_id=user.enterprise_id,
                filename=dup.get("name") or "解包条目",
                file_id=dup.get("file_id"),
                status="duplicate",
                message="内容与已入库文件相同，已跳过重复入库",
                document_role=link.kind,
                notice_id=notice.id,
                source_url=link.href,
                source_archive_file_id=zip_fobj.id,
            )
        )


def _basename(href: str) -> str:
    from pathlib import PurePosixPath

    name = PurePosixPath(urllib.parse.urlparse(href).path).name
    return name or "附件"
