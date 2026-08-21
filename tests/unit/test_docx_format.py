"""DOCX 导出回归（Issue #8 + 产品要求：唯一生成路径=底稿式；全部改动以修订模式+批注记录）。"""

from __future__ import annotations

import io
import zipfile
from urllib.parse import unquote

from docx import Document
from docx.oxml.ns import qn

from app.services.export_service import docx_from_template


def _make_source() -> bytes:
    doc = Document()
    doc.add_paragraph("第一章 投标人须知")
    doc.add_paragraph("本采购由（采购人）实施，项目为（项目名称）。")
    doc.add_paragraph("供应商名称：（响应供应商名称）")
    doc.add_paragraph("联系电话：____")
    tb = doc.add_table(rows=1, cols=2)
    tb.rows[0].cells[0].text = "条款"
    tb.rows[0].cells[1].text = "说明"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _doc_xml(data: bytes):
    """从产物 zip 读 document.xml 并用纯 lxml 解析（Word 实际读取的序列化形态）。

    注意：不能用 python-docx 重开后的元素做 itertext——其 CT_P/CT_R 的 .text 是
    计算属性，逐层重复取值会让同一文本出现多份（读取侧假象，序列化产物无此问题）。"""
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return etree.fromstring(zf.read("word/document.xml"))


def _all_text(data: bytes) -> str:
    return "".join(_doc_xml(data).itertext())


def _count_el(data: bytes, local_tag: str) -> int:
    return sum(1 for e in _doc_xml(data).iter() if e.tag.split("}")[-1] == local_tag)


def _comment_texts(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "word/comments.xml" in names, f"缺少批注部件，包内: {names[:10]}"
        return zf.read("word/comments.xml").decode("utf-8")


def test_draft_export_keeps_whole_source_and_fills_tracked(tmp_path):
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {
        "buyer": "测试招标人",
        "project_name": "测试采购项目",
        "supplier_name": "测试供应商",
        "supplement_nodes": [
            {"type": "heading", "text": "一、总体响应说明"},
            {"type": "paragraph", "text": "我方完全响应采购文件全部要求。"},
        ],
    }
    data = docx_from_template(str(src), model)
    joined = _all_text(data)
    doc = Document(io.BytesIO(data))
    # 整本保留：原段落都在，表格保留
    assert "第一章 投标人须知" in joined
    assert len(doc.tables) == 1
    # 已知字段回填（修订插入可见）
    assert "本采购由测试招标人实施，项目为测试采购项目。" in joined
    assert "供应商名称：测试供应商" in joined
    assert "联系电话：【待补充】" in joined
    # 补充节追加
    assert "补充响应内容" in joined
    assert "我方完全响应采购文件全部要求。" in joined
    # 修订模式：删除（原文删除线）与插入标记、批注引用
    assert _count_el(data, "ins") > 0
    assert _count_el(data, "del") > 0
    assert _count_el(data, "commentReference") > 0
    # 批注部件存在且说明来源
    comments = _comment_texts(data)
    assert "来源" in comments and "BidVolt" in comments


def test_draft_export_fills_unknown_with_placeholder(tmp_path):
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {"buyer": "", "project_name": "", "supplier_name": "", "supplement_nodes": []}
    data = docx_from_template(str(src), model)
    joined = _all_text(data)
    assert "【待补充：招标人名称】" in joined
    assert "【待补充：项目名称】" in joined
    assert "【待补充：供应商名称】" in joined


def test_draft_export_supplement_headings_bold_without_style_dependency(tmp_path):
    """追加标题不依赖源文档是否含内置 Heading 样式（国网模板曾因此 KeyError 回退）。"""
    src = tmp_path / "source.docx"
    src.write_bytes(_make_source())
    model = {
        "buyer": "",
        "project_name": "",
        "supplier_name": "",
        "supplement_nodes": [
            {"type": "heading", "text": "补充节标题"},
            {"type": "paragraph", "text": "（采购人）应对本项目负责。"},
        ],
    }
    data = docx_from_template(str(src), model)
    joined = _all_text(data)
    assert "【待补充：招标人名称】应对本项目负责。" in joined
    # 标题加粗（w:b 存在）
    assert _count_el(data, "b") > 0
    # 补充节内容标记为插入
    assert _count_el(data, "ins") > 0


def test_draft_label_format():
    from app.services.export_service import _draft_label

    assert _draft_label("采购文件.docx", "商务标") == "商务标(底稿:采购文件.docx)"
    assert _draft_label(None, "商务标") == "商务标"


def test_draft_download_filename_marks_source(client):
    """下载文件名标注底稿来源（产品要求：一眼可见底稿是谁）。"""
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "fn@test.com", "password": "Abc12345", "enterprise_name": "文件名企业"},
    )
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]

    src = Document()
    src.add_paragraph("采购文件原文。")
    buf = io.BytesIO()
    src.save(buf)
    up = client.post(
        "/api/v1/files/upload",
        data={"target": "project", "project_id": str(pid)},
        files=[("files", ("采购文件.docx", buf.getvalue(), "application/octet-stream"))],
        headers=h,
    )
    assert up.status_code == 200

    did = client.post(
        "/api/v1/deliverables",
        json={"project_id": pid, "deliverable_type": 1, "title": "商务标"},
        headers=h,
    ).json()["deliverable_id"]
    client.post(
        f"/api/v1/deliverables/{did}/versions",
        json={"content": {"nodes": [{"id": "n1", "type": "paragraph", "text": "商务响应"}]}},
        headers=h,
    )
    r = client.get(f"/api/v1/deliverables/{did}/versions/1/download", headers=h)
    assert r.status_code == 200
    disposition = unquote(r.headers["content-disposition"])
    assert "底稿:采购文件.docx" in disposition, disposition
