"""切片通道图片节点回归（issue #67：大卷走底稿骨架通道插证据图）。

覆盖：本地 path 来源 / 企业资料库 file_id 来源 / 路径白名单 / 缺来源报错 /
宽度帽 16cm / 题注楷体且不进大纲（issue #66 纪律）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import tempfile
import time
import zipfile

import pytest
from docx import Document
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import assembly_service

TEST_DB = "./.test_bidvolt.db"

# 1×1 PNG（内置 base64，避免测试环境依赖 Pillow）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _setup(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "img@test.com", "password": "Abc12345", "enterprise_name": "图片测试企业"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post(
        "/api/v1/projects", json={"name": "图片测试项目"}, headers=headers
    ).json()["project_id"]
    return headers, pid


def _tmp_png(name: str) -> str:
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "wb") as f:
        f.write(PNG_1PX)
    return path


def _seed_slice(pid: int, text: str) -> str:
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    sid = f"simg{int(time.time() * 1000)}"
    assembly_service._SLICES[sid] = {
        "doc": doc,
        "sess": None,
        "source_text": text,
        "file_id": 1,
        "req_id": 1,
        "title": "（四）补充文件",
        "matched_title": "（四）补充文件",
        "verified": False,
        "task_id": 1,
        "project_id": pid,
        "enterprise_id": 1,
        "created": time.time(),
    }
    return sid


def test_append_image_node_from_path_capped_and_caption_out_of_outline(client):
    """path 来源插图：宽度帽 16cm、题注楷体且不写大纲级别、媒体部件入库。"""
    _h, pid = _setup(client)
    path = _tmp_png("bidvolt_img_a.png")
    sid = _seed_slice(pid, "（四）补充文件\n1.响应保证金")
    try:
        nodes, stats = asyncio.run(
            assembly_service.resolve_image_nodes(
                None,
                1,
                pid,
                [{"type": "image", "path": path, "width_cm": 30, "caption": "图：测试证据-第1页"}],
            )
        )
        assert stats["images_resolved"] == 1
        res = assembly_service.append_slice(sid, 1, nodes, None, None, page_break=False)
        assert res["appended_images"] == 1

        data = assembly_service._SLICES[sid]["sess"].finish()
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        xml = z.read("word/document.xml").decode("utf-8")
        assert any(n.startswith("word/media/") for n in names), names
        assert "<w:drawing" in xml
        assert "图：测试证据-第1页" in xml
        assert "楷体" in xml
        # 题注段落不得写大纲级别（issue #66）；整体噪声体检为 0
        from app.services import docx_normalize

        cap = re.search(
            r"<w:p>(?:(?!</w:p>).)*图：测试证据-第1页(?:(?!</w:p>).)*</w:p>", xml, re.S
        )
        assert cap is not None
        assert "<w:outlineLvl" not in cap.group(0)
        assert docx_normalize.audit_docx(data)["outline_noise"] == 0
        # 请求 30cm → 帽到 16cm = 5760000 EMU
        cx = max(int(v) for v in re.findall(r'<wp:extent cx="(\d+)"', xml))
        assert cx <= 5760000 + 2000, cx
    finally:
        assembly_service._SLICES.pop(sid, None)


def test_resolve_image_node_rejects_path_outside_whitelist():
    with pytest.raises(ValueError, match="仅允许"):
        asyncio.run(
            assembly_service.resolve_image_nodes(
                None, 1, 1, [{"type": "image", "path": "/etc/passwd"}]
            )
        )


def test_resolve_image_node_requires_source():
    with pytest.raises(ValueError, match="缺少来源"):
        asyncio.run(assembly_service.resolve_image_nodes(None, 1, 1, [{"type": "image"}]))


def test_resolve_image_node_from_file_id(client, monkeypatch):
    """企业资料库 file_id 来源：图片直用；越权企业/已删除一律报错。"""
    _h, pid = _setup(client)
    path = _tmp_png("bidvolt_img_b.png")
    engine = create_async_engine("sqlite+aiosqlite:///" + TEST_DB)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed(enterprise_id: int) -> int:
        async with maker() as session:
            from app.models.file import FileObject

            fo = FileObject(
                enterprise_id=enterprise_id,
                project_id=pid,
                owner_type=1,
                bucket="local",
                object_key="abc/original",
                sha256="0" * 64,
                original_name="扫描件.png",
                size_bytes=len(PNG_1PX),
                ext=".png",
                status=1,
                is_deleted=False,
            )
            session.add(fo)
            await session.commit()
            return int(fo.id)

    fid = asyncio.run(_seed(1))
    from app.services.storage import StorageProvider

    monkeypatch.setattr(StorageProvider, "open", lambda self, bucket, key: path)

    async def _resolve():
        async with maker() as session:
            return await assembly_service.resolve_image_nodes(
                session, 1, pid, [{"type": "image", "file_id": fid, "caption": "图：营业执照"}]
            )

    nodes, stats = asyncio.run(_resolve())
    assert stats["images_resolved"] == 1
    assert nodes[0]["_data"] == PNG_1PX
    assert nodes[0]["_ext"] == "png"

    other = asyncio.run(_seed(2))

    async def _resolve_other():
        async with maker() as session:
            return await assembly_service.resolve_image_nodes(
                session, 1, pid, [{"type": "image", "file_id": other}]
            )

    with pytest.raises(ValueError, match="不可用"):
        asyncio.run(_resolve_other())
    asyncio.run(engine.dispose())
