"""成文工具链产物自检测试：打包去重/完整性信号/产物预览。"""

from __future__ import annotations

import asyncio
import io
import zipfile

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


def test_package_zip_dedupes_and_reports_missing(client, monkeypatch):
    """打包：同名产物自动改名不覆盖；模板清单未封存条目如实返回 missing_file_items。"""
    monkeypatch.setattr(settings, "agent_pipeline_enabled", 1)
    h, pid = _setup(client)

    from app.models.agent import AgentArtifact
    from app.models.requirement import Requirement
    from app.models.task import Task

    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with maker() as session:
            task = Task(
                enterprise_id=1,
                project_id=pid,
                task_type="agent_pipeline",
                idempotency_key="asm-task-1",
                status=2,
                payload={},
            )
            session.add(task)
            await session.flush()
            # 两个文件条目 + 一个结构行（is_file_item=false）
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
            # 两个同名 docx 产物 + 一个 xlsx
            for name in ("价格文件/（一）响应函及报价汇总表.docx",) * 2:
                session.add(
                    AgentArtifact(
                        enterprise_id=1,
                        project_id=pid,
                        task_id=task.id,
                        kind="item_docx",
                        name=name,
                        mime="application/octet-stream",
                        content=b"fake-docx",
                    )
                )
            session.add(
                AgentArtifact(
                    enterprise_id=1,
                    project_id=pid,
                    task_id=task.id,
                    kind="xlsx",
                    name="价格文件/报价单.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    content=b"fake-xlsx",
                )
            )
            await session.commit()
            return task.id

    task_id = asyncio.run(_seed())
    from app.services import assembly_service

    async def _pack():
        async with maker() as session:
            ids = []
            async with maker() as s2:
                rows = (
                    await s2.execute(
                        __import__("sqlalchemy").text("select id from agent_artifact where task_id=:t"),
                        {"t": task_id},
                    )
                ).fetchall()
                ids = [r[0] for r in rows]
            return await assembly_service.package_zip(session, 1, pid, task_id, ids, None)

    result = asyncio.run(_pack())
    print("package result:", {k: result.get(k) for k in ("file_count", "missing_file_items")})
    assert result["missing_file_items"] == ["（二）报价明细表"], result["missing_file_items"]

    async def _load():
        async with maker() as session:
            from sqlalchemy import text

            row = (
                await session.execute(
                    text("select content from agent_artifact where kind='zip' and task_id=:t"),
                    {"t": task_id},
                )
            ).fetchone()
            return row[0]

    zdata = asyncio.run(_load())
    names = zipfile.ZipFile(io.BytesIO(zdata)).namelist()
    print("zip entries:", names)
    # 同名 docx 去重后不重复
    assert names.count("价格文件/（一）响应函及报价汇总表.docx") == 1
    assert any("(2)" in n for n in names), names
    # 会话记录 + manifest 自动附带
    assert "会话记录/主会话记录.md" in names
    assert "manifest.json" in names
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
