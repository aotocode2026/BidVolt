"""SQLAlchemy 模型包（导入即注册到 Base.metadata）。"""

from app.models.audit import AuditLog
from app.models.auth import AppUser, Enterprise, EnterprisePermission, ProjectEditLock, RefreshToken
from app.models.doc import DocBlock
from app.models.deliverable import (
    AIEditDiff,
    Deliverable,
    DeliverableContent,
    DeliverableVersion,
)
from app.models.enterprise_domain import (
    EnterpriseAsset,
    EnterpriseAssetCategory,
    EnterpriseAssetRevision,
    EnterpriseFact,
    EnterpriseFactEvidence,
    EnterpriseIngestionTask,
)
from app.models.export import ExportJob, FinalCheck
from app.models.file import ArchiveJob, FileObject
from app.models.project import Project
from app.models.project_material import (
    MaterialMatchResult,
    ProjectEvent,
    ProjectMaterial,
    ProjectMaterialRevision,
    ProjectSnapshot,
)
from app.models.quote import HistoryPriceSnapshot, QuoteCalc
from app.models.quota import TenantQuota
from app.models.requirement import Requirement, RequirementRevision
from app.models.review import (
    ReviewItem,
    ReviewMaterialLink,
    ReviewProvider,
    ReviewRun,
    ScoreRecord,
)
from app.models.task import Task

__all__ = [
    "AppUser",
    "AIEditDiff",
    "ArchiveJob",
    "AuditLog",
    "DocBlock",
    "Enterprise",
    "EnterpriseAsset",
    "EnterpriseAssetCategory",
    "EnterpriseAssetRevision",
    "EnterpriseFact",
    "EnterpriseFactEvidence",
    "EnterpriseIngestionTask",
    "EnterprisePermission",
    "ExportJob",
    "Deliverable",
    "DeliverableContent",
    "DeliverableVersion",
    "FileObject",
    "FinalCheck",
    "HistoryPriceSnapshot",
    "MaterialMatchResult",
    "Project",
    "ProjectEditLock",
    "ProjectEvent",
    "ProjectMaterial",
    "ProjectMaterialRevision",
    "ProjectSnapshot",
    "QuoteCalc",
    "ReviewItem",
    "ReviewMaterialLink",
    "ReviewProvider",
    "ReviewRun",
    "ScoreRecord",
    "RefreshToken",
    "Requirement",
    "RequirementRevision",
    "Task",
    "TenantQuota",
]
