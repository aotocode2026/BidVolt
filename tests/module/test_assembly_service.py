"""成文工具链信息信号测试：seal 身份/校验信号 + 打包全量核对信号（只提示、不拦截——
合规性由主会话+验收/评审子 agent 保证，服务端不设硬性流程代码）。"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile

import pytest
from docx import Document
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

TEST_DB = "./.test_bidvolt.db"


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "asm@test.com", "password": "Abc12345", "enterprise_name": "成文测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/v1/projects", json={"name": "成文项目"}, headers=headers).json()["project_id"]
    return headers, pid


def _docx_bytes(paragraphs: list[str]) -> bytes:
    """构造合规字体 docx（宋体 eastAsia + Times New Roman——打包字体硬门禁要求）。"""
    from docx.oxml.ns import qn

    doc = Document()
    for p in paragraphs:
        para = doc.add_paragraph()
        run = para.add_run(p)
        run.font.name = "Times New Roman"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _docx_bytes_with_deleted_bare(paragraphs: list[str]) -> bytes:
    """docx 首段注入删除层裸【待补充】（w:delText）——模拟多轮 fill 后旧标记落在删除层。"""
    raw = _docx_bytes(paragraphs)
    zin = zipfile.ZipFile(io.BytesIO(raw))
    xml = zin.read("word/document.xml")
    inj = (
        b'<w:del w:id="99" w:author="t" w:date="2026-01-01T00:00:00Z">'
        b"<w:r><w:delText>\xe3\x80\x90\xe5\xbe\x85\xe8\xa1\xa5\xe5\x85\x85\xe3\x80\x91</w:delText></w:r></w:del>"
    )
    xml = xml.replace(b"<w:p>", b"<w:p>" + inj, 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            zout.writestr(n, xml if n == "word/document.xml" else zin.read(n))
    return buf.getvalue()


def _seed_artifact(maker, pid: int, name: str, content: bytes) -> int:
    """直接落一条 item_docx 产物（无外键约束的 task_id 用 1 即可）。"""
    from app.models.agent import AgentArtifact

    async def _run() -> int:
        async with maker() as session:
            art = AgentArtifact(
                enterprise_id=1,
                project_id=pid,
                task_id=1,
                kind="item_docx",
                name=name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                content=content,
                version_no=1,
            )
            session.add(art)
            await session.commit()
            return int(art.id)

    return asyncio.run(_run())


def test_save_as_new_keeps_lineage_and_old_version(client):
    """另存为新版本：继承逻辑文件身份、逻辑版本递增，旧 artifact 保留可下载（issue #21）。"""
    _h, pid = _setup(client)
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    v1 = _docx_bytes(["第一版内容"])
    v2 = _docx_bytes(["第二版内容"])
    aid = _seed_artifact(maker, pid, "价格文件/（一）响应函及报价汇总表.docx", v1)

    async def _run():
        async with maker() as session:
            res = await assembly_service.save_artifact_file(
                session, 1, pid, None, aid, v2, mode="new"
            )
            return res

    res = asyncio.run(_run())
    assert res["mode"] == "new"
    assert res["logical_file_id"] == aid
    assert res["parent_artifact_id"] == aid
    assert res["logical_version_no"] == 2
    assert res["version_no"] == 1

    async def _list():
        async with maker() as session:
            return await assembly_service.list_artifact_versions(session, 1, pid, aid)

    versions = asyncio.run(_list())
    assert [v["artifact_id"] for v in versions["versions"]] == [aid, res["artifact_id"]]
    assert [v["logical_version_no"] for v in versions["versions"]] == [1, 2]
    asyncio.run(engine.dispose())


def test_overwrite_archives_previous_content(client):
    """覆盖保存：version_no 递增，覆盖前内容可通过历史读取（issue #21）。"""
    _h, pid = _setup(client)
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    v1 = _docx_bytes(["覆盖前内容"])
    v2 = _docx_bytes(["覆盖后内容"])
    aid = _seed_artifact(maker, pid, "价格文件/（一）响应函及报价汇总表.docx", v1)

    async def _run():
        async with maker() as session:
            res = await assembly_service.save_artifact_file(
                session, 1, pid, None, aid, v2, mode="overwrite"
            )
            old, mime, name = await assembly_service.read_artifact_version(
                session, 1, pid, aid, 1
            )
            new, _, _ = await assembly_service.read_artifact_version(
                session, 1, pid, aid, 2
            )
            return res, old, new, mime, name

    res, old, new, mime, name = asyncio.run(_run())
    assert res["version_no"] == 2
    assert res["logical_file_id"] == aid
    assert old == v1
    assert new == v2
    assert name == "（一）响应函及报价汇总表.docx"
    assert mime.startswith("application/vnd.openxmlformats")
    asyncio.run(engine.dispose())


