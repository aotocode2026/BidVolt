"""文件上传/解包/解析/入库编排（M2）。"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UserContext
from app.models.doc import DocBlock
from app.models.enterprise_domain import EnterpriseAsset, EnterpriseAssetRevision
from app.models.file import ArchiveJob, FileObject
from app.models.project import Project
from app.models.project_material import ProjectEvent, ProjectMaterial, ProjectMaterialRevision
from app.services import file_safety, parser
from app.services.quota_service import check_storage
from app.services.storage import StorageProvider

storage = StorageProvider()


def _category_heuristic(name: str) -> str | None:
    lower = name.lower()
    for keyword, category in (
        ("营业执照", "证照"),
        ("执照", "证照"),
        ("资质", "资质"),
        ("许可证", "资质"),
        ("业绩", "业绩"),
        ("合同", "业绩"),
        ("中标", "业绩"),
        ("身份证", "人员"),
        ("证书", "人员"),
        ("检测", "检测报告"),
        ("报告", "检测报告"),
        ("参数", "产品参数"),
        ("产品", "产品参数"),
    ):
        if keyword in lower:
            return category
    return None


async def _get_project(session: AsyncSession, enterprise_id: int, project_id: int) -> Project:
    project = await session.scalar(
        select(Project).where(Project.id == project_id, Project.enterprise_id == enterprise_id)
    )
    if project is None or project.is_deleted:
        raise ValueError("项目不存在或已归档")
    return project


async def _parse_file(session: AsyncSession, fobj: FileObject) -> None:
    """解析并写 doc_block；失败只记录 parse_status，不阻塞入库。"""
    try:
        path = storage.open(fobj.bucket, fobj.object_key)
        image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}
        if fobj.ext in image_exts:
            from app.services.llm import DashScopeVLClient, vl_enabled

            if vl_enabled():
                text = await DashScopeVLClient().describe(path.read_bytes(), mime=fobj.mime_type or "image/png")
                session.add(
                    DocBlock(
                        file_id=fobj.id,
                        block_type="paragraph",
                        page_no=None,
                        block_index=0,
                        text_content=text,
                        extra={"source": "qwen-vl"},
                    )
                )
                fobj.status = 3
                fobj.category = fobj.category or _category_heuristic(fobj.original_name)
                return
            raise ValueError("视觉模型门禁关闭（P1），图片解析不可用")
        if fobj.ext in (".zip", ".rar", ".7z"):
            # 压缩包不作文本解析：.zip 供"压缩包导入"流程使用（导入前校验完整性，
            # 坏包红字拒绝）；.rar/.7z 当前既不能解析也不能导入，直接拒绝避免歧义。
            if fobj.ext != ".zip":
                raise ValueError("暂不支持 .rar/.7z：请转换为 .zip 后上传，或直接上传压缩包内的文件")
            import zipfile

            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
            if bad:
                raise ValueError(f"压缩包损坏（条目 {bad} 校验失败）")
            fobj.status = 3  # 接受为压缩包（正文文本由导入后的内部文件提供）
            fobj.category = fobj.category or "压缩包"
            return
        blocks = parser.parse_to_blocks(path, fobj.ext or "")
        # Issue #12 根因：重解析前必须清除旧块。此前 upload 时解析 1 次 + 每轮任务重解析又追加，
        # 同一文件 6 次解析累积 2370 块（6 倍重复文本），污染 LLM 抽取提示词导致要求碎片化。
        await session.execute(delete(DocBlock).where(DocBlock.file_id == fobj.id))
        for block in blocks:
            session.add(
                DocBlock(
                    file_id=fobj.id,
                    block_type=block["block_type"],
                    page_no=block.get("page_no"),
                    block_index=block["block_index"],
                    text_content=block.get("text_content"),
                    extra=block.get("extra"),
                )
            )
        fobj.status = 3  # parsed
        fobj.category = fobj.category or _category_heuristic(fobj.original_name)
    except Exception as exc:  # noqa: BLE001
        fobj.status = 4  # parse_failed
        fobj.parse_status = {"error_code": type(exc).__name__, "message": str(exc)}


async def reparse_file(session: AsyncSession, file_id: int) -> FileObject:
    """任务 handler 用：按 file_id 重新解析并更新项目材料状态。"""
    fobj = await session.get(FileObject, file_id)
    if fobj is None:
        raise ValueError(f"文件不存在：{file_id}")
    await _parse_file(session, fobj)
    material = await session.scalar(
        select(ProjectMaterial).where(ProjectMaterial.file_id == file_id)
    )
    if material is not None:
        material.status = 2 if fobj.status == 3 else 4
    return fobj


async def process_upload(
    session: AsyncSession,
    user: UserContext,
    data: bytes,
    filename: str,
    target: str,
    project_id: int | None = None,
    document_role: str | None = None,
) -> FileObject:
    if target not in ("enterprise", "project"):
        raise ValueError("target 必须是 enterprise 或 project")
    if target == "project":
        if project_id is None:
            raise ValueError("target=project 时必须传 project_id")
        await _get_project(session, user.enterprise_id, project_id)

    mime, ext = file_safety.validate_upload(filename, data)
    file_safety.virus_scan(data)
    await check_storage(session, user.enterprise_id, len(data))
    saved = storage.save(data, user.enterprise_id, filename)

    fobj = FileObject(
        enterprise_id=user.enterprise_id,
        project_id=project_id if target == "project" else None,
        owner_type=1 if target == "enterprise" else 2,
        bucket=saved["bucket"],
        object_key=saved["object_key"],
        sha256=saved["sha256"],
        original_name=filename,
        size_bytes=saved["size_bytes"],
        mime_type=mime,
        ext=ext,
        document_role=document_role,
        status=2,  # parsing
    )
    session.add(fobj)
    await session.flush()

    await _parse_file(session, fobj)
    if fobj.status != 3:
        # 积极报错（Issue #8 复盘）：解析失败的文件不允许入库/进入资料列表——
        # 上传即拒绝，红字给出原因，绝不让用户拿到一个"上传成功但用不了"的文件。
        ps = fobj.parse_status or {}
        detail = str(ps.get("message") or ps.get("error_code") or "未知原因").strip()
        raise ValueError(f"文件解析失败：{filename}（原因：{detail}）；请修复文件格式后重新上传")

    if target == "enterprise":
        asset = EnterpriseAsset(
            enterprise_id=user.enterprise_id,
            name=filename,
            asset_type=_category_heuristic(filename) or "other",
            source_file_id=fobj.id,
            status=1,
        )
        session.add(asset)
        await session.flush()
        session.add(
            EnterpriseAssetRevision(
                enterprise_id=user.enterprise_id,
                asset_id=asset.id,
                revision_no=1,
                file_id=fobj.id,
                sha256=fobj.sha256,
                created_by=user.user_id,
            )
        )
    else:
        material = ProjectMaterial(
            enterprise_id=user.enterprise_id,
            project_id=project_id,
            file_id=fobj.id,
            status=2 if fobj.status == 3 else 4,
        )
        session.add(material)
        await session.flush()
        session.add(
            ProjectMaterialRevision(
                enterprise_id=user.enterprise_id,
                material_id=material.id,
                revision_no=1,
                source_file_id=fobj.id,
            )
        )
        session.add(
            ProjectEvent(
                enterprise_id=user.enterprise_id,
                project_id=project_id,
                event_type="material_uploaded",
                event_data={"file_id": fobj.id, "parse_status": fobj.status},
            )
        )
        project = await _get_project(session, user.enterprise_id, project_id)
        if project.status == 1:  # draft → processing
            project.status = 2
    return fobj


async def process_archive(
    session: AsyncSession,
    user: UserContext,
    archive_file_id: int,
    target: str,
    project_id: int | None = None,
) -> ArchiveJob:
    src = await session.scalar(
        select(FileObject).where(
            FileObject.id == archive_file_id,
            FileObject.enterprise_id == user.enterprise_id,
            FileObject.is_deleted.is_(False),
        )
    )
    if src is None or src.ext != ".zip":
        raise ValueError("压缩包文件不存在或不是 zip")

    data = storage.open(src.bucket, src.object_key).read_bytes()
    file_safety.virus_scan(data)
    entries = file_safety.extract_zip(data)

    job = ArchiveJob(
        enterprise_id=user.enterprise_id,
        archive_file_id=archive_file_id,
        status=1,
        result={"imported": [], "failed": []},
    )
    session.add(job)
    await session.flush()

    imported: list[int] = []
    failed: list[dict] = []
    for entry in entries:
        try:
            fobj = await process_upload(
                session, user, entry["data"], entry["name"], target, project_id
            )
            imported.append(fobj.id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": entry["name"], "reason": str(exc)})

    job.status = 2 if not failed else 3
    job.result = {"imported": imported, "failed": failed}
    await session.commit()
    return job
