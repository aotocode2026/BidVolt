from __future__ import annotations

import io
import struct
import zipfile

import pytest

from app.services import file_safety


def _make_mixed_encoding_zip(entries: dict[str, bytes]) -> bytes:
    """构造“中央目录 GBK 无 UTF-8 标志 + 本地头 UTF-8”的混合编码 zip（#35 场景）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    raw = bytearray(buf.getvalue())
    eocd_pos = raw.rfind(b"PK\x05\x06")
    assert eocd_pos != -1
    cd_offset, = struct.unpack_from("<I", raw, eocd_pos + 16)
    total, = struct.unpack_from("<H", raw, eocd_pos + 10)
    comment_len, = struct.unpack_from("<H", raw, eocd_pos + 20)
    comment = bytes(raw[eocd_pos + 22: eocd_pos + 22 + comment_len])

    pos = cd_offset
    rebuilt = bytearray()
    for _ in range(total):
        assert raw[pos:pos + 4] == b"PK\x01\x02"
        flag, = struct.unpack_from("<H", raw, pos + 8)
        name_len, = struct.unpack_from("<H", raw, pos + 28)
        extra_len, = struct.unpack_from("<H", raw, pos + 30)
        clen, = struct.unpack_from("<H", raw, pos + 32)
        name_bytes = bytes(raw[pos + 46: pos + 46 + name_len])
        name = name_bytes.decode("utf-8")
        gbk = name.encode("gbk")
        new_flag = flag & ~0x800
        header = (
            bytes(raw[pos: pos + 8])
            + struct.pack("<H", new_flag)
            + bytes(raw[pos + 10: pos + 28])
            + struct.pack("<HHH", len(gbk), extra_len, clen)
            + bytes(raw[pos + 34: pos + 46])
        )
        rebuilt += header + gbk + bytes(raw[pos + 46 + name_len: pos + 46 + name_len + extra_len + clen])
        pos += 46 + name_len + extra_len + clen

    new_cd_size = len(rebuilt)
    eocd = (
        b"PK\x05\x06"
        + bytes(raw[eocd_pos + 4: eocd_pos + 12])
        + struct.pack("<I", new_cd_size)
        + struct.pack("<I", cd_offset)
        + struct.pack("<H", comment_len)
        + comment
    )
    return bytes(raw[:cd_offset]) + bytes(rebuilt) + eocd


def test_mixed_encoding_zip_normalized_and_extracted():
    """#35：中央目录 GBK/本地头 UTF-8 的包不得被误判损坏，解包名与内容正确。"""
    payload = "内容".encode("utf-8")
    data = _make_mixed_encoding_zip({"材料.txt": payload})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is not None  # 修复前：被误判为损坏条目
    normalized = file_safety.normalize_zip(data)
    with zipfile.ZipFile(io.BytesIO(normalized)) as zf:
        assert zf.testzip() is None
        assert zf.infolist()[0].filename == "材料.txt"
    out = file_safety.extract_zip(data)
    assert out[0]["name"] == "材料.txt"
    assert out[0]["data"] == payload


def test_normalize_zip_ascii_is_noop():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", b"abc")
    data = buf.getvalue()
    assert file_safety.normalize_zip(data) == data


def test_virus_scan_disabled_skips(monkeypatch):
    monkeypatch.setattr(file_safety.settings, "virus_scan_required", False)
    called = {"n": 0}

    def fake_scan(data):
        called["n"] += 1
        return True

    monkeypatch.setattr(file_safety, "scan_clamav", fake_scan)
    file_safety.virus_scan(b"x")
    assert called["n"] == 0


def test_virus_scan_required(monkeypatch):
    """required=True：扫出病毒一律拦截；clamd 不可用连续重试后 fail-open 放行（产品决定）。"""
    monkeypatch.setattr(file_safety.settings, "virus_scan_required", True)

    def infected(data):
        return False

    def unavailable(data):
        raise RuntimeError("clamd down")

    monkeypatch.setattr(file_safety, "scan_clamav", infected)
    with pytest.raises(ValueError, match="拦截"):
        file_safety.virus_scan(b"x")

    attempts = {"n": 0}

    def unavailable_counting(data):
        attempts["n"] += 1
        raise RuntimeError("clamd down")

    monkeypatch.setattr(file_safety, "scan_clamav", unavailable_counting)
    monkeypatch.setattr(file_safety.time, "sleep", lambda s: None)  # 测试不真睡
    file_safety.virus_scan(b"x")  # 不抛：3 次失败后 fail-open 放行
    assert attempts["n"] == 3


def test_clamav_eicar_detected():
    """EICAR 测试样本被 ClamAV 拦截（A-5）；无 clamd 环境跳过。"""
    EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    try:
        clean = file_safety.scan_clamav(EICAR)
    except Exception:  # noqa: BLE001 - 本地无 clamd
        pytest.skip("ClamAV 不可用")
    assert clean is False


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
    mime, ext = file_safety.validate_upload("材料.txt", "招标公告内容".encode())
    assert mime == "text/plain"
    assert ext == ".txt"


def test_validate_upload_ofd_zip_container():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OFD.xml", "<ofd:OFD/>")
    mime, ext = file_safety.validate_upload("招标文件.ofd", buf.getvalue())
    assert mime == "application/zip"
    assert ext == ".ofd"


def test_extract_zip_rejects_path_traversal():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    with pytest.raises(ValueError, match="路径穿越压缩条目"):
        file_safety.extract_zip(buf.getvalue())


def test_extract_zip_rejects_absolute_path():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "bad")
    with pytest.raises(ValueError, match="绝对路径压缩条目"):
        file_safety.extract_zip(buf.getvalue())


def test_extract_zip_normal_entries():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("sub/b.txt", "world")
    entries = file_safety.extract_zip(buf.getvalue())
    assert {e["name"] for e in entries} == {"a.txt", "b.txt"}


def test_extract_zip_rejects_oversize_total():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("big.txt", b"x" * 1000)
    with pytest.raises(ValueError, match="解压总量超过限制"):
        file_safety.extract_zip(buf.getvalue(), max_total=100)


def test_extract_zip_rejects_too_many_entries():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}.txt", b"x")
    with pytest.raises(ValueError, match="文件数超过限制"):
        file_safety.extract_zip(buf.getvalue(), max_entries=3)


def test_extract_zip_rejects_deep_nesting():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b/c/d/e.txt", b"deep")
    with pytest.raises(ValueError, match="层级超过限制"):
        file_safety.extract_zip(buf.getvalue(), max_depth=3)


def test_extract_zip_rejects_symlink_entry():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16  # symlink
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="符号链接"):
        file_safety.extract_zip(buf.getvalue())