def test_artifact_versions_endpoints(client):
    """版本链列表与历史版本下载接口可被普通 JWT 用户访问（issue #21）。"""
    h, pid = _setup(client)
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    v1 = _docx_bytes(["端点第一版"])
    v2 = _docx_bytes(["端点第二版"])
    aid = _seed_artifact(maker, pid, "价格文件/（一）响应函及报价汇总表.docx", v1)

    from app.services import assembly_service

    async def _overwrite():
        async with maker() as session:
            await assembly_service.save_artifact_file(
                session, 1, pid, None, aid, v2, mode="overwrite"
            )

    asyncio.run(_overwrite())

    r = client.get(f"/api/v1/projects/{pid}/assembly/artifacts/{aid}/versions", headers=h)
    assert r.status_code == 200
    payload = r.json()
    assert payload["logical_file_id"] == aid
    assert [v["version_no"] for v in payload["versions"]] == [2]

    dl = client.get(
        f"/api/v1/projects/{pid}/agent-artifact/{aid}/versions/1/download", headers=h
    )
    assert dl.status_code == 200
    assert dl.content == v1
    asyncio.run(engine.dispose())


def test_inspect_artifact_reports_file_health(client):
    """产物详情带文件健康信号：正常 docx 标记可读（issue #24）。"""
    _h, pid = _setup(client)
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    aid = _seed_artifact(
        maker, pid, "内部管理文件/编制逻辑与评分响应记录.docx", _docx_bytes(["编制逻辑记录"])
    )

    async def _run():
        async with maker() as session:
            return await assembly_service.inspect_artifact(session, 1, pid, None, aid)

    detail = asyncio.run(_run())
    assert detail["file_health"]["readable"] is True
    assert detail["file_health"]["zip_ok"] is True
    assert detail["file_health"]["document_xml_ok"] is True
    assert detail["file_health"]["text_chars"] > 0
    asyncio.run(engine.dispose())


def test_inspect_artifact_graceful_on_broken_docx(client):
    """损坏 docx：详情返回不可读信号与原因，而不是抛 500（issue #24）。"""
    _h, pid = _setup(client)
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    aid = _seed_artifact(
        maker, pid, "内部管理文件/编制逻辑与评分响应记录.docx", b"not-a-docx"
    )

    async def _run():
        async with maker() as session:
            return await assembly_service.inspect_artifact(session, 1, pid, None, aid)

    detail = asyncio.run(_run())
    assert detail["file_health"]["readable"] is False
    assert detail["file_health"]["error"]
    asyncio.run(engine.dispose())


