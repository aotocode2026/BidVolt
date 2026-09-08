"""投标行情内容库（Issue #34）：抓取/上传入库、AI 提炼要点（1:N）、Agent 参考 provider。

设计要点：
- 一条资料（`MarketKnowledgeArticle`）→ 多条提炼要点（`MarketKnowledgePoint`，1:N）；
- 行情库为**平台共享内容**：所有登录用户可见，不按企业分类；`enterprise_id` 仅记录上传者所属企业用于溯源；
- 上传/删除/重试为平台管理员操作（权限点 `market_knowledge.manage`）；
- 图片走现有识图链路（`image_desc`，qwen-vl，受 `vl_enabled` 门禁），描述文本并入提炼输入；
- 提炼提示词带版本号（`MARKET_RULES_VERSION`），写入资料与任务 payload，便于后续演进追溯；
- Agent 参考经可替换策略 provider（当前 `all`=全量读取已提炼要点），调用方不感知策略变化；
- 云模型关闭/提炼失败：资料仍可入库可预览，`extract_status=failed`，可重试。
"""

from __future__ import annotations

import io
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext
from app.config import settings
from app.constants import TaskType
from app.models.file import FileObject, ImageDescription
from app.models.market_knowledge import (
    MarketKnowledgeArticle,
    MarketKnowledgeImage,
    MarketKnowledgePoint,
)
from app.models.task import Task
from app.services import file_safety
from app.services.quota_service import check_storage
from app.services.storage import StorageProvider

storage = StorageProvider()

# 提炼提示词版本：修改提示词/要点格式时 +1，并记录 UPDATE_LOG（Issue #34 约定）
MARKET_RULES_VERSION = "1"

_EXTRACT_SYSTEM = (
    "你是投标行情经验库的提炼助手。从给定资料中提炼可用于编写标书的短要点"
    "（写作技巧、注意事项、行业惯例、评标要点、常见问题规避等）。"
    "规则：每条要点不超过 120 字；只写资料中真实存在的信息，不得编造；"
    "不得输出其他企业的具体业绩/资质/报价等不可复用的敏感事实；"
    "与投标写作无关的内容不要提炼。只输出一个 JSON 对象："
    '{"points": ["要点1", "要点2"]}；确实没有可提炼内容时输出 {"points": []}。'
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp", ".gif"}
TEXT_EXTS = {".txt", ".md", ".csv", ".html", ".htm", ".docx", ".pdf", ".doc", ".xlsx", ".xls"}


class _ArticleParser(HTMLParser):
    """从 HTML 提取标题/正文段落/图片地址（只取最外层文本块，避免嵌套重复）。"""

    _CAPTURE = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title: str | None = None
        self.blocks: list[str] = []
        self.images: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._capture_depth = 0
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "meta":
            key = (attrs_map.get("property") or attrs_map.get("name") or "").lower()
            if key in ("og:title", "twitter:title"):
                self.og_title = attrs_map.get("content") or self.og_title
        elif tag == "title":
            self._in_title = True
        elif tag in self._CAPTURE:
            self._capture_depth += 1
        if tag == "img":
            src = (
                attrs_map.get("data-src")
                or attrs_map.get("data-original")
                or attrs_map.get("src")
                or ""
            ).strip()
            if src:
                self.images.append(src)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._capture_depth == 1:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self.title = "".join(self._title_parts).strip()
            self._in_title = False
        elif tag in self._CAPTURE:
            if self._capture_depth == 1:
                text = "".join(self._buf)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    self.blocks.append(text)
                self._buf = []
            if self._capture_depth:
                self._capture_depth -= 1


def parse_article_html(html: str, base_url: str) -> dict:
    parser = _ArticleParser()
    parser.feed(html)
    title = (parser.og_title or parser.title or "").strip()
    text = "\n".join(parser.blocks).strip()
    images: list[str] = []
    seen: set[str] = set()
    for src in parser.images:
        absolute = urllib.parse.urljoin(base_url, src)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme in ("http", "https") and absolute not in seen:
            seen.add(absolute)
            images.append(absolute)
    return {"title": title, "text": text, "images": images}


def article_category(source_type: str, name_or_url: str) -> str:
    if source_type == "url":
        host = (urllib.parse.urlparse(name_or_url).hostname or "").lower()
        if "weixin.qq.com" in host or "mp.weixin" in host:
            return "公众号文章"
        return "网页文章"
    ext = PurePosixPath(name_or_url).suffix.lower()
    return "图片" if ext in IMAGE_EXTS else "文档"


def _normalize_image(data: bytes, ordinal: int = 0) -> tuple[bytes, str]:
    """用 Pillow 解码并规范化图片（webp/gif/未知格式 → PNG，jpeg 保留），过滤非图片内容。"""
    from PIL import Image  # noqa: PLC0415

    img = Image.open(io.BytesIO(data))
    img.load()
    buf = io.BytesIO()
    if (img.format or "").upper() == "JPEG":
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), f"图片{ordinal or ''}.jpg"
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    img.save(buf, format="PNG")
    return buf.getvalue(), f"图片{ordinal or ''}.png"


