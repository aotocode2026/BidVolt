"""DOCX 导出回归（Issue #8 + 产品要求：唯一生成路径=底稿式；全部改动以修订模式+批注记录）。"""

from __future__ import annotations

import io
import zipfile
from urllib.parse import unquote

from docx import Document

from app.services.export_service import docx_from_template


def _make_source() -> bytes:
    doc = Document()
    doc.add_paragraph("第一章 投标人须知")
    doc.add_paragraph("本采购由（采购人）实施，项目为（项目名称）。")
    doc.add_paragraph("供应商名称：（响应供应商名称）")
    doc.add_paragraph("联系电话：____")
    doc.add_paragraph("致：采购人")
    # 授权委托书形态：标签紧邻空位（下划线才是字段本身）
    doc.add_paragraph(
        "特授权____代表我方全权办理____（项目名称）（采购编号）（分标编号）（包号）"
        "项目的响应、谈判、签约、执行等具体工作。"
    )
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
    # 应答函裸标签形态：致：采购人 → 致：真实招标人
    assert "致：测试招标人" in joined
    # 标签紧邻空位整体回填：____（项目名称）→ 项目名，而不是【待补充】+项目名
    assert "全权办理测试采购项目（采购编号）" in joined
    assert "【待补充】测试采购项目" not in joined
    # 补充节追加
    assert "补充响应内容" in joined
    assert "我方完全响应采购文件全部要求。" in joined
    # 修订模式：删除（原文删除线）与插入标记、批注引用
    assert _count_el(data, "ins") > 0
    assert _count_el(data, "del") > 0
    assert _count_el(data, "commentReference") > 0
    # Word 显示修订的前提：settings.xml 含 trackChanges
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        settings = zf.read("word/settings.xml").decode("utf-8")
    assert "<w:trackChanges" in settings
    # 批注部件存在且说明来源
    comments = _comment_texts(data)
    assert "来源" in comments and "BidVolt" in comments
    # 批注范围顺序：commentRangeStart → del → ins → commentRangeEnd → commentReference
    root = _doc_xml(data)
    for p in root.iter():
        if p.tag.split("}")[-1] != "p":
            continue
        tags = [ch.tag.split("}")[-1] for ch in p if isinstance(ch.tag, str)]
        if "commentRangeStart" not in tags:
            continue
        i_start = tags.index("commentRangeStart")
        i_del = tags.index("del")
        i_ins = tags.index("ins")
        i_end = tags.index("commentRangeEnd")
        i_ref = next(i for i, t in enumerate(tags) if t == "r")
        assert i_start < i_del < i_ins < i_end < i_ref, tags


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


def test_split_supplement_tolerates_string_nodes():
    """撰写内容节点兼容裸字符串（新方案 agent 落库形状）与混合形状。"""
    from app.services.export_service import _split_supplement

    titles = ["（一）响应函及报价汇总表", "（二）报价明细表"]
    nodes = [
        {"type": "heading", "text": "（一）响应函及报价汇总表"},
        "我方承诺……（字符串节点）",
        {"type": "heading", "text": "（二）报价明细表"},
        {"type": "paragraph", "text": "报价明细"},
    ]
    matched, rest = _split_supplement(titles, nodes)
    assert len(matched[0]) == 2  # heading + 字符串节点（归一到 paragraph）分配到第 0 条
    assert len(matched[1]) == 2  # heading + paragraph 分配到第 1 条
    assert rest == []


def test_labeled_space_blank_fill():
    """带标签空位（空格/零宽形态）：有资料回填、无资料原位【待补充】，全程修订模式。"""
    from docx import Document

    src = Document()
    src.add_paragraph("1. 我方已仔细研究了采购编号：    ，项目名称：   ，分标名称：   ，包名称：   采购文件的全部内容。")
    src.add_paragraph("响应供应商：")
    src.add_paragraph("响应供应商：（盖章）")
    src.add_paragraph("电话：")
    src.add_paragraph("附表1 项目名称：，采购编号：，包号：，单位：万元人民币")
    buf = io.BytesIO()
    src.save(buf)
    p = __import__("pathlib").Path(__import__("tempfile").gettempdir()) / "label_blank_src.docx"
    p.write_bytes(buf.getvalue())

    from app.services.export_service import _FillSession

    doc = Document(str(p))
    sess = _FillSession(doc, "采购人甲", "项目乙", "供应商丙", "412623-1")
    sess.apply_to_doc()
    data = sess.finish()

    joined = _all_text(data)
    assert "采购编号：412623-1，项目名称：项目乙，分标名称：【待补充：分标名称】，包名称：【待补充：包名称】采购文件的全部内容" in joined
    assert "响应供应商：供应商丙" in joined
    assert "响应供应商：供应商丙（盖章）" in joined
    assert "电话：【待补充：电话】" in joined
    assert "项目名称：项目乙，采购编号：412623-1，包号：【待补充：包号】，单位：万元人民币" in joined
    # 修订模式 + 批注（改什么都留痕）
    assert _count_el(data, "ins") > 0 and _count_el(data, "del") > 0
    assert "采购编号=「412623-1」" in _comment_texts(data)
    # 原文保留：修订删除线里仍有模板原句（空格位原文）——删除线数量>0 即可（空格删除也留痕）
    assert _count_el(data, "delText") > 0