def test_seal_returns_identity_signals(client, monkeypatch):
    """seal 只给信息信号、不拦截：回执带 req_title/matched_title/was_verified，供 agent 自查。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)

    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    src_text = "（一）响应函及报价汇总表（在投标工具在线填写）\n我方承诺……"
    doc = Document()
    for line in src_text.split("\n"):
        doc.add_paragraph(line)
    base = {
        "doc": doc,
        "sess": None,
        "source_text": src_text,
        "file_id": 1,
        "req_id": 1,
        "title": "（一）响应函及报价汇总表",
        "matched_title": "（一）响应函及报价汇总表（在投标工具在线填写）",
        "verified": False,
        "task_id": 1,
        "project_id": pid,
        "enterprise_id": 1,
        "created": time.time(),
    }
    assembly_service._SLICES["stest1"] = dict(base)

    async def _seal(fname):
        async with maker() as session:
            return await assembly_service.seal_slice(session, "stest1", 1, "价格文件", fname)

    # 未 verify 也允许封存（不拦截），但 was_verified 信号如实=false
    res = asyncio.run(_seal("（二）报价明细表.docx"))
    assert res["artifact_id"] > 0
    assert res["was_verified"] is False
    assert res["req_title"] == "（一）响应函及报价汇总表"
    assert res["matched_title"].startswith("（一）响应函及报价汇总表")

    # verify 后封存：was_verified=true
    assembly_service._SLICES["stest2"] = {
        **base,
        "doc": Document(io.BytesIO(_docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"]))),
        "source_text": "（一）响应函及报价汇总表\n我方承诺……",
        "verified": False,
        "created": time.time(),
    }
    r = assembly_service.verify_slice("stest2", 1)
    assert r["passed"] is True
    async def _seal2():
        async with maker() as session:
            return await assembly_service.seal_slice(session, "stest2", 1, "价格文件", "（一）响应函及报价汇总表.docx")

    res2 = asyncio.run(_seal2())
    assert res2["was_verified"] is True
    assert res2["matched_title"].startswith("（一）响应函及报价汇总表")
    assembly_service._SLICES.pop("stest1", None)
    asyncio.run(engine.dispose())


def _seed_pkg(maker, pid, artifacts):
    from app.models.agent import AgentArtifact
    from app.models.requirement import Requirement
    from app.models.task import Task

    async def _run():
        async with maker() as session:
            task = Task(
                enterprise_id=1,
                project_id=pid,
                task_type="agent_pipeline",
                idempotency_key=f"asm-pkg-{len(artifacts)}-{time.time_ns()}",
                status=2,
                payload={},
            )
            session.add(task)
            await session.flush()
            for i, (title, role, order) in enumerate(
                [
                    ("（一）响应函及报价汇总表", "price", 1),
                    ("（二）报价明细表", "price", 2),
                    ("一、价格文件", "price", 0),
                ]
            ):
                session.add(
                    Requirement(
                        enterprise_id=1,
                        project_id=pid,
                        req_type="doc_template",
                        content=title,
                        current=True,
                        structured={"role": role, "order": order},
                    )
                )
            for name, content in artifacts:
                session.add(
                    AgentArtifact(
                        enterprise_id=1,
                        project_id=pid,
                        task_id=task.id,
                        kind="item_docx",
                        name=name,
                        mime="application/octet-stream",
                        content=content,
                    )
                )
            await session.commit()
            return task.id

    return asyncio.run(_run())


def _pack(maker, pid, task_id):
    from app.services import assembly_service

    async def _run():
        async with maker() as session:
            from sqlalchemy import text

            ids = [
                r[0]
                for r in (
                    await session.execute(
                        text("select id from agent_artifact where task_id=:t and kind='item_docx'"),
                        {"t": task_id},
                    )
                ).fetchall()
            ]
            return await assembly_service.package_zip(session, 1, pid, task_id, ids, None)

    return asyncio.run(_run())


def test_package_zip_rejects_missing_item(client, monkeypatch):
    """打包硬门禁（R8 教训）：is_file_item 条目未 seal 进包 → 直接拒绝打包并列出缺失条目。
    旧行为是「只给信号、照常出包」，已废弃——服务端保证结构完整，不依赖主会话自觉。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tid = _seed_pkg(
        maker, pid,
        [("价格文件/（一）响应函及报价汇总表.docx",
          _docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"]))],
    )
    with pytest.raises(ValueError) as exc:
        _pack(maker, pid, tid)
    assert "（二）报价明细表" in str(exc.value)
    asyncio.run(engine.dispose())


def test_audit_bare_pending_counts_final_text_only(client, monkeypatch):
    """回归：audit.bare_pending 只算最终文本（w:t），删除层（w:delText）旧标记不算——
    曾用 itertext 把删除层也算进来，与 verify 口径打架，主会话被迫解包核对（任务 380 教训）。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tid = _seed_pkg(
        maker, pid,
        [("价格文件/（一）响应函及报价汇总表.docx",
          _docx_bytes_with_deleted_bare(["（一）响应函及报价汇总表", "我方承诺【待补充】其余【待补充：被授权人姓名】"])),
         ("价格文件/（二）报价明细表.docx",
          _docx_bytes(["（二）报价明细表", "明细报价如下……"]))],
    )
    result = _pack(maker, pid, tid)
    audit = result["audit"]
    # 最终文本 1 处裸（"我方承诺【待补充】"）；删除层 1 处不算；带标签的不算裸
    assert audit["bare_pending_count"] == 1, audit
    items = audit["bare_pending"].get("价格文件/（一）响应函及报价汇总表.docx", [])
    assert len(items) == 1 and items[0]["label"] == "", audit["bare_pending"]
    asyncio.run(engine.dispose())


def test_inspect_artifact_excludes_deleted_layer(client, monkeypatch):
    """回归：inspect_agent_artifact 的 pending_items/bare_pending_count 只算最终文本。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)

    from app.models.agent import AgentArtifact
    from app.models.task import Task
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with maker() as session:
            task = Task(
                enterprise_id=1,
                project_id=pid,
                task_type="agent_pipeline",
                idempotency_key="asm-del-" + str(time.time_ns()),
                status=2,
                payload={},
            )
            session.add(task)
            await session.flush()
            session.add(
                AgentArtifact(
                    enterprise_id=1,
                    project_id=pid,
                    task_id=task.id,
                    kind="item_docx",
                    name="商务文件/（四）补充文件.docx",
                    mime="application/octet-stream",
                    content=_docx_bytes_with_deleted_bare(["（四）补充文件", "承诺【待补充】其余【待补充：被授权人姓名】"]),
                )
            )
            await session.commit()
            return task.id

    task_id = asyncio.run(_seed())

    async def _inspect():
        async with maker() as session:
            from sqlalchemy import text

            aid = (
                await session.execute(
                    text("select id from agent_artifact where task_id=:t"), {"t": task_id}
                )
            ).fetchone()[0]
            return await assembly_service.inspect_artifact(session, 1, pid, task_id, aid)

    info = asyncio.run(_inspect())
    assert info["bare_pending_count"] == 1, info
    assert info["pending_count"] == 2, info  # 最终文本 2 处【待补充】（1 裸 + 1 带标签）；删除层 1 处不计
    asyncio.run(engine.dispose())


def test_package_zip_reports_duplicate_and_identity(client, monkeypatch):
    """打包只给信号、不拦截：同部分雷同与身份不符如实进入 audit 信号。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    # 雷同：两份不同文件名、同一内容
    tid = _seed_pkg(
        maker, pid,
        [
            ("价格文件/（一）响应函及报价汇总表.docx", _docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"])),
            ("价格文件/（二）报价明细表.docx", _docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"])),
        ],
    )
    result = _pack(maker, pid, tid)
    assert result["audit"]["unique_ok"] is False
    assert result["audit"]["duplicate_pairs"], result["audit"]

    # 身份不符：文件名是报价明细表，内容却是响应函
    tid2 = _seed_pkg(
        maker, pid,
        [
            ("价格文件/（一）响应函及报价汇总表.docx", _docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"])),
            ("价格文件/（二）报价明细表.docx", _docx_bytes(["响应函价格表", "合计报价……"])),
        ],
    )
    result2 = _pack(maker, pid, tid2)
    assert result2["audit"]["identity_ok"] is False
    assert result2["audit"]["identity_issues"], result2["audit"]
    asyncio.run(engine.dispose())


def test_package_zip_passes_full_audit_and_dedupes(client, monkeypatch):
    """打包核对信号全绿：产出 zip，同名产物改名不覆盖，manifest 带 audit 结论。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    _h, pid = _setup(client)
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # 两份正确内容 + 一份与首份同名但正文不同的多余产物（测改名去重：审计通过后同名改名）
    tid = _seed_pkg(
        maker, pid,
        [
            ("价格文件/（一）响应函及报价汇总表.docx", _docx_bytes(["（一）响应函及报价汇总表", "我方承诺……"])),
            ("价格文件/（二）报价明细表.docx", _docx_bytes(["（二）报价明细表", "明细报价如下……"])),
            ("价格文件/（一）响应函及报价汇总表.docx", _docx_bytes(["（一）响应函及报价汇总表", "我方承诺（补充）……"])),
        ],
    )
    result = _pack(maker, pid, tid)
    assert result.get("missing_file_items") == []

    from sqlalchemy import text

    async def _load():
        async with maker() as session:
            row = (
                await session.execute(
                    text("select content from agent_artifact where kind='zip' and task_id=:t"),
                    {"t": tid},
                )
            ).fetchone()
            return row[0]

    zdata = asyncio.run(_load())
    with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
        names = zf.namelist()
        assert names.count("价格文件/（一）响应函及报价汇总表.docx") == 1
        assert any("(2)" in n for n in names), names
        assert "价格文件/（二）报价明细表.docx" in names
        assert "会话记录/主会话记录.md" in names
        assert "会话记录/主会话记录-精简版.md" in names  # 纯代码生成：每个包都带完整版+精简版
        import json

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["audit"]["coverage_ok"] is True
        assert manifest["audit"]["unique_ok"] is True
        assert manifest["audit"]["identity_ok"] is True
    asyncio.run(engine.dispose())


def test_inspect_artifact_previews(client, monkeypatch):
    """产物自检：xlsx 预览行列与行内容；zip 预览文件清单。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    h, pid = _setup(client)

    from app.models.agent import AgentArtifact
    from app.models.task import Task
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with maker() as session:
            task = Task(
                enterprise_id=1,
                project_id=pid,
                task_type="agent_pipeline",
                idempotency_key="asm-task-2",
                status=2,
                payload={},
            )
            session.add(task)
            await session.flush()
            # 一个真实 xlsx 产物（多行多列）
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "报价单"
            ws.append(["序号", "名称"])
            ws.append([1, "虚拟电厂平台"])
            buf = io.BytesIO()
            wb.save(buf)
            session.add(
                AgentArtifact(
                    enterprise_id=1,
                    project_id=pid,
                    task_id=task.id,
                    kind="xlsx",
                    name="价格文件/报价单.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content=buf.getvalue(),
                )
            )
            await session.commit()
            return task.id

    task_id = asyncio.run(_seed())

    async def _inspect():
        async with maker() as session:
            from sqlalchemy import text

            aid = (
                await session.execute(
                    text("select id from agent_artifact where task_id=:t and kind='xlsx'"),
                    {"t": task_id},
                )
            ).fetchone()[0]
            return await assembly_service.inspect_artifact(session, 1, pid, task_id, aid)

    info = asyncio.run(_inspect())
    print("inspect:", info)
    assert info["kind"] == "xlsx"
    assert info["sheets"][0]["rows"] == 2
    assert info["sheets"][0]["cols"] == 2
    assert "虚拟电厂平台" in str(info["sheets"][0]["preview"])
    asyncio.run(engine.dispose())


def test_fill_slice_table_fills_per_item_feedback():
    """table_fills 逐项回执：成功/失败各自说明（坐标越界给表数提示）——回归：
    agent 曾把越界试出的 done=0 误判为'机制未生效'而放弃填原表。"""
    import time

    from docx import Document

    from app.services import assembly_service

    src = Document()
    tb = src.add_table(rows=3, cols=3)
    tb.rows[0].cells[0].text = "表头"
    assembly_service._SLICES["stf1"] = {
        "doc": src,
        "sess": None,
        "source_text": "x",
        "file_id": 1,
        "req_id": 1,
        "title": "t",
        "matched_title": "t",
        "verified": False,
        "task_id": 1,
        "project_id": 1,
        "enterprise_id": 1,
        "created": time.time(),
    }
    r = assembly_service.fill_slice(
        "stf1", 1, {}, [{"find": "不存在的原文", "value": "x"}, {"find": "表头", "value": "改表头"}],
        [
            {"table": 0, "row": 1, "col": 0, "value": "有效填值"},
            {"table": 3, "row": 0, "col": 0, "value": "越界"},
        ],
    )
    assert r["table_fills_done"] == 1
    # 逐项 fills 回执：找不到的原文如实报 found=False（不再静默 0）
    found_flags = {x["find"]: x["found"] for x in r["fills_results"]}
    assert found_flags["不存在的原文"] is False
    assert found_flags["表头"] is True
    ok = [x for x in r["table_fills_results"] if x["ok"]]
    bad = [x for x in r["table_fills_results"] if not x["ok"]]
    assert len(ok) == 1 and len(bad) == 1
    assert "1 张表" in bad[0]["error"], bad[0]
    assembly_service._SLICES.pop("stf1", None)


def test_inspect_artifact_pending_items_signal():
    """产物自检：docx 返回 pending_items 逐项清单（标签+上下文），不再只有计数——
    回归：计数会掩盖'本可填实却空着/标签含混'，检查者靠清单逐项核对。"""
    from app.models.agent import AgentArtifact
    from app.models.task import Task
    from app.services import assembly_service

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with maker() as session:
            task = Task(
                enterprise_id=1, project_id=1, task_type="agent_pipeline",
                idempotency_key="asm-task-pending", status=2, payload={},
            )
            session.add(task)
            await session.flush()
            content = _docx_bytes(["电话：【待补充：电话】", "特授权【待补充】代表我方全权办理。"])
            session.add(
                AgentArtifact(
                    enterprise_id=1, project_id=1, task_id=task.id,
                    kind="item_docx", name="价格文件/响应函.docx",
                    mime="application/octet-stream", content=content,
                )
            )
            await session.commit()
            return task.id

    task_id = asyncio.run(_seed())

    async def _inspect():
        async with maker() as session:
            from sqlalchemy import text

            aid = (
                await session.execute(
                    text("select id from agent_artifact where task_id=:t and kind='item_docx'"),
                    {"t": task_id},
                )
            ).fetchone()[0]
            return await assembly_service.inspect_artifact(session, 1, 1, task_id, aid)

    info = asyncio.run(_inspect())
    labels = [i["label"] for i in info["pending_items"]]
    assert "电话" in labels
    assert "" in labels  # 裸【待补充】如实暴露（label 为空）
    assert info["pending_count"] == len(info["pending_items"])
    # 裸待补充排最前 + kind=bare
    kinds = [i["kind"] for i in info["pending_items"]]
    assert "bare" in kinds and kinds.index("bare") == 0
    assert info["bare_pending_count"] >= 1
    asyncio.run(engine.dispose())
