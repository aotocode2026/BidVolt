"""企业资料库接口（4.2.7）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission, UserContext
from app.constants import Permission, TaskStatus, TaskType
from app.db import get_session
from app.models.enterprise_domain import (
    EnterpriseAsset,
    EnterpriseAssetCategory,
    EnterpriseFact,
    EnterpriseIngestionTask,
)
from app.models.task import Task
from app.services.audit import write_audit

router = APIRouter(prefix="/enterprise", tags=["enterprise"])


def _classify(name: str) -> tuple[str, list[tuple[str, str, float]]]:
    lower = name.lower()
    rules = (
        (("营业执照", "执照"), "证照", [("credit_code", "营业执照", 0.9)]),
        (("资质", "许可证"), "资质", [("qualification", name, 0.6)]),
        (("业绩", "合同", "中标"), "业绩", [("performance", name, 0.6)]),
        (("身份证", "证书"), "人员", [("personnel", name, 0.6)]),
        (("检测", "报告"), "检测报告", [("test_report", name, 0.6)]),
        (("参数", "产品"), "产品参数", [("product_param", name, 0.6)]),
    )
    for keywords, category, facts in rules:
        if any(k in lower for k in keywords):
            return category, facts
    return "其他", []


async def _ensure_categories(session: AsyncSession, enterprise_id: int) -> dict[str, int]:
    existing = await session.scalars(
        select(EnterpriseAssetCategory).where(EnterpriseAssetCategory.enterprise_id == enterprise_id)
    )
    mapping = {c.name: c.id for c in existing}
    for name in ("证照", "资质", "业绩", "人员", "产品参数", "检测报告", "其他"):
        if name not in mapping:
            cat = EnterpriseAssetCategory(enterprise_id=enterprise_id, name=name)
            session.add(cat)
            await session.flush()
            mapping[name] = cat.id
    return mapping


@router.get("/categories")
async def list_categories(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> list[dict]:
    rows = await session.scalars(
        select(EnterpriseAssetCategory).where(EnterpriseAssetCategory.enterprise_id == user.enterprise_id)
    )
    return [{"category_id": c.id, "name": c.name, "parent_id": c.parent_id} for c in rows]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    cat = EnterpriseAssetCategory(
        enterprise_id=user.enterprise_id, name=body["name"], parent_id=body.get("parent_id")
    )
    session.add(cat)
    await session.commit()
    return {"category_id": cat.id, "name": cat.name}


@router.get("/assets")
async def list_assets(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> list[dict]:
    rows = await session.scalars(
        select(EnterpriseAsset).where(
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    return [
        {
            "asset_id": a.id,
            "name": a.name,
            "asset_type": a.asset_type,
            "category_id": a.category_id,
            "status": a.status,
            "source_file_id": a.source_file_id,
        }
        for a in rows
    ]


@router.get("/assets/{asset_id}")
async def get_asset(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    facts = await session.scalars(select(EnterpriseFact).where(EnterpriseFact.asset_id == asset.id))
    return {
        "asset_id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "category_id": asset.category_id,
        "status": asset.status,
        "source_file_id": asset.source_file_id,
        "facts": [
            {"fact_id": f.id, "fact_key": f.fact_key, "fact_value": f.fact_value, "confidence": float(f.confidence) if f.confidence is not None else None, "status": f.status}
            for f in facts
        ],
    }


@router.patch("/assets/{asset_id}/category")
async def correct_category(
    asset_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    asset.category_id = body["category_id"]
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="asset.category_change",
        object_type="enterprise_asset",
        object_id=asset.id,
        payload={"category_id": body["category_id"]},
    )
    await session.commit()
    return {"asset_id": asset.id, "category_id": asset.category_id}


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingest(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    asset_ids = body.get("asset_ids") or []
    task = Task(
        enterprise_id=user.enterprise_id,
        project_id=0,
        task_type=TaskType.ENTERPRISE_INGESTION,
        idempotency_key=f"ingest-{user.enterprise_id}-{'-'.join(str(i) for i in asset_ids)}-{len(asset_ids)}",
        status=int(TaskStatus.RUNNING),
        payload={"asset_ids": asset_ids},
    )
    session.add(task)
    await session.flush()
    ingest = EnterpriseIngestionTask(
        enterprise_id=user.enterprise_id,
        task_id=task.id,
        asset_ids=asset_ids,
        status=1,
    )
    session.add(ingest)

    categories = await _ensure_categories(session, user.enterprise_id)
    classified: list[dict] = []
    for asset_id in asset_ids:
        asset = await session.get(EnterpriseAsset, asset_id)
        if asset is None or asset.enterprise_id != user.enterprise_id:
            continue
        category, facts = _classify(asset.name)
        asset.category_id = categories.get(category)
        asset.asset_type = category
        asset.status = 2  # 待确认
        for fact_key, value, confidence in facts:
            session.add(
                EnterpriseFact(
                    enterprise_id=user.enterprise_id,
                    asset_id=asset.id,
                    fact_key=fact_key,
                    fact_value={"value": value},
                    confidence=confidence,
                    status=1,
                )
            )
        classified.append({"asset_id": asset.id, "category": category, "confidence": 0.8})

    task.status = int(TaskStatus.DONE)
    task.result = {"classified": classified}
    ingest.status = 2  # 待确认
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="enterprise.ingest",
        object_type="enterprise_ingestion_task",
        object_id=ingest.id,
    )
    await session.commit()
    return {"task_id": task.id, "ingest_id": ingest.id, "classified": classified}


@router.get("/ingest/{task_id}")
async def ingest_status(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    task = await session.scalar(
        select(Task).where(Task.id == task_id, Task.enterprise_id == user.enterprise_id)
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导入任务不存在")
    return {"task_id": task.id, "status": task.status, "result": task.result}