def test_assembly_primitives_roundtrip(tmp_path):
    """成文工具链机制原语：切片→填空→校验→封存 一轮闭环（旧路径不动）。"""
    from docx import Document
    from docx.oxml.ns import qn

    from app.services.export_service import (
        _FillSession,
        _build_item_document,
        check_doc_fidelity,
        replace_text_tracked,
    )

    src = Document()
    src.add_paragraph("（一）响应函及报价汇总表")
    src.add_paragraph("1. 我方已仔细研究了采购编号：    ，项目名称：   ，包名称：   采购文件。")
    p = tmp_path / "src.docx"
    src.save(str(p))

    src2 = Document(str(p))
    elems = [("p", el) for el in src2.element.body.iterchildren() if el.tag == qn("w:p")]
    doc, located = _build_item_document(str(p), None, elems)
    assert located is True

    # 填空（标准字段 + 显式定向替换）
    sess = _FillSession(doc, "采购人甲", "项目乙", "供应商丙", "412623-1")
    sess.apply_to_doc()
    n = replace_text_tracked(sess.editor, "包名称：【待补充：包名称】", "包名称：包A", "主会话指定应答分包")
    assert n == 1

    # 校验：原文⊂底稿必须通过（删除线保留原文，插入不算改写）
    import zipfile as _z

    from lxml import etree as _e

    with _z.ZipFile(str(p)) as zf:
        src_text = "".join(_e.fromstring(zf.read("word/document.xml")).itertext())
    r = check_doc_fidelity(doc, src_text)
    assert r["ok"] is True, r["issues"]

    data = sess.finish()
    joined = _all_text(data)
    assert "采购编号：412623-1" in joined
    assert "项目名称：项目乙" in joined
    assert "包名称：包A" in joined
    assert _count_el(data, "ins") > 0 and _count_el(data, "del") > 0


def test_draft_label_format():
    from app.services.export_service import _draft_label

    assert _draft_label("采购文件.docx", "商务标") == "商务标(底稿：采购文件.docx)"
    assert _draft_label(None, "商务标") == "商务标"


def test_xlsx_bytes_shape_tolerant():
    """xlsx_bytes 兼容旧 {sheets} 与新方案 agent 落库的 {docModel:{sections}} 形状，
    任何形状下都必须产出至少一个可见工作表（openpyxl 硬性要求）。"""
    from openpyxl import load_workbook

    from app.services.export_service import xlsx_bytes

    def sheets_of(data):
        wb = load_workbook(io.BytesIO(data))
        assert len(wb.worksheets) >= 1, "必须至少有一个可见工作表"
        return wb

    # 旧形状：显式 sheets
    wb = sheets_of(xlsx_bytes({"sheets": [{"name": "报价单", "rows": [["a", "1"]]}]}))
    assert wb.active["A1"].value == "a"

    # 新方案形状：docModel.sections（agent 主会话落库形态）
    wb = sheets_of(
        xlsx_bytes(
            {
                "docModel": {
                    "title": "价格标",
                    "sections": [
                        {"title": "1. 响应函", "content": "我方承诺。/n 第二条。", "source": "block 1~9"},
                    ],
                }
            }
        )
    )
    ws = wb.active
    assert ws["A1"].value == "条目"
    assert "我方承诺。\n 第二条。" in str(ws["B2"].value)

    # 空模型 / 完全无表数据：单可见工作表 + 说明行，不得 500
    wb = sheets_of(xlsx_bytes({}))
    assert wb.active["A1"].value is not None

    # 被 {version_no, model} 包装的形状
    wb = sheets_of(xlsx_bytes({"version_no": 2, "model": {"rows": [["x", "y"]]}}))
    assert wb.active["A1"].value == "x"


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
    assert "底稿：采购文件.docx" in disposition, disposition
