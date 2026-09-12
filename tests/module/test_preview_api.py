"""浏览器内 Office 预览（issue #63）：docx→PDF、xlsx→表格网格、其余→unsupported。"""

from __future__ import annotations

import io

from app.services import preview_service


def _headers(client, email="pv@test.com"):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": "预览企业"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload(client, headers, data: bytes, name: str, mime: str) -> dict:
    r = client.post(
        "/api/v1/files/upload",
        data={"target": "enterprise"},
        files=[("files", (name, io.BytesIO(data), mime))],
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["files"][0]


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "报价明细表"
    ws.append(["序号", "名称", "含税单价"])
    ws.append([1, "虚拟电厂平台", 194.0])
    ws.append([2, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx_bytes(text: str = "投标响应文件正文") -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_preview_kind_mapping():
    assert preview_service.preview_kind_for_ext(".docx") == "pdf"
    assert preview_service.preview_kind_for_ext("PDF") == "pdf"
    assert preview_service.preview_kind_for_ext(".xlsx") == "sheet"
    assert preview_service.preview_kind_for_ext(".xls") == "sheet"
    assert preview_service.preview_kind_for_ext(".pptx") == "pdf"
    assert preview_service.preview_kind_for_ext(".ppt") == "pdf"
    assert preview_service.preview_kind_for_ext(".zip") == "unsupported"
    assert preview_service.preview_kind_for_ext(None) == "unsupported"


def test_xlsx_preview_returns_cell_grid(client):
    h = _headers(client)
    entry = _upload(
        client,
        h,
        _xlsx_bytes(),
        "报价单.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert entry["preview_kind"] == "sheet"

    r = client.get(f"/api/v1/files/{entry['file_id']}/preview", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "sheet"
    sheet = body["sheets"][0]
    assert sheet["name"] == "报价明细表"
    assert sheet["cells"][0] == ["序号", "名称", "含税单价"]
    assert sheet["cells"][1] == [1, "虚拟电厂平台", 194.0]
    # 行尾空单元格裁剪，占位行不膨胀载荷
    assert sheet["cells"][2] == [2]


def test_docx_preview_converts_to_pdf_and_caches(client, monkeypatch):
    calls = {"n": 0}

    def fake_convert(data: bytes, filename: str, target_ext: str) -> bytes:
        calls["n"] += 1
        return b"%PDF-1.4\n%fake\n%%EOF\n"

    monkeypatch.setattr(preview_service, "_soffice_convert_sync", fake_convert)
    h = _headers(client, "pv2@test.com")
    entry = _upload(
        client,
        h,
        _docx_bytes(),
        "技术标.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert entry["preview_kind"] == "pdf"
    file_id = entry["file_id"]

    first = client.get(f"/api/v1/files/{file_id}/preview", headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "pdf"
    assert first.json()["cached"] is False

    second = client.get(f"/api/v1/files/{file_id}/preview", headers=h)
    assert second.json()["cached"] is True
    assert calls["n"] == 1  # 内容未变只转换一次

    pdf = client.get(f"/api/v1/files/{file_id}/preview.pdf", headers=h)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    assert "inline" in pdf.headers["content-disposition"]


def test_unsupported_preview_reports_reason(client):
    h = _headers(client, "pv3@test.com")
    entry = _upload(client, h, "普通文本".encode(), "说明.txt", "text/plain")
    assert entry["preview_kind"] == "unsupported"

    body = client.get(f"/api/v1/files/{entry['file_id']}/preview", headers=h).json()
    assert body["kind"] == "unsupported"
    assert "下载" in body["reason"]


def test_presentation_preview_converts_to_pdf(client, monkeypatch):
    """issue #64：演示文稿（.ppt/.pptx）经 LibreOffice Impress 转 PDF 预览。"""
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    calls = {"n": 0}

    def fake_convert(data: bytes, filename: str, target_ext: str) -> bytes:
        calls["n"] += 1
        assert target_ext == "pdf"
        assert filename.endswith((".ppt", ".pptx"))
        return b"%PDF-1.4\n%fake\n%%EOF\n"

    monkeypatch.setattr(preview_service, "_soffice_convert_sync", fake_convert)

    async def _run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            src = preview_service.PreviewSource(
                source_type="file",
                source_id=990001,
                enterprise_id=1,
                filename="供应商投标注意事项.pptx",
                ext=".pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                version_key="test-pptx-1",
                data=b"PK\x03\x04fake-pptx",
            )
            manifest = await preview_service.build_manifest(session, src)
            pdf, mime = await preview_service.get_pdf(session, src)
            cached_manifest = await preview_service.build_manifest(session, src)
        await engine.dispose()
        return manifest, pdf, mime, cached_manifest

    manifest, pdf, mime, cached_manifest = asyncio.run(_run())
    assert manifest["kind"] == "pdf"
    assert manifest["converted"] is True
    assert mime == "application/pdf"
    assert pdf.startswith(b"%PDF-")
    assert cached_manifest["cached"] is True
    assert calls["n"] == 1  # 内容未变只转换一次
