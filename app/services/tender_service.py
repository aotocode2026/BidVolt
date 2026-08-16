"""招标公告 URL 安全导入（Issue #6 P0）。

SSRF/DNS rebinding 防护：仅 http/https；逐跳（含重定向）解析 DNS 并校验所有解析结果
均非内网/保留地址；手动重定向循环（上限 5）；下载大小上限；内容类型白名单；
正文仅进入本项目材料（document_role=招标公告），绝不写企业资料库；全程审计。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from datetime import datetime, timezone
from pathlib import PurePosixPath

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext
from app.config import settings
from app.models.project import Project
from app.models.tender_notice import TenderNotice
from app.services import file_service

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_CONTENT_TYPES = {
    "text/html",
    "text/plain",
    "application/pdf",
    "application/zip",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/octet-stream",
}
CONTENT_TYPE_EXT = {
    "text/html": ".html",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.rar": ".rar",
    "application/x-7z-compressed": ".7z",
}


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
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TenderImportError("unsupported_scheme", "仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise TenderImportError("invalid_url", "URL 缺少主机名")
    if parsed.username or parsed.password:
        raise TenderImportError("invalid_url", "URL 不允许携带用户信息")
    _validate_host(host)


def _content_type_ok(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    return base in ALLOWED_CONTENT_TYPES


def _ensure_ext(filename: str, content_type: str) -> str:
    if PurePosixPath(filename).suffix:
        return filename
    base = content_type.split(";", 1)[0].strip().lower()
    return filename + CONTENT_TYPE_EXT.get(base, ".html")


async def fetch_document(url: str) -> tuple[bytes, str, str]:
    """带逐跳 SSRF 校验的下载：返回 (data, filename, content_type)。"""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            follow_redirects=False,
            proxy=settings.http_proxy or None,
        ) as client:
            resp = await client.get(current, headers={"User-Agent": "BidVolt/1.0 (tender-notice-import)"})
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise TenderImportError("redirect_missing", f"重定向缺少 Location（{resp.status_code}）")
            current = urllib.parse.urljoin(current, location)
            continue
        if resp.status_code != 200:
            raise TenderImportError("http_error", f"下载失败：HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if not _content_type_ok(content_type):
            raise TenderImportError("content_type_blocked", f"不允许的内容类型：{content_type or '(无)'}")
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise TenderImportError("too_large", f"公告超过大小上限（{MAX_DOWNLOAD_BYTES} 字节）")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise TenderImportError("too_large", f"公告超过大小上限（{MAX_DOWNLOAD_BYTES} 字节）")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise TenderImportError("empty_body", "公告内容为空")
        path = urllib.parse.urlparse(current).path
        filename = PurePosixPath(path).name or "招标公告.html"
        filename = _ensure_ext(filename, content_type)
        return data, filename, content_type
    raise TenderImportError("too_many_redirects", f"重定向超过 {MAX_REDIRECTS} 次")


async def import_tender_notice(
    session: AsyncSession,
    user: UserContext,
    project_id: int,
    url: str,
) -> TenderNotice:
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.enterprise_id == user.enterprise_id,
        )
    )
    if project is None or project.is_deleted:
        raise ValueError("项目不存在或已归档")

    notice = TenderNotice(
        enterprise_id=user.enterprise_id,
        project_id=project_id,
        source_url=url,
        status=1,
    )
    session.add(notice)
    await session.flush()

    try:
        data, filename, _ct = await fetch_document(url)
        fobj = await file_service.process_upload(
            session,
            user,
            data,
            filename,
            "project",
            project_id,
            document_role="招标公告",
        )
        notice.file_id = fobj.id
        notice.title = filename
        notice.status = 2
        notice.imported_at = datetime.now(timezone.utc)
    except TenderImportError as exc:
        notice.status = 3
        notice.error_code = exc.code
        notice.error_message = exc.message
    await session.commit()
    return notice
