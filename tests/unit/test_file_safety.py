from __future__ import annotations

import io
import zipfile

import pytest

from app.services import file_safety


def test_sniff_mime():
    assert file_safety.sniff_mime(b"%PDF-1.7 ...") == "application/pdf"
    assert file_safety.sniff_mime(b"PK\x03\x04...") == "application/zip"
    assert file_safety.sniff_mime(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert file_safety.sniff_mime(b"hello plain text") == "text/plain"


def test_validate_upload_magic_mismatch():
    with pytest.raises(ValueError, match="扩展名不符"):
        file_safety.validate_upload("fake.pdf", b"not a pdf at all")


def test_validate_upload_rejects_unknown_ext():
    with pytest.raises(ValueError, match="不允许的文件类型"):
        file_safety.validate_upload("evil.exe", b"MZ...")


def test_validate_upload_accepts_txt():
    mime, ext = file_safety.validate_upload("材料.txt", "招标公告内容".encode("utf-8"))
    assert mime == "text/plain"
    assert ext == ".txt"


def test_extract_zip_rejects_path_traversal():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    with pytest.raises(ValueError, match="不安全的压缩条目"):
        file_safety.extract_zip(buf.getvalue())


def test_extract_zip_rejects_absolute_path():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "bad")
    with pytest.raises(ValueError, match="不安全的压缩条目"):
        file_safety.extract_zip(buf.getvalue())


def test_extract_zip_normal_entries():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("sub/b.txt", "world")
    entries = file_safety.extract_zip(buf.getvalue())
    assert {e["name"] for e in entries} == {"a.txt", "b.txt"}
