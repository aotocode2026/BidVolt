"""企业资料库接口（4.2.7）。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext, _set_rls_context, get_current_user, require_capability, require_permission
from app.config import settings
from app.constants import Permission, TaskStatus, TaskType
from app.db import SessionLocal, get_session
from app.models.enterprise_domain import (
    EnterpriseAsset,
    EnterpriseAssetCategory,
    EnterpriseAssetRevision,
    EnterpriseFact,
    EnterpriseFactRevision,
    EnterpriseIngestionTask,
)
from app.models.task import Task
from app.services.asset_classification_service import classify_asset_with_ai
from app.services.audit import write_audit
from app.services.enterprise_service import (
    classify_asset_name as _classify,
)
from app.services.enterprise_service import (
    ensure_asset_categories as _ensure_categories,
)

router = APIRouter(prefix="/enterprise", tags=["enterprise"])

_CLASSIFY_SEMAPHORE = asyncio.Semaphore(max(1, int(settings.enterprise_classify_concurrency or 4)))


async def _classify_one_in_new_session(enterprise_id: int, asset_id: int) -> dict:
    """在独立短命会话中完成单个资产的 AI 分类，供并发 gather 使用。

    全局信号量把跨用户、跨请求的总并发限制在配置上限内；分类只读，
    结果由主请求会话统一写库，避免 AsyncSession 跨并发任务共享。
    """
    async with _CLASSIFY_SEMAPHORE:
        async with SessionLocal() as s:
            asset = await s.get(EnterpriseAsset, int(asset_id))
            if asset is None or int(asset.enterprise_id) != int(enterprise_id):
                return {
                    "asset_id": int(asset_id),
                    "status": "skipped",
                    "reason": "资产不存在或不属于本企业",
                }
            ai = await classify_asset_with_ai(s, asset)
            return {"asset_id": int(asset_id), "status": "ok", "ai": ai}


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
    user: UserContext = Depends(require_capability("search_assets")),
) -> list[dict]:
    # R7 线上定位（2026-08-31）：本端点曾对 1425 条资产返回空列表。双保险：
    # ①入口自设 RLS 上下文（与泵循环同款防御，不依赖上游依赖链事务状态）；
    # ②下方 list() 物化——ScalarResult 单次可迭代，双重迭代时第二轮拿到空集
    #   （经 CI 测试复现验证：这才是返回 [] 的真正根因，而非 RLS）。
    await _set_rls_context(session, user.enterprise_id)
    # 关键：ScalarResult 单次可迭代——必须 list() 物化，否则下方 fids 推导式
    # 消费掉结果集后，返回列表推导式拿到空集（线上 1425 条资产返回 [] 的真正根因）
    rows = list(
        await session.scalars(
            select(EnterpriseAsset).where(
                EnterpriseAsset.enterprise_id == user.enterprise_id,
                EnterpriseAsset.is_deleted.is_(False),
            )
        )
    )
    # 图片描述状态（sha256 缓存：入库后台任务产出）——asset 是图片时给 described 标记
    from app.models.file import FileObject, ImageDescription

    fids = [a.source_file_id for a in rows if a.source_file_id]
    fobjs = {
        f.id: f
        for f in await session.scalars(select(FileObject).where(FileObject.id.in_(fids)))
    } if fids else {}
    img_hashes = [f.sha256 for f in fobjs.values() if f.ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff")]
    desc_hashes = {
        d.sha256
        for d in await session.scalars(select(ImageDescription).where(ImageDescription.sha256.in_(img_hashes)))
    } if img_hashes else set()
    return [
        {
            "asset_id": a.id,
            "name": a.name,
            "asset_type": a.asset_type,
            "category_id": a.category_id,
            "status": a.status,
            "source_file_id": a.source_file_id,
            # 图片资料：是否已有结构化描述（described=True 时 get_asset 带 description）
            "image_described": (
                fobjs[a.source_file_id].sha256 in desc_hashes
                if a.source_file_id and a.source_file_id in fobjs else False
            ),
        }
        for a in rows
    ]


@router.get("/assets/{asset_id}")
async def get_asset(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("get_asset")),
) -> dict:
    await _set_rls_context(session, user.enterprise_id)  # 同 /assets：自含 RLS（R7 线上定位）
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
    # 图片资产的结构化描述（入库后台任务产出，sha256 缓存）：描述找图、装订复核
    image_description = None
    if asset.source_file_id:
        from app.models.file import FileObject, ImageDescription

        _fobj = await session.get(FileObject, asset.source_file_id)
        if _fobj is not None:
            _desc = await session.get(ImageDescription, _fobj.sha256)
            if _desc is not None:
                image_description = _desc.description
    return {
        "asset_id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "category_id": asset.category_id,
        "status": asset.status,
        "source_file_id": asset.source_file_id,
        "image_description": image_description,
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
    category_id = body.get("category_id")
    if not category_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 category_id")
    category = await session.get(EnterpriseAssetCategory, int(category_id))
    if category is None or category.enterprise_id != user.enterprise_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="分类不存在")
    asset.category_id = category.id
    asset.asset_type = category.name
    # 前端人工修改后即为最终口径，不再等待 AI 确认
    asset.status = 3
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="asset.category_change",
        object_type="enterprise_asset",
        object_id=asset.id,
        payload={"category_id": asset.category_id, "asset_type": asset.asset_type},
    )
    await session.commit()
    return {"asset_id": asset.id, "category_id": asset.category_id, "asset_type": asset.asset_type, "status": asset.status}


@router.post("/assets/{asset_id}/confirm-category")
async def confirm_asset_category(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_UPLOAD)),
) -> dict:
    """确认当前 AI/人工分类为最终分类。"""
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    asset.status = 3
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="enterprise.confirm_asset_category",
        object_type="enterprise_asset",
        object_id=asset.id,
        payload={"category_id": asset.category_id, "asset_type": asset.asset_type},
    )
    await session.commit()
    return {"asset_id": asset.id, "category_id": asset.category_id, "asset_type": asset.asset_type, "status": asset.status}


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
    # 先提交“处理中”标记：同步分类期间并发轮询能看到 分类中 状态（分类状态信号）
    await session.commit()
    await _set_rls_context(session, user.enterprise_id)

    categories = await _ensure_categories(session, user.enterprise_id)
    ai_results = await asyncio.gather(
        *(_classify_one_in_new_session(user.enterprise_id, int(asset_id)) for asset_id in asset_ids),
        return_exceptions=True,
    )

    classified: list[dict] = []
    for raw in ai_results:
        if isinstance(raw, BaseException):
            classified.append({"status": "failed", "reason": str(raw)[:200]})
            continue
        if raw.get("status") != "ok":
            classified.append(raw)
            continue
        asset_id = int(raw["asset_id"])
        ai = raw["ai"]
        asset = await session.get(EnterpriseAsset, asset_id)
        if asset is None or asset.enterprise_id != user.enterprise_id:
            classified.append({"asset_id": asset_id, "status": "skipped", "reason": "资产不存在或不属于本企业"})
            continue
        if asset.asset_type == "源文件":
            # 源压缩包无需业务内容分类：保留“源文件”身份与状态，跳过 AI 分类（discussion #55）
            classified.append(
                {
                    "asset_id": asset.id,
                    "category": "源文件",
                    "confidence": 1.0,
                    "source": "source_archive",
                    "note": "源文件无需业务分类",
                }
            )
            continue
        ai = await classify_asset_with_ai(session, asset)
        category = ai["category"]
        _, facts = _classify(asset.name)
        asset.category_id = categories.get(category)
        asset.asset_type = category
        asset.status = 2  # 待确认
        existing_keys = set(
            (await session.scalars(
                select(EnterpriseFact.fact_key).where(EnterpriseFact.asset_id == asset.id)
            )).all()
        )
        for fact_key, value, confidence in facts:
            if fact_key in existing_keys:
                continue  # 幂等：同名事实已存在（含上传自动入库），不重复插入
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
        classified.append(
            {
                "asset_id": asset.id,
                "category": category,
                "confidence": ai.get("confidence", 0.8),
                "source": ai.get("source", "unknown"),
                "evidence": ai.get("evidence"),
            }
        )

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


@router.get("/classification-status")
async def classification_status(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """企业资料分类进行中信号：pending=true 时前端显示“分类中”，完成后一次性刷新最终数量。"""
    await _set_rls_context(session, user.enterprise_id)
    pending_assets = await session.scalar(
        select(func.count())
        .select_from(EnterpriseAsset)
        .where(
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
            EnterpriseAsset.status == 1,
            EnterpriseAsset.asset_type != "源文件",
        )
    )
    running_ingests = await session.scalar(
        select(func.count())
        .select_from(EnterpriseIngestionTask)
        .where(
            EnterpriseIngestionTask.enterprise_id == user.enterprise_id,
            EnterpriseIngestionTask.status == 1,
        )
    )
    return {
        "pending": bool(pending_assets or running_ingests),
        "pending_asset_count": int(pending_assets or 0),
        "running_ingest_count": int(running_ingests or 0),
    }


@router.post("/assets/{asset_id}/classify", status_code=status.HTTP_202_ACCEPTED)
async def classify_enterprise_asset(
    asset_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("classify_enterprise_asset")),
) -> dict:
    """企业资料导入任务专属：识别资料类型、抽取结构化字段、建议归档目录。"""
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    if asset.asset_type == "源文件":
        return {
            "asset_id": asset.id,
            "category": "源文件",
            "confidence": 1.0,
            "source": "source_archive",
            "note": "源文件无需业务分类",
        }
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 task_id")
    task = await session.scalar(
        select(Task).where(
            Task.id == int(task_id),
            Task.enterprise_id == user.enterprise_id,
            Task.task_type == TaskType.ENTERPRISE_INGESTION,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="非企业资料导入任务，拒绝分类")

    async with _CLASSIFY_SEMAPHORE:
        ai = await classify_asset_with_ai(session, asset)
    category = ai["category"]
    _, facts = _classify(asset.name)
    categories = await _ensure_categories(session, user.enterprise_id)
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
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="enterprise.classify_asset",
        object_type="enterprise_asset",
        object_id=asset.id,
        payload={"task_id": task.id, "category": category},
    )
    await session.commit()
    return {
        "asset_id": asset.id,
        "category": category,
        "confidence": ai.get("confidence", 0.8),
        "source": ai.get("source", "unknown"),
        "evidence": ai.get("evidence"),
        "status": 2,
    }


@router.post("/assets/{asset_id}/facts", status_code=status.HTTP_201_CREATED)
async def upsert_enterprise_facts(
    asset_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_capability("upsert_enterprise_facts")),
) -> dict:
    """企业资料导入任务专属：写入/更新企业事实（结构化字段 + 证据引用）。"""
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 task_id")
    task = await session.scalar(
        select(Task).where(
            Task.id == int(task_id),
            Task.enterprise_id == user.enterprise_id,
            Task.task_type == TaskType.ENTERPRISE_INGESTION,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="非企业资料导入任务，拒绝写入")

    facts = body.get("facts") or []
    if not facts:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="facts 为空")
    created = []
    for f in facts:
        fact_value = f.get("value")
        if not isinstance(fact_value, dict):
            fact_value = {"value": fact_value}
        if f.get("evidence_ref"):
            fact_value["evidence_ref"] = f["evidence_ref"]  # 来源文件版本 + 原文定位
        fact = EnterpriseFact(
            enterprise_id=user.enterprise_id,
            asset_id=asset.id,
            fact_key=f["fact_key"],
            fact_value=fact_value,
            confidence=float(f.get("confidence", 0.6)),
            status=2 if float(f.get("confidence", 0.6)) < 0.7 else 3,  # 低置信待人工确认
        )
        session.add(fact)
        await session.flush()
        created.append(fact.id)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="enterprise.upsert_facts",
        object_type="enterprise_asset",
        object_id=asset.id,
        payload={"task_id": task.id, "fact_ids": created},
    )
    await session.commit()
    return {"asset_id": asset.id, "fact_ids": created, "count": len(created)}


@router.get("/assets/{asset_id}/revisions")
async def asset_revisions(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """企业资料 revision 列表（Issue #2 #23）。"""
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    rows = (
        await session.scalars(
            select(EnterpriseAssetRevision)
            .where(
                EnterpriseAssetRevision.asset_id == asset_id,
                EnterpriseAssetRevision.enterprise_id == user.enterprise_id,
            )
            .order_by(EnterpriseAssetRevision.revision_no.desc())
        )
    ).all()
    return {
        "items": [
            {
                "revision_id": r.id,
                "revision_no": r.revision_no,
                "file_id": r.file_id,
                "sha256": r.sha256,
                "source_location": r.source_location,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/assets/{asset_id}/facts")
async def list_asset_facts(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """企业资料当前结构化字段（Issue #2 #22/#24 展示用）。"""
    asset = await session.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == user.enterprise_id,
            EnterpriseAsset.is_deleted.is_(False),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资料不存在")
    rows = (
        await session.scalars(
            select(EnterpriseFact)
            .where(
                EnterpriseFact.asset_id == asset_id,
                EnterpriseFact.enterprise_id == user.enterprise_id,
            )
            .order_by(EnterpriseFact.id.asc())
        )
    ).all()
    return {
        "items": [
            {
                "fact_id": f.id,
                "fact_key": f.fact_key,
                "fact_value": f.fact_value,
                "confidence": float(f.confidence) if f.confidence is not None else None,
                "status": f.status,
                "expires_at": f.expires_at.isoformat() if f.expires_at else None,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            }
            for f in rows
        ]
    }


@router.put("/facts/{fact_id}")
async def confirm_or_correct_fact(
    fact_id: int,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.PROJECT_EDIT)),
) -> dict:
    """确认或纠正结构化 fact，并写修订记录（Issue #2 #24）。"""
    fact = await session.scalar(
        select(EnterpriseFact).where(
            EnterpriseFact.id == fact_id,
            EnterpriseFact.enterprise_id == user.enterprise_id,
        )
    )
    if fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事实不存在")

    new_value = body.get("fact_value")
    if new_value is not None and not isinstance(new_value, dict):
        new_value = {"value": new_value}
    confirmed = body.get("confirmed") is True
    if confirmed and new_value is None:
        fact.status = 2  # 已确认
    elif new_value is not None:
        fact.fact_value = new_value
        fact.status = 3  # 已纠正
        fact.confidence = 1.0
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="需要 fact_value（纠正）或 confirmed=true（确认）",
        )

    prev_count = await session.scalar(
        select(func.count()).select_from(EnterpriseFactRevision).where(
            EnterpriseFactRevision.fact_id == fact.id,
            EnterpriseFactRevision.enterprise_id == user.enterprise_id,
        )
    )
    revision = EnterpriseFactRevision(
        enterprise_id=user.enterprise_id,
        fact_id=fact.id,
        revision_no=int(prev_count or 0) + 1,
        fact_value=dict(fact.fact_value),
        confidence=float(fact.confidence) if fact.confidence is not None else None,
        status=fact.status,
        note=body.get("note"),
        created_by=user.user_id,
    )
    session.add(revision)
    await write_audit(
        session,
        enterprise_id=user.enterprise_id,
        user_id=user.user_id,
        action="enterprise.fact_confirm_or_correct",
        object_type="enterprise_fact",
        object_id=fact.id,
        payload={"status": fact.status, "revision_no": revision.revision_no},
    )
    await session.commit()
    return {
        "fact_id": fact.id,
        "status": fact.status,
        "revision_no": revision.revision_no,
        "fact_value": fact.fact_value,
    }


@router.get("/facts/{fact_id}/revisions")
async def fact_revisions(
    fact_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """fact 修订记录（Issue #2 #24）。"""
    fact = await session.scalar(
        select(EnterpriseFact).where(
            EnterpriseFact.id == fact_id,
            EnterpriseFact.enterprise_id == user.enterprise_id,
        )
    )
    if fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事实不存在")
    rows = (
        await session.scalars(
            select(EnterpriseFactRevision)
            .where(
                EnterpriseFactRevision.fact_id == fact_id,
                EnterpriseFactRevision.enterprise_id == user.enterprise_id,
            )
            .order_by(EnterpriseFactRevision.revision_no.desc())
        )
    ).all()
    return {
        "items": [
            {
                "revision_id": r.id,
                "revision_no": r.revision_no,
                "fact_value": r.fact_value,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "status": r.status,
                "note": r.note,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/ingest")
async def list_ingest_tasks(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_permission(Permission.FILE_READ)),
) -> dict:
    """企业资料处理队列（Issue #2 #21：刷新后恢复活动/历史处理任务）。"""
    rows = (
        await session.scalars(
            select(EnterpriseIngestionTask)
            .where(EnterpriseIngestionTask.enterprise_id == user.enterprise_id)
            .order_by(EnterpriseIngestionTask.id.desc())
            .limit(100)
        )
    ).all()
    return {
        "items": [
            {
                "ingest_id": r.id,
                "task_id": r.task_id,
                "asset_ids": r.asset_ids,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


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