async def save_market_file(
    session: AsyncSession,
    user: UserContext,
    data: bytes,
    filename: str,
) -> FileObject:
    """行情库文件入库：安全校验 + 病毒扫描 + 配额，不入企业资料/项目材料列表（owner_type=3）。"""
    mime, ext = file_safety.validate_upload(filename, data)
    file_safety.virus_scan(data)
    await check_storage(session, user.enterprise_id, len(data))
    saved = storage.save(data, user.enterprise_id, filename)
    fobj = FileObject(
        enterprise_id=user.enterprise_id,
        project_id=None,
        owner_type=3,  # 行情库文件（与 1 企业资料 / 2 项目材料隔离）
        bucket=saved["bucket"],
        object_key=saved["object_key"],
        sha256=saved["sha256"],
        original_name=filename,
        size_bytes=saved["size_bytes"],
        mime_type=mime,
        ext=ext,
        document_role="行情库",
        status=3 if ext in IMAGE_EXTS else 2,
    )
    session.add(fobj)
    await session.flush()
    return fobj


async def fetch_article(url: str) -> dict:
    """抓取公开网页/公众号文章：返回 {title, text, html, image_urls}。失败抛 ValueError。"""
    from app.services.tender_crawler import (
        CrawlerError,
        TenderPageFetcher,
        fetch_static_page,
        validate_site_url,
    )

    try:
        validate_site_url(url)
        if "#/" in url or "#!" in url:
            async with TenderPageFetcher(url) as fetcher:
                doc = await fetcher.fetch_and_discover()
                html, final_url = doc.html, doc.final_url or url
        else:
            html, final_url = await fetch_static_page(url)
    except CrawlerError as exc:
        raise ValueError(f"无法解析该链接：{exc.message}") from exc
    parsed = parse_article_html(html, final_url)
    if not parsed["title"] and not parsed["text"]:
        raise ValueError("无法解析该链接：页面没有可读正文")
    return {
        "title": parsed["title"],
        "text": parsed["text"],
        "html": html,
        "image_urls": parsed["images"],
    }


async def enqueue_extract(session: AsyncSession, article: MarketKnowledgeArticle) -> None:
    from app.services.task_service import create_task  # noqa: PLC0415

    await create_task(
        session,
        enterprise_id=article.enterprise_id,
        project_id=0,
        task_type=TaskType.MARKET_KNOWLEDGE_EXTRACT,
        payload={"article_id": article.id, "attempt": article.extract_attempt},
        idempotency_key=f"market-extract-{article.id}-{article.extract_attempt}",
        priority=20,
    )


async def _enqueue_image_describe(session: AsyncSession, fobj: FileObject) -> None:
    from app.services.image_desc import enqueue_describe  # noqa: PLC0415

    await enqueue_describe(session, fobj, "asset")


async def import_from_url(
    session: AsyncSession,
    user: UserContext,
    url: str,
) -> MarketKnowledgeArticle:
    fetched = await fetch_article(url)
    title = (fetched["title"] or url).strip()[:500] or "未命名资料"
    html_fobj = await save_market_file(
        session, user, fetched["html"].encode("utf-8"), "行情文章.html"
    )
    article = MarketKnowledgeArticle(
        enterprise_id=user.enterprise_id,
        title=title,
        category=article_category("url", url),
        source_type="url",
        source_url=url,
        file_id=html_fobj.id,
        text_content=fetched["text"][:1_000_000],
        extract_status="pending",
        created_by=user.user_id,
    )
    session.add(article)
    await session.flush()

    from app.services.tender_crawler import AttachmentDownloadError, download_attachment

    skipped_images = 0
    for ordinal, image_url in enumerate(
        fetched["image_urls"][: settings.market_knowledge_max_images]
    ):
        try:
            data, _name, _ct = await download_attachment(
                image_url, max_bytes=settings.market_knowledge_image_max_bytes
            )
            image_data, image_name = _normalize_image(data, ordinal)
            image_fobj = await save_market_file(session, user, image_data, image_name)
            session.add(
                MarketKnowledgeImage(
                    article_id=article.id,
                    enterprise_id=user.enterprise_id,
                    file_id=image_fobj.id,
                    ordinal=ordinal,
                )
            )
            await _enqueue_image_describe(session, image_fobj)
        except (AttachmentDownloadError, ValueError, OSError):
            skipped_images += 1
            continue
    await enqueue_extract(session, article)
    await session.commit()
    return article


