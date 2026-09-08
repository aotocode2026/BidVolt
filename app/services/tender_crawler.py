"""招标公告站点抓取（Issue #32）：正文获取 + 附件发现 + 附件安全下载。

安全模型：
- 页面 URL 允许任意公开网址（仅 http/https），但内网/保留地址等 SSRF 目标一律拒绝；
- 附件下载逐跳（含重定向）校验：仅 http/https、逐跳解析 DNS、禁内网/保留地址、重定向上限；
- 附件大小上限 1GB（流式计数），不限制内容类型，但默认拦截高风险可执行扩展名；
- 需登录的附件（401/403 / 重定向到登录页 / 返回登录 HTML）判定为 skipped 并说明原因。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath

import httpx

from app.config import settings

MAX_REDIRECTS = 5
FILE_EXTS = {
    ".zip", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".ofd", ".rar", ".7z", ".txt", ".csv", ".html", ".htm", ".wps", ".et", ".dps",
}
LOGIN_MARKERS = ("login", "sso", "cas", "signin", "sign-in", "passport", "oauth")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 BidVolt/1.0"
)


class CrawlerError(Exception):
    """抓取失败（带稳定错误码，可落库）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AttachmentDownloadError(Exception):
    """单个附件下载失败；skip_login=True 表示“需登录，跳过而非报错”。"""

    def __init__(self, code: str, message: str, skip_login: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.skip_login = skip_login


@dataclass
class AttachmentLink:
    text: str
    href: str
    kind: str = "公告附件"  # 公告附件 / 招标文件
    login_candidate: bool = False
    is_action: bool = False  # javascript:void(0) 等需浏览器内点击触发下载


@dataclass
class RenderedDocument:
    html: str
    title: str = ""
    final_url: str = ""
    links: list[AttachmentLink] = field(default_factory=list)


def _blocked_exts() -> set[str]:
    exts = {
        e.strip().lower()
        for e in settings.tender_import_blocked_exts.split(",")
        if e.strip()
    }
    return {e if e.startswith(".") else f".{e}" for e in exts}


def validate_site_url(url: str) -> str:
    """校验并规范化页面 URL：任意公开网址，仅 http/https，内网/保留地址拒绝（SSRF）。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise CrawlerError("unsupported_scheme", "仅支持 http/https 链接")
    if not parsed.hostname:
        raise CrawlerError("invalid_url", "URL 缺少主机名")
    if parsed.username or parsed.password:
        raise CrawlerError("invalid_url", "URL 不允许携带用户信息")
    _validate_host(parsed.hostname)
    return url


def _validate_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise CrawlerError("dns_failed", f"域名解析失败：{host}") from exc
    if not infos:
        raise CrawlerError("dns_failed", f"域名无解析结果：{host}")
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
            raise CrawlerError("blocked_address", f"目标地址 {ip} 为内网/保留地址，已拒绝")


def _validate_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AttachmentDownloadError("unsupported_scheme", "附件链接仅支持 http/https")
    host = parsed.hostname
    if not host:
        raise AttachmentDownloadError("invalid_url", "附件链接缺少主机名")
    if parsed.username or parsed.password:
        raise AttachmentDownloadError("invalid_url", "附件链接不允许携带用户信息")
    _validate_host(host)
    return url


def _validate_page_hop(url: str) -> str:
    """公告页重定向逐跳 SSRF 校验（任意公开站点均可，内网/保留地址拒绝）。"""
    return _validate_download_url(url)


def parse_content_disposition(value: str | None) -> str | None:
    """解析 Content-Disposition 中的文件名（优先 filename*，退回 filename）。"""
    if not value:
        return None
    star = re.search(r"filename\*\s*=\s*(?:utf-8|UTF-8)''([^;]+)", value)
    if star:
        name = urllib.parse.unquote(star.group(1).strip().strip('"'))
        if name:
            return name
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', value)
    if plain:
        name = plain.group(1).strip()
        if name:
            return name
    return None


def _looks_like_login(response: httpx.Response, href: str) -> bool:
    url = str(response.url).lower()
    if response.status_code in (401, 403):
        return True
    if any(m in url for m in LOGIN_MARKERS):
        return True
    return False


def _maybe_reject_login_wall(
    data: bytes,
    content_type: str,
    href: str,
    filename: str,
    login_candidate: bool,
) -> None:
    """识别“登录墙冒充附件”：附件链接返回 HTML 登录页时跳过而非入库。"""
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype != "text/html":
        return
    url_low = href.lower()
    ext = PurePosixPath(urllib.parse.urlparse(href).path).suffix.lower()
    expects_file = ext not in ("", ".html", ".htm")
    head = data[:8192].decode("utf-8", errors="replace").lower()
    login_markers = any(m in url_low for m in LOGIN_MARKERS)
    login_text = ("请登录" in head) or ("验证码" in head)
    if login_markers or (login_candidate and (expects_file or login_text)) or (
        not login_candidate and expects_file
    ):
        raise AttachmentDownloadError(
            "login_required",
            "附件需登录或无权限下载，已跳过（可手动上传补充）",
            skip_login=True,
        )


def _ensure_ext(filename: str, content_type: str, href: str) -> str:
    if PurePosixPath(filename).suffix:
        return filename
    base = content_type.split(";", 1)[0].strip().lower()
    ext_map = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.rar": ".rar",
        "application/x-7z-compressed": ".7z",
        "text/html": ".html",
        "text/plain": ".txt",
    }
    suffix = PurePosixPath(urllib.parse.urlparse(href).path).suffix.lower()
    return filename + (suffix if suffix else ext_map.get(base, ""))


def _resolve_redirect(current: str, resp: httpx.Response) -> tuple[str, bool]:
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location")
        if not location:
            return current, False
        return urllib.parse.urljoin(current, location), True
    return current, False


async def download_attachment(
    href: str,
    max_bytes: int | None = None,
    login_candidate: bool = False,
) -> tuple[bytes, str, str]:
    """逐跳 SSRF 校验下载附件：返回 (data, filename, content_type)。

    命中登录信号（401/403/重定向登录页/登录 HTML）时抛 skip_login=True 的异常。
    """
    limit = max_bytes or settings.tender_import_attachment_max_bytes
    current = _validate_download_url(href)
    resp: httpx.Response | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0),
        follow_redirects=False,
        proxy=settings.http_proxy or None,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            _validate_download_url(current)
            resp = await client.get(current, headers={"User-Agent": DEFAULT_UA})
            next_url, redirect = _resolve_redirect(current, resp)
            if redirect:
                current = next_url
                continue
            if resp.status_code != 200:
                if _looks_like_login(resp, href):
                    raise AttachmentDownloadError(
                        "login_required",
                        "附件需登录或无权限下载，已跳过（可手动上传补充）",
                        skip_login=True,
                    )
                raise AttachmentDownloadError(
                    "http_error", f"附件下载失败：HTTP {resp.status_code}"
                )
            if _looks_like_login(resp, href):
                raise AttachmentDownloadError(
                    "login_required",
                    "附件需登录或无权限下载，已跳过（可手动上传补充）",
                    skip_login=True,
                )
            content_type = resp.headers.get("content-type", "")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise AttachmentDownloadError(
                    "too_large", f"附件超过大小上限（{limit} 字节）"
                )
            filename = parse_content_disposition(resp.headers.get("content-disposition"))
            if not filename:
                filename = PurePosixPath(urllib.parse.urlparse(current).path).name or "附件"
            filename = _ensure_ext(filename, content_type, current)
            ext = PurePosixPath(filename).suffix.lower()
            if ext in _blocked_exts():
                raise AttachmentDownloadError(
                    "blocked_ext", f"附件类型 {ext or '(无)'} 为高风险可执行文件，已拦截"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise AttachmentDownloadError(
                        "too_large", f"附件超过大小上限（{limit} 字节）"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise AttachmentDownloadError("empty_body", "附件内容为空")
            _maybe_reject_login_wall(data, content_type, href, filename, login_candidate)
            return data, filename, content_type
    raise AttachmentDownloadError("too_many_redirects", f"附件重定向超过 {MAX_REDIRECTS} 次")


class _AnchorCollector(HTMLParser):
    """静态 HTML 中提取 <a>：文本 + href。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_map = {k.lower(): v for k, v in attrs}
        self._current_href = attrs_map.get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href is not None:
            self.anchors.append(("".join(self._text).strip(), self._current_href))
            self._current_href = None
            self._text = []


def extract_links(
    base_url: str,
    anchors: list[tuple[str, str]],
) -> list[AttachmentLink]:
    """从 (text, href) 列表中发现附件链接（分类 + 去重 + 绝对化）。"""
    seen: set[str] = set()
    links: list[AttachmentLink] = []
    for text, href in anchors:
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        lowered = (text or "").lower()
        kind = None
        if any(k in lowered for k in ("招标文件", "获取招标文件", "下载招标文件")):
            kind = "招标文件"
        elif any(k in lowered for k in ("下载", "附件")):
            kind = "公告附件"
        path_ext = PurePosixPath(urllib.parse.urlparse(absolute).path).suffix.lower()
        if kind is None and path_ext not in FILE_EXTS:
            continue
        login_candidate = ("获取" in lowered) or any(m in absolute.lower() for m in LOGIN_MARKERS)
        links.append(
            AttachmentLink(
                text=text[:200],
                href=absolute,
                kind=kind or "公告附件",
                login_candidate=login_candidate,
            )
        )
    return links


def extract_actions(
    base_url: str,
    anchors: list[tuple[str, str]],
) -> list[AttachmentLink]:
    """浏览器渲染后的“下载按钮”发现：含 javascript:void(0) 等点击触发下载的元素。"""
    seen: set[str] = set()
    links: list[AttachmentLink] = []
    for text, href in anchors:
        text = (text or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if not any(k in lowered for k in ("下载", "附件", "获取", "招标文件")):
            continue
        href = (href or "").strip()
        dedupe_key = f"{text}|{href}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        absolute = href
        if href and not href.startswith(("javascript:", "mailto:", "#", "blob:")):
            absolute = urllib.parse.urljoin(base_url, href)
        kind = "招标文件" if any(k in lowered for k in ("招标文件",)) else "公告附件"
        login_candidate = ("获取" in lowered) or any(m in absolute.lower() for m in LOGIN_MARKERS)
        links.append(
            AttachmentLink(
                text=text[:200],
                href=absolute,
                kind=kind,
                login_candidate=login_candidate,
                is_action=absolute.startswith(("javascript:", "#", "blob:")) or not absolute,
            )
        )
    return links


class TenderPageFetcher:
    """页面抓取器：hash 路由 SPA 用无头浏览器渲染，其余静态页直连解析。

    用法：
        async with TenderPageFetcher(url) as f:
            doc = await f.fetch_and_discover()
            for link in doc.links:
                data, name, ct = await f.download(link)
    """

    def __init__(self, url: str) -> None:
        self.url = validate_site_url(url)
        self._use_browser = self._needs_browser(url)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._httpx = None

    @staticmethod
    def _needs_browser(url: str) -> bool:
        if not settings.tender_import_browser_enabled:
            return False
        parsed = urllib.parse.urlparse(url)
        return "#/" in url or "#!" in url or bool(parsed.fragment)

    async def __aenter__(self) -> TenderPageFetcher:
        if self._use_browser:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise CrawlerError(
                    "browser_unavailable", "无头浏览器运行时不可用（Playwright 未安装）"
                ) from exc
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass

    async def fetch_and_discover(self) -> RenderedDocument:
        if self._use_browser and self._page is not None:
            return await self._fetch_browser()
        return await self._fetch_static()

    async def _fetch_browser(self) -> RenderedDocument:
        await self._page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(settings.tender_import_render_wait_ms)
        title = (await self._page.title()).strip()
        final_url = self._page.url
        html = await self._page.content()
        # 先取普通 <a href> 文件链接，再取“下载按钮”类（javascript:void(0) 点击触发下载）
        anchors_raw: list[dict] = await self._page.eval_on_selector_all(
            "a",
            "els => els.map(e => ({t: (e.innerText || '').trim(), h: e.href || ''}))",
        )
        anchors = [(a.get("t") or "", a.get("h") or "") for a in anchors_raw]
        links = extract_links(final_url, anchors)
        actions_raw: list[dict] = await self._page.evaluate(
            """() => {
              const kw = ['下载', '附件', '获取', '招标文件'];
              const out = [];
              for (const e of document.querySelectorAll('a,button,span,div')) {
                const t = (e.innerText || '').trim();
                if (!t || t.length > 30) continue;
                if (!kw.some(k => t.includes(k))) continue;
                if (e.offsetParent === null) continue;
                const href = e.getAttribute && (e.getAttribute('href') || e.href || '');
                out.push({t, h: href});
              }
              return out;
            }"""
        )
        action_pairs = [(a.get("t") or "", a.get("h") or "") for a in actions_raw]
        action_links = extract_actions(final_url, action_pairs)
        # 动作类与普通链接按 (text, href) 去重，动作优先（真实 href 的普通链接保留）
        existing = {(item.text, item.href) for item in links}
        for action in action_links:
            if action.is_action and (action.text, "") not in existing:
                links.append(action)
                existing.add((action.text, ""))
            elif not action.is_action and (action.text, action.href) not in existing:
                links.append(action)
                existing.add((action.text, action.href))
        return RenderedDocument(html=html, title=title, final_url=final_url, links=links)

    async def _fetch_static(self) -> RenderedDocument:
        self._httpx = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0),
            follow_redirects=False,
            proxy=settings.http_proxy or None,
        )
        current = self.url
        for _hop in range(MAX_REDIRECTS + 1):
            _validate_page_hop(current)
            resp = await self._httpx.get(current, headers={"User-Agent": DEFAULT_UA})
            next_url, redirect = _resolve_redirect(current, resp)
            if redirect:
                current = next_url
                continue
            if resp.status_code != 200:
                raise CrawlerError("http_error", f"公告页面抓取失败：HTTP {resp.status_code}")
            html = resp.text
            parser = _AnchorCollector()
            parser.feed(html)
            links = extract_links(str(resp.url), parser.anchors)
            title = _extract_title(html)
            return RenderedDocument(html=html, title=title, final_url=str(resp.url), links=links)
        raise CrawlerError("too_many_redirects", f"公告页面重定向超过 {MAX_REDIRECTS} 次")

    async def download(self, link: AttachmentLink) -> tuple[bytes, str, str]:
        """下载附件：浏览器会话存在时用其请求上下文（携带站点 cookie），否则 httpx。"""
        if link.is_action and self._page is not None:
            return await self._download_via_click(link)
        if self._context is not None:
            # 浏览器 cookie 下载：请求前先做逐跳 SSRF 预校验（探测重定向终点）
            final = await self._probe_final_url(link.href)
            _validate_download_url(final)
            resp = await self._context.request.get(
                final,
                max_redirects=MAX_REDIRECTS,
                timeout=180_000,
                headers={"User-Agent": DEFAULT_UA},
            )
            if resp.status in (401, 403):
                raise AttachmentDownloadError(
                    "login_required",
                    "附件需登录或无权限下载，已跳过（可手动上传补充）",
                    skip_login=True,
                )
            if resp.status != 200:
                raise AttachmentDownloadError(
                    "http_error", f"附件下载失败：HTTP {resp.status}"
                )
            data = await resp.body()
            limit = settings.tender_import_attachment_max_bytes
            if len(data) > limit:
                raise AttachmentDownloadError("too_large", f"附件超过大小上限（{limit} 字节）")
            if not data:
                raise AttachmentDownloadError("empty_body", "附件内容为空")
            filename = parse_content_disposition(resp.headers.get("content-disposition"))
            if not filename:
                filename = PurePosixPath(urllib.parse.urlparse(resp.url).path).name or "附件"
            filename = _ensure_ext(filename, resp.headers.get("content-type", ""), resp.url)
            ext = PurePosixPath(filename).suffix.lower()
            if ext in _blocked_exts():
                raise AttachmentDownloadError(
                    "blocked_ext", f"附件类型 {ext or '(无)'} 为高风险可执行文件，已拦截"
                )
            _maybe_reject_login_wall(
                data,
                resp.headers.get("content-type", ""),
                resp.url,
                filename,
                link.login_candidate,
            )
            return data, filename, resp.headers.get("content-type", "")
        return await download_attachment(link.href, login_candidate=link.login_candidate)

    async def _download_via_click(self, link: AttachmentLink) -> tuple[bytes, str, str]:
        """浏览器内点击“下载按钮”并拦截下载（兼容站点前端加密/验签，无需逆向协议）。"""
        locator = self._page.get_by_text(link.text, exact=True)
        if await locator.count() == 0:
            locator = self._page.get_by_text(link.text)
        target = locator.first
        tmp_path: str | None = None
        try:
            async with self._page.expect_download(
                timeout=settings.tender_import_click_timeout_ms
            ) as dl_info:
                await target.click()
            download = await dl_info.value
            filename = download.suggested_filename or _basename_plain(link.text)
            fd, tmp_path = tempfile.mkstemp(
                prefix="bidvolt_attach_", dir=tempfile.gettempdir()
            )
            os.close(fd)
            await download.save_as(tmp_path)
            data = _read_bytes(tmp_path, settings.tender_import_attachment_max_bytes)
            url = download.url or ""
            content_type = ""
            _maybe_reject_login_wall(data, content_type, url, filename, link.login_candidate)
            ext = PurePosixPath(filename).suffix.lower()
            if ext in _blocked_exts():
                raise AttachmentDownloadError(
                    "blocked_ext", f"附件类型 {ext or '(无)'} 为高风险可执行文件，已拦截"
                )
            return data, filename, content_type
        except AttachmentDownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 点击未触发下载
            if link.login_candidate:
                raise AttachmentDownloadError(
                    "login_required",
                    "附件需登录或无权限下载，已跳过（可手动上传补充）",
                    skip_login=True,
                ) from exc
            raise AttachmentDownloadError(
                "click_no_download",
                "点击下载未触发（可能需要登录或站点限制）：" + str(exc)[:200],
            ) from exc
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def _probe_final_url(self, href: str) -> str:
        """不下载正文，仅逐跳校验重定向链并返回终点 URL（浏览器下载前的 SSRF 预检）。"""
        current = _validate_download_url(href)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0),
            follow_redirects=False,
            proxy=settings.http_proxy or None,
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                _validate_download_url(current)
                resp = await client.get(current, headers={"User-Agent": DEFAULT_UA})
                next_url, redirect = _resolve_redirect(current, resp)
                if not redirect:
                    if resp.status_code in (401, 403):
                        raise AttachmentDownloadError(
                            "login_required",
                            "附件需登录或无权限下载，已跳过（可手动上传补充）",
                            skip_login=True,
                        )
                    if resp.status_code != 200:
                        raise AttachmentDownloadError(
                            "http_error", f"附件下载失败：HTTP {resp.status_code}"
                        )
                    return str(resp.url)
                current = next_url
        raise AttachmentDownloadError("too_many_redirects", f"附件重定向超过 {MAX_REDIRECTS} 次")


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def _basename_plain(text: str) -> str:
    name = text.strip()
    return name if name else "附件"


def _read_bytes(path: str, limit: int) -> bytes:
    size = os.path.getsize(path)
    if size > limit:
        raise AttachmentDownloadError("too_large", f"附件超过大小上限（{limit} 字节）")
    with open(path, "rb") as fh:
        data = fh.read()
    if not data:
        raise AttachmentDownloadError("empty_body", "附件内容为空")
    return data
