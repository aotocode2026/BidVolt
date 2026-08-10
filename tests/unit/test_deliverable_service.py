from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.deliverable import Deliverable, DeliverableContent
from app.services import deliverable_service
from app.services.deliverable_service import VersionConflict

TEST_DB = "./.test_bidvolt.db"


def test_content_hash_deterministic():
    doc = {"nodes": [{"id": "n1", "text": "内容"}]}
    assert deliverable_service.content_hash(doc) == deliverable_service.content_hash(
        {"nodes": [{"text": "内容", "id": "n1"}]}
    )


def test_compute_diff():
    prev = {"nodes": [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}]}
    curr = {"nodes": [{"id": "n1", "text": "a2"}, {"id": "n3", "text": "c"}]}
    ops = deliverable_service.compute_diff(prev, curr)
    by_op = {o["op"] for o in ops}
    assert by_op == {"replace", "insert", "remove"}


def test_apply_diff_replace_and_missing_node():
    content = {"nodes": [{"id": "n1", "text": "old"}]}
    diff = {"operations": [{"op": "replace", "node_id": "n1", "after": {"id": "n1", "text": "new"}}]}
    result = deliverable_service.apply_diff(content, diff)
    assert result["nodes"][0]["text"] == "new"
    with pytest.raises(ValueError):
        deliverable_service.apply_diff(
            content, {"operations": [{"op": "replace", "node_id": "n9", "after": {}}]}
        )


def test_create_edit_diff_replaces_selection():
    content = {"nodes": [{"id": "n1", "text": "旧"}]}
    diff = deliverable_service.create_edit_diff(content, {"refs": ["n1"]}, "新内容")
    assert diff["operations"][0]["after"]["text"] == "新内容"


def test_save_version_cas_idempotency_and_dedup():
    engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with factory() as session:
            d = Deliverable(enterprise_id=1, project_id=1, deliverable_type=1, title="商务标")
            session.add(d)
            await session.flush()
            v1 = await deliverable_service.save_version(session, d, {"nodes": [{"id": "n1", "text": "x"}]})
            assert v1.version_no == 1
            with pytest.raises(VersionConflict):
                await deliverable_service.save_version(
                    session, d, {"nodes": []}, expected_version_no=2
                )
            v2 = await deliverable_service.save_version(
                session,
                d,
                {"nodes": [{"id": "n1", "text": "x"}]},
                expected_version_no=1,
                idempotency_key="idem-1",
            )
            v2_dup = await deliverable_service.save_version(
                session,
                d,
                {"nodes": [{"id": "n1", "text": "x"}]},
                expected_version_no=2,
                idempotency_key="idem-1",
            )
            assert v2_dup.id == v2.id
            c1 = await session.get(DeliverableContent, v1.content_id)
            c2 = await session.get(DeliverableContent, v2.content_id)
            return v1.version_no, v2.version_no, c1.id, c2.id

    v1no, v2no, c1id, c2id = asyncio.run(scenario())
    engine.sync_engine.dispose()
    assert v1no == 1
    assert v2no == 2
    assert c1id == c2id  # 内容去重