async def create_from_upload(
    session: AsyncSession,
    user: UserContext,
    data: bytes,
    filename: str,
) -> MarketKnowledgeArticle:
    ext = PurePosixPath(filename).suffix.lower()
    original_title = PurePosixPath(filename).stem.strip()[:500]
    is_image = ext in IMAGE_EXTS
    if is_image:
        data, filename = _normalize_image(data)
        ext = PurePosixPath(filename).suffix.lower()
    fobj = await save_market_file(session, user, data, filename)
    article = MarketKnowledgeArticle(
        enterprise_id=user.enterprise_id,
        title=original_title or "未命名资料",
        category=article_category("upload", filename),
        source_type="upload",
        file_id=fobj.id,
        extract_status="pending",
        created_by=user.user_id,
    )
    session.add(article)
    await session.flush()

    if is_image:
        session.add(
            MarketKnowledgeImage(
                article_id=article.id,
                enterprise_id=user.enterprise_id,
                file_id=fobj.id,
                ordinal=0,
            )
        )
        await _enqueue_image_describe(session, fobj)
    else:
        try:
            from app.services import parser

            blocks = parser.parse_to_blocks(storage.open(fobj.bucket, fobj.object_key), ext)
            article.text_content = "\n".join(
                b.get("text_content") or "" for b in blocks
            )[:1_000_000]
        except Exception:  # noqa: BLE001 解析失败不阻塞入库，提炼阶段会标记原因
            article.text_content = ""
        if ext in (".docx", ".pdf"):
            from app.services.image_desc import extract_embedded_images

            for ordinal, embedded in enumerate(extract_embedded_images(fobj)[:settings.market_knowledge_max_images]):
                try:
                    image_data, image_name = _normalize_image(embedded["data"], ordinal)
                    image_fobj = await save_market_file(session, user, image_data, image_name)
                    session.add(
                        MarketKnowledgeImage(
                            article_id=article.id,
                            enterprise_id=user.enterprise_id,
                            file_id=image_fobj.id,
                            ordinal=ordinal,
                        )
                    )
                    await _enqueue_image_describe(session, image_fobj)
                except (ValueError, OSError):
                    continue
    await enqueue_extract(session, article)
    await session.commit()
    return article


async def _image_descriptions(session: AsyncSession, article: MarketKnowledgeArticle) -> list[str]:
    file_ids = list(
        (
            await session.scalars(
                select(MarketKnowledgeImage.file_id).where(
                    MarketKnowledgeImage.article_id == article.id,
                    MarketKnowledgeImage.enterprise_id == article.enterprise_id,
                )
            )
        ).all()
    )
    if not file_ids:
        return []
    shas = list(
        (
            await session.scalars(
                select(FileObject.sha256).where(
                    FileObject.id.in_(file_ids),
                    FileObject.enterprise_id == article.enterprise_id,
                )
            )
        ).all()
    )
    if not shas:
        return []
    rows = (
        await session.scalars(
            select(ImageDescription).where(ImageDescription.sha256.in_(shas))
        )
    ).all()
    out: list[str] = []
    for row in rows:
        desc = row.description or {}
        summary = str(desc.get("text_summary") or "").strip()
        if summary:
            out.append(summary)
        else:
            pieces = [
                str(desc.get(k) or "")
                for k in ("doc_type", "subject", "numbers", "dates", "amounts", "people", "stamps")
                if desc.get(k)
            ]
            if pieces:
                out.append("；".join(pieces))
    return out


