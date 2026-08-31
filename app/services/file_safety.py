"""文件安全管道（P1）：magic bytes 校验、压缩包防护、ClamAV 扫描。"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from pathlib import PurePosixPath

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTS = {
    ".pdf", ".ofd", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".txt", ".md", ".jpg", ".jpeg", ".png", ".bmp",
    ".tiff", ".zip", ".rar", ".7z", ".html", ".htm",
}


def sniff_mime(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
        return "application/zip"  # docx/xlsx/pptx/zip 均为 OOXML 容器
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "application/octet-stream"  # OLE2：doc/xls/ppt
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"Rar!":
        return "application/vnd.rar"
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return "application/x-7z-compressed"
    return "text/plain"


_EXT_MAGIC: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
}
_ZIP_EXTS = {".zip", ".docx", ".xlsx", ".pptx", ".ofd"}
_OLE_EXTS = {".doc", ".xls", ".ppt"}


def validate_upload(name: str, data: bytes, max_bytes: int | None = None) -> tuple[str, str]:
    """返回 (mime, ext)。校验失败抛 ValueError。"""
    if not data:
        raise ValueError("空文件")
    limit = max_bytes or settings.max_upload_bytes
    if len(data) > limit:
        raise ValueError(f"文件超过大小上限（{limit} 字节）")

    ext = PurePosixPath(name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不允许的文件类型：{ext or '(无扩展名)'}")

    mime = sniff_mime(data)
    expected = _EXT_MAGIC.get(ext)
    if expected is not None and mime != expected:
        raise ValueError(f"文件内容与扩展名不符：{ext}（magic bytes={mime}）")
    if ext in _ZIP_EXTS and mime != "application/zip":
        raise ValueError(f"{ext} 应为 OOXML/ZIP 容器，实际 magic={mime}")
    if ext in _OLE_EXTS and mime != "application/octet-stream":
        raise ValueError(f"{ext} 应为 OLE2 容器，实际 magic={mime}")
    return mime, ext


def scan_clamav(data: bytes) -> bool:
    """ClamAV 扫描；不可用时抛异常由调用方决定 fail-closed。"""
    import clamd

    cd = clamd.ClamdUnixSocket()
    result = cd.instream(io.BytesIO(data))
    status = result.get("stream", ("", "OK"))[0]
    return status == "OK"


_SCAN_ATTEMPTS = 3
_SCAN_BACKOFF_SECONDS = 1.0


def virus_scan(data: bytes) -> None:
    """按配置执行病毒扫描。

    可用性优先（产品决定）：clamd 瞬时抖动先退避重试；全部失败时**跳过查杀放行**
    （fail-open，记录告警日志）——病毒确实被扫出来（FOUND）仍一律拦截。
    管理员可按需将 virus_scan_required 关掉以完全停用扫描。"""
    if not settings.virus_scan_required:
        return
    for attempt in range(_SCAN_ATTEMPTS):
        try:
            clean = scan_clamav(data)
            if not clean:
                raise ValueError("文件被病毒扫描拦截")
            return
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 瞬时故障重试
            if attempt < _SCAN_ATTEMPTS - 1:
                time.sleep(_SCAN_BACKOFF_SECONDS * (attempt + 1))
                continue
            logger.warning("病毒扫描不可用（连续 %s 次失败）：跳过查杀放行（fail-open）：%s", _SCAN_ATTEMPTS, exc)


def extract_zip(
    data: bytes,
    max_entries: int = 1000,
    max_total: int = 2 * 1024 * 1024 * 1024,
    max_depth: int = 8,
) -> list[dict]:
    """解压 ZIP：拒绝路径穿越/绝对路径/符号链接，返回 [{name, path, data}]。

    name=条目文件名（basename）；path=包内相对路径（保留目录层次，供入库溯源展示）。
    报错信息逐类区分（文件数/总量/穿越/绝对路径/符号链接/层级过深），用户能看懂是哪类问题。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"压缩包内文件数超过限制（{len(infos)} 个 > 上限 {max_entries} 个）：请分批打包后重新上传")
        total = sum(i.file_size for i in infos)
        if total > max_total:
            raise ValueError(f"压缩包解压总量超过限制（{total / 1024 / 1024 / 1024:.2f} GB > 上限 2 GB）")

        results: list[dict] = []
        for info in infos:
            name = info.filename
            # 中文 Windows 打包的 zip 文件名常为 GBK 编码（未置 UTF-8 标志）：
            # zipfile 按 cp437 解出乱码——检测标志位，非 UTF-8 时按 cp437→GBK 还原
            if not (info.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("gbk")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            p = PurePosixPath(name)
            if p.is_absolute():
                raise ValueError(f"拒绝绝对路径压缩条目：{name}（请重新打包为相对路径）")
            if ".." in p.parts:
                raise ValueError(f"拒绝路径穿越压缩条目：{name}（包内路径含 ..）")
            if len(p.parts) > max_depth:
                raise ValueError(f"压缩条目目录层级超过限制（{len(p.parts)} 层 > 上限 {max_depth} 层）：{name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # symlink
                raise ValueError(f"拒绝符号链接条目：{name}")
            if info.is_dir():
                continue
            results.append({"name": p.name, "path": str(p), "data": zf.read(info)})
        return results
