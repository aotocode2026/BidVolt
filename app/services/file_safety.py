"""文件安全管道（P1）：magic bytes 校验、压缩包防护、ClamAV 扫描。"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from app.config import settings

ALLOWED_EXTS = {
    ".pdf", ".ofd", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".txt", ".jpg", ".jpeg", ".png", ".bmp",
    ".tiff", ".zip", ".rar", ".7z",
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
_ZIP_EXTS = {".zip", ".docx", ".xlsx", ".pptx"}
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


def virus_scan(data: bytes) -> None:
    """按配置执行病毒扫描。virus_scan_required=True 时扫描不可用 = 拒绝入库。"""
    if not settings.virus_scan_required:
        return
    try:
        clean = scan_clamav(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"病毒扫描不可用（fail-closed）：{exc}") from exc
    if not clean:
        raise ValueError("文件被病毒扫描拦截")


def extract_zip(
    data: bytes,
    max_entries: int = 1000,
    max_total: int = 2 * 1024 * 1024 * 1024,
    max_depth: int = 3,
) -> list[dict]:
    """解压 ZIP，拒绝路径穿越/绝对路径/符号链接，返回 [{name, data}]。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"压缩包文件数超限（>{max_entries}）")
        total = sum(i.file_size for i in infos)
        if total > max_total:
            raise ValueError("压缩包解压总量超限")

        results: list[dict] = []
        for info in infos:
            name = info.filename
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or len(p.parts) > max_depth:
                raise ValueError(f"拒绝不安全的压缩条目：{name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # symlink
                raise ValueError(f"拒绝符号链接条目：{name}")
            if info.is_dir():
                continue
            results.append({"name": p.name, "data": zf.read(info)})
        return results