async def extract_handler(session: AsyncSession, task: Task) -> None:
    """worker handler：文本 + 已完成的图片描述 → LLM 提炼短要点（1:N 落库）。"""
    from app.services.llm import LLMClient, LLMGateClosed, llm_enabled, try_extract_json
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    article_id = int((task.payload or {}).get("article_id") or 0)
    article = await session.scalar(
        select(MarketKnowledgeArticle).where(
            MarketKnowledgeArticle.id == article_id,
            MarketKnowledgeArticle.deleted_at.is_(None),
        )
    )
    if article is None:
        task.result = {"error": f"行情资料不存在：{article_id}"}
        return
    if not llm_enabled():
        article.extract_status = "failed"
        article.extract_error = "云模型未启用（数据分级/客户授权未确认，P1 门禁）"
        await session.commit()
        task.result = {"points": 0, "error": article.extract_error}
        return

    article.extract_status = "processing"
    await session.commit()
    await _set_rls_context(session, task.enterprise_id)

    text_head = (article.text_content or "")[: settings.market_knowledge_text_max_chars]
    descriptions = await _image_descriptions(session, article)
    image_block = "\n".join(f"[图片] {d}" for d in descriptions[:10]) or "（无图片描述）"
    user_prompt = (
        f"资料标题：{article.title}\n资料正文：\n{text_head or '（无文本正文）'}\n\n"
        f"资料内图片的可见内容描述：\n{image_block}"
    )
    try:
        reply = await LLMClient().chat(_EXTRACT_SYSTEM, user_prompt)
    except LLMGateClosed as exc:
        article.extract_status = "failed"
        article.extract_error = f"云模型未启用：{exc}"
        await session.commit()
        task.result = {"points": 0, "error": article.extract_error}
        return
    parsed = try_extract_json(reply)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("points"), list):
        article.extract_status = "failed"
        article.extract_error = "提炼输出不是合法 JSON（points 数组缺失）"
        await session.commit()
        task.result = {"points": 0, "error": article.extract_error}
        return

    points: list[str] = []
    seen: set[str] = set()
    for raw in parsed["points"][: settings.market_knowledge_max_points]:
        if not isinstance(raw, str):
            continue
        content = re.sub(r"\s+", " ", raw).strip()[: settings.market_knowledge_point_max_chars]
        if content and content not in seen:
            seen.add(content)
            points.append(content)

    now = datetime.now(timezone.utc)
    for old in (
        await session.scalars(
            select(MarketKnowledgePoint).where(
                MarketKnowledgePoint.article_id == article.id,
                MarketKnowledgePoint.enterprise_id == task.enterprise_id,
                MarketKnowledgePoint.deleted_at.is_(None),
            )
        )
    ).all():
        old.deleted_at = now
    for ordinal, content in enumerate(points):
        session.add(
            MarketKnowledgePoint(
                article_id=article.id,
                enterprise_id=task.enterprise_id,
                content=content,
                ordinal=ordinal,
            )
        )
    article.extract_status = "done"
    article.extract_error = None
    article.rules_version = MARKET_RULES_VERSION
    task.result = {"points": len(points), "rules_version": MARKET_RULES_VERSION}
    await session.commit()


# —— Agent 参考 provider（可替换策略；本期 strategy=all 全量读取已提炼要点）——
_STRATEGIES: dict[str, object] = {}


def register_strategy(name: str, fn) -> None:
    """注册参考收集策略：后续按相关性/任务类型筛选时替换实现，不改调用方。"""
    _STRATEGIES[name] = fn


async def _collect_all(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(
                MarketKnowledgePoint.article_id,
                MarketKnowledgePoint.content,
                MarketKnowledgePoint.ordinal,
                MarketKnowledgeArticle.title,
                MarketKnowledgeArticle.category,
            )
            .join(
                MarketKnowledgeArticle,
                MarketKnowledgePoint.article_id == MarketKnowledgeArticle.id,
            )
            .where(
                MarketKnowledgePoint.deleted_at.is_(None),
                MarketKnowledgeArticle.deleted_at.is_(None),
            )
            .order_by(MarketKnowledgeArticle.id, MarketKnowledgePoint.ordinal)
        )
    ).all()
    return [
        {
            "article_id": int(r.article_id),
            "content": r.content,
            "ordinal": int(r.ordinal),
            "title": r.title,
            "category": r.category,
        }
        for r in rows
    ]


register_strategy("all", _collect_all)


async def collect_reference_points(
    session: AsyncSession,
    *,
    strategy: str = "all",
    limit: int | None = None,
) -> dict:
    fn = _STRATEGIES.get(strategy) or _collect_all
    items = await fn(session)
    if limit:
        items = items[:limit]
    return {
        "count": len(items),
        "items": items,
        "strategy": strategy,
        "rules_version": MARKET_RULES_VERSION,
    }


def format_reference_block(items: list[dict]) -> str:
    if not items:
        return "（行情库暂无已提炼要点）"
    block = "\n".join(f"- {i['content']}" for i in items)
    if len(block) > settings.market_knowledge_ref_max_chars:
        block = block[: settings.market_knowledge_ref_max_chars].rstrip() + "\n（已截断）"
    return (
        "行情库提炼要点（低优先级辅助参考：仅作写作参考，招标文件要求与企业真实资料优先，"
        "不得虚构企业事实或把其他企业业绩/资质当作本企业事实）：\n" + block
    )


async def delete_article(session: AsyncSession, article_id: int) -> None:
    article = await session.scalar(
        select(MarketKnowledgeArticle).where(
            MarketKnowledgeArticle.id == article_id,
            MarketKnowledgeArticle.deleted_at.is_(None),
        )
    )
    if article is None:
        raise ValueError("行情资料不存在或已删除")
    now = datetime.now(timezone.utc)
    article.deleted_at = now
    for point in (
        await session.scalars(
            select(MarketKnowledgePoint).where(
                MarketKnowledgePoint.article_id == article.id,
                MarketKnowledgePoint.deleted_at.is_(None),
            )
        )
    ).all():
        point.deleted_at = now
    await session.commit()
