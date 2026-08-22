"""成文工具链服务（新方案）：主会话自主成文的机制工具。

主会话经 MCP 调用本模块的机制原语，自主完成
"选底稿 → 列清单 → 切片 → 填空 → 追加 → 校验 → 封存 → 打包"：
- 机制保真：切片=底稿条目区间字节级复制；填空/追加=修订模式+批注；校验=原文逐字⊂底稿；
- 决策在主会话：填什么值、加什么内容、按什么顺序、何时封存打包，全部由主会话决定。

旧方案路径（build_response_package 整段服务端成文）不受影响。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 内存切片仓：一个切片持有 (Document + 持久 _FillSession/editor)，保证批注 id 全局连续。
# 容量上限 + TTL 防止无界增长（app 进程单 worker；MCP 会话串行调用）。
_SLICES: dict[str, dict[str, Any]] = {}
_SLICE_CAP = 40
_SLICE_TTL = 3600.0


def _prune_slices() -> None:
    now = time.time()
    for sid in [s for s, v in _SLICES.items() if now - v["created"] > _SLICE_TTL]:
        _SLICES.pop(sid, None)
    while len(_SLICES) > _SLICE_CAP:
        oldest = min(_SLICES, key=lambda s: _SLICES[s]["created"])
        _SLICES.pop(oldest, None)


def _slice(slice_id: str, task_id: int) -> dict[str, Any]:
    s = _SLICES.get(slice_id)
    if s is None:
        raise ValueError(f"切片不存在或已过期：{slice_id}（请重新 slice_template_item）")
    if int(s["task_id"]) != int(task_id):
        raise ValueError("切片不属于当前任务")
    return s


async def list_draft_candidates(session: AsyncSession, enterprise_id: int, project_id: int) -> dict:
    """候选底稿：按内容分级排序（含"响应文件格式"章优先），返回列表+推荐 file_id。
    主会话可据此选择底稿（也允许指定其他 docx）。"""
    from sqlalchemy import select as sa_select

    from app.models.doc import DocBlock
    from app.models.file import FileObject

    rows = (
        await session.scalars(
            sa_select(FileObject).where(
                FileObject.project_id == int(project_id),
                FileObject.enterprise_id == enterprise_id,
                FileObject.is_deleted.is_(False),
                FileObject.owner_type == 2,
            )
        )
    ).all()
    docx_rows = [f for f in rows if (f.ext or "").strip(".").lower() == "docx"]
    texts: dict[int, str] = {}
    for f in docx_rows:
        bs = (
            await session.scalars(
                sa_select(DocBlock.text_content).where(DocBlock.file_id == f.id)
            )
        ).all()
        texts[f.id] = "\n".join(b or "" for b in bs)[:200000]

    def _rank(f) -> tuple[int, str]:
        t = texts.get(f.id) or ""
        if "响应文件格式" in t:
            return (3, "含《响应文件格式》章（完整采购文件）")
        if "商务文件" in t and "技术文件" in t:
            return (2, "含商务/技术文件章节")
        if "商务文件" in t or "技术文件" in t:
            return (1, "含商务或技术文件章节")
        return (0, "其他 docx")

    candidates = []
    for f in docx_rows:
        rank, reason = _rank(f)
        candidates.append(
            {
                "file_id": f.id,
                "name": f.original_name or f.storage_name or "",
                "size_bytes": f.size_bytes or 0,
                "rank": rank,
                "reason": reason,
            }
        )
    candidates.sort(key=lambda c: (c["rank"], c["size_bytes"]), reverse=True)
    return {"candidates": candidates, "recommended_file_id": candidates[0]["file_id"] if candidates else None}


async def get_template_outline(session: AsyncSession, enterprise_id: int, project_id: int) -> dict:
    """模板清单：《响应文件格式》条目（doc_template），按 价格/商务/技术 分组排序。
    每项带 req_id（后续 slice_template_item 的 item_ref）。"""
    from sqlalchemy import select as sa_select

    from app.models.requirement import Requirement

    rows = (
        await session.scalars(
            sa_select(Requirement).where(
                Requirement.enterprise_id == enterprise_id,
                Requirement.project_id == project_id,
                Requirement.current.is_(True),
                Requirement.req_type == "doc_template",
            )
        )
    ).all()
    items_by_role: dict[str, list[dict]] = {"price": [], "business": [], "technical": []}
    for r in rows:
        role = (r.structured or {}).get("role")
        if role in items_by_role:
            order = (r.structured or {}).get("order", 0)
            try:
                order = int(order)
            except (TypeError, ValueError):
                order = 99999
            items_by_role[role].append(
                {
                    "req_id": r.id,
                    "title": (r.content or "").split("\n")[0][:80],
                    "order": order,
                }
            )
    for role in items_by_role:
        items_by_role[role].sort(key=lambda x: (isinstance(x["order"], int), x["order"]))
    return {"items_by_role": items_by_role, "counts": {k: len(v) for k, v in items_by_role.items()}}


async def create_slice(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
    task_id: int,
    file_id: int,
    req_id: int,
) -> dict:
    """切片：把底稿中该条目区间原文整段复制为独立文档骨架，存入内存切片仓。
    返回 slice_id 与定位状态（未定位时按解析清单内容重建并如实标注）。"""
    import secrets

    from sqlalchemy import select as sa_select

    from app.models.file import FileObject
    from app.models.requirement import Requirement
    from app.services import export_service
    from app.services.storage import StorageProvider

    row = await session.scalar(
        sa_select(Requirement).where(
            Requirement.id == int(req_id),
            Requirement.enterprise_id == enterprise_id,
            Requirement.project_id == int(project_id),
            Requirement.current.is_(True),
            Requirement.req_type == "doc_template",
        )
    )
    if row is None:
        raise ValueError(f"模板条目不存在：req_id={req_id}")
    fobj = await session.get(FileObject, int(file_id))
    if fobj is None or fobj.enterprise_id != enterprise_id or fobj.is_deleted or (fobj.ext or "").strip(".").lower() != "docx":
        raise ValueError(f"底稿文件不可用：file_id={file_id}")
    source_path = StorageProvider().open(fobj.bucket, fobj.object_key)

    elements = export_service.locate_item_elements(source_path, row)
    doc, located = export_service._build_item_document(source_path, row, elements)

    # 底稿原文（校验基准）
    import zipfile as _zip

    from lxml import etree as _etree

    with _zip.ZipFile(source_path) as zf:
        src_xml = _etree.fromstring(zf.read("word/document.xml"))
    source_text = "".join(src_xml.itertext())

    _prune_slices()
    slice_id = "s" + secrets.token_hex(6)
    _SLICES[slice_id] = {
        "doc": doc,
        "sess": None,
        "source_text": source_text,
        "file_id": int(file_id),
        "req_id": int(req_id),
        "task_id": int(task_id),
        "project_id": int(project_id),
        "enterprise_id": int(enterprise_id),
        "created": time.time(),
    }
    return {
        "slice_id": slice_id,
        "located": located,
        "file_id": int(file_id),
        "req_id": int(req_id),
        "warn": None if located else "该条目在底稿中未定位到原文区间，按解析清单内容重建（可能不完整），请对照采购文件原件核对。",
    }


def _ensure_sess(s, fields: dict) -> Any:
    from app.services import export_service

    if s["sess"] is None:
        s["sess"] = export_service._FillSession(
            s["doc"],
            str(fields.get("buyer") or "").strip(),
            str(fields.get("project_name") or "").strip(),
            str(fields.get("supplier") or "").strip(),
            str(fields.get("tender_no") or "").strip(),
        )
    else:
        sess = s["sess"]
        if fields.get("buyer") is not None:
            sess.buyer = str(fields["buyer"]).strip()
        if fields.get("project_name") is not None:
            sess.project_name = str(fields["project_name"]).strip()
        if fields.get("supplier") is not None:
            sess.supplier = str(fields["supplier"]).strip()
        if fields.get("tender_no") is not None:
            sess.tender_no = str(fields["tender_no"]).strip()
    return s["sess"]


def fill_slice(slice_id: str, task_id: int, fields: dict | None, fills: list[dict] | None) -> dict:
    """填空：标准字段（buyer/project_name/supplier/tender_no）→ 带标签空位规则（无资料原位【待补充】），
    再按主会话给出的显式 fills=[{find,value,comment}] 定向替换。全部修订模式+批注。"""
    s = _slice(slice_id, task_id)
    sess = _ensure_sess(s, fields or {})
    sess.apply_to_doc()
    n_fills = 0
    for f in fills or []:
        find = str(f.get("find") or "")
        if not find:
            continue
        value = str(f.get("value") or "")
        comment = str(f.get("comment") or "") or None
        from app.services import export_service

        n_fills += export_service.replace_text_tracked(sess.editor, find, value, comment)
    return {"slice_id": slice_id, "explicit_fills": n_fills}


def append_slice(slice_id: str, task_id: int, nodes: list[dict] | None, comment: str | None) -> dict:
    """追加撰写内容：修订插入 + 批注（节点形状兼容段落/标题/表格/裸字符串）。"""
    s = _slice(slice_id, task_id)
    sess = _ensure_sess(s, {})
    sess.append_supplement(
        nodes or [],
        heading_text="响应内容（主会话撰写，修订插入，请复核）",
        comment=comment or "本节为主会话针对本条目撰写的响应内容（修订插入），请人工复核。",
    )
    return {"slice_id": slice_id, "appended_nodes": len(nodes or [])}


def verify_slice(slice_id: str, task_id: int) -> dict:
    """忠实性校验：条目文件原文（含删除线、剔除插入）逐字⊂底稿。不过时返回差异片段。"""
    s = _slice(slice_id, task_id)
    from app.services import export_service

    r = export_service.check_doc_fidelity(s["doc"], s["source_text"])
    r["slice_id"] = slice_id
    return r


async def seal_slice(
    session: AsyncSession,
    slice_id: str,
    task_id: int,
    dir_name: str,
    filename: str,
) -> dict:
    """封存：生成条目 docx 并落产物表（先 verify 通过再 seal 是 skill 约定，服务端不强制）。"""
    s = _slice(slice_id, task_id)
    sess = _ensure_sess(s, {})
    # 未显式填空也走一遍规则：带标签空位无资料原位【待补充】（诚实标注）
    sess.apply_to_doc()
    data = sess.finish()

    from sqlalchemy import select as sa_select

    from app.models.agent import AgentArtifact
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, s["enterprise_id"])
    art = AgentArtifact(
        enterprise_id=s["enterprise_id"],
        project_id=s["project_id"],
        task_id=int(task_id),
        kind="item_docx",
        name=f"{dir_name}/{filename}",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=data,
    )
    session.add(art)
    await session.commit()
    await _set_rls_context(session, s["enterprise_id"])
    _SLICES.pop(slice_id, None)  # 封存后切片即失效（不可再改）
    return {"artifact_id": art.id, "name": art.name, "bytes": len(data)}


async def quote_xlsx(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
    task_id: int,
    sheets: list[dict] | None,
) -> dict:
    """报价单 xlsx：主会话给出 sheets=[{name,rows}]，服务端确定性生成。"""
    from app.models.agent import AgentArtifact
    from app.services import export_service
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    data = export_service.xlsx_bytes({"sheets": sheets or []})
    await _set_rls_context(session, enterprise_id)
    art = AgentArtifact(
        enterprise_id=enterprise_id,
        project_id=int(project_id),
        task_id=int(task_id),
        kind="xlsx",
        name="价格文件/报价单.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=data,
    )
    session.add(art)
    await session.commit()
    await _set_rls_context(session, enterprise_id)
    return {"artifact_id": art.id, "name": art.name, "bytes": len(data)}


async def package_zip(
    session: AsyncSession,
    enterprise_id: int,
    project_id: int,
    task_id: int,
    artifact_ids: list[int] | None,
    draft_file_id: int | None,
) -> dict:
    """打包：把主会话已封存的条目文件/报价单 zip 成响应文件包，
    自动附 会话记录/主会话记录.md 与 manifest.json。返回 zip 产物 id。"""
    import io as _io
    import json as _json
    import zipfile as _zip

    from sqlalchemy import select as sa_select

    from app.models.agent import AgentArtifact
    from app.models.file import FileObject
    from app.models.task import Task
    from app.services import agent_pipeline
    from app.services.task_service import _set_rls_context  # noqa: PLC0415

    await _set_rls_context(session, enterprise_id)
    arts = (
        await session.scalars(
            sa_select(AgentArtifact).where(
                AgentArtifact.id.in_([int(i) for i in (artifact_ids or [])]),
                AgentArtifact.enterprise_id == enterprise_id,
                AgentArtifact.project_id == int(project_id),
                AgentArtifact.task_id == int(task_id),
                AgentArtifact.kind.in_(["item_docx", "xlsx"]),
            )
        )
    ).all()
    if not arts:
        raise ValueError("没有可打包的封存文件（请先 seal_template_item / build_quote_xlsx）")

    draft_name = "采购文件"
    if draft_file_id:
        fobj = await session.get(FileObject, int(draft_file_id))
        if fobj is not None and fobj.enterprise_id == enterprise_id:
            draft_name = fobj.original_name or "采购文件"

    buf = _io.BytesIO()
    files_manifest = []
    seen: set[str] = set()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zf:
        for a in arts:
            name = a.name if a.name in seen else a.name
            seen.add(name)
            zf.writestr(name, a.content)
            files_manifest.append({"name": name, "bytes": len(a.content)})
        # 主会话全程记录
        task = await session.scalar(sa_select(Task).where(Task.id == int(task_id)))
        if task is not None:
            try:
                record = await agent_pipeline.session_record_markdown(session, task)
                zf.writestr("会话记录/主会话记录.md", record.encode("utf-8"))
                files_manifest.append({"name": "会话记录/主会话记录.md", "bytes": len(record.encode("utf-8"))})
            except Exception:  # noqa: BLE001 会话记录缺失不影响打包主体
                logger.warning("打包附会话记录失败", exc_info=True)
        manifest = {
            "project_id": int(project_id),
            "task_id": int(task_id),
            "draft": draft_name,
            "note": "主会话经成文工具链（切片→填空→追加→校验→封存→打包）自主成文；"
                    "全部改动可在 Word【审阅→所有标记】中逐处查看；附主会话全程记录。",
            "files": files_manifest,
        }
        zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2))
    data = buf.getvalue()

    art = AgentArtifact(
        enterprise_id=enterprise_id,
        project_id=int(project_id),
        task_id=int(task_id),
        kind="zip",
        name=f"响应文件包(底稿：{draft_name}).zip",
        mime="application/zip",
        content=data,
    )
    session.add(art)
    await session.commit()
    await _set_rls_context(session, enterprise_id)
    return {"artifact_id": art.id, "name": art.name, "bytes": len(data), "file_count": len(files_manifest)}
