"""SQLAlchemy 模型包（导入即注册到 Base.metadata）。"""

from app.models.agent import AgentSessionEvent
from app.models.audit import AuditLog
from app.models.auth import AppUser, Enterprise, EnterprisePermission, ProjectEditLock, RefreshToken
from app.models.chat import Conversation, ConversationMessage
from app.models.deliverable import (
    AIEditDiff,
    Deliverable,
    DeliverableContent,
    DeliverableVersion,
)
from app.models.doc import DocBlock
from app.models.editor import EditorSession
from app.models.enterprise_domain import (
    EnterpriseAsset,
    EnterpriseAssetCategory,
    EnterpriseAssetRevision,
    EnterpriseFact,
    EnterpriseFactEvidence,
    EnterpriseFactRevision,
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
from app.models.quota import TenantQuota
from app.models.quote import HistoryPriceSnapshot, QuoteCalc
from app.models.requirement import Requirement, RequirementRevision
from app.models.review import (
    ReviewItem,
    ReviewMaterialLink,
    ReviewProvider,
    ReviewRun,
    ScoreRecord,
)
from app.models.search import Citation, SearchSource
from app.models.task import Task
from app.models.tender_notice import TenderNotice

__all__ = [
    "AgentSessionEvent",
    "AppUser",
    "AIEditDiff",
    "ArchiveJob",
    "AuditLog",
    "Citation",
    "Conversation",
    "ConversationMessage",
    "DocBlock",
    "Enterprise",
    "EnterpriseAsset",
    "EnterpriseAssetCategory",
    "EnterpriseAssetRevision",
    "EnterpriseFact",
    "EnterpriseFactEvidence",
    "EnterpriseFactRevision",
    "EnterpriseIngestionTask",
    "EnterprisePermission",
    "ExportJob",
    "EditorSession",
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
    "SearchSource",
    "RefreshToken",
    "Requirement",
    "RequirementRevision",
    "Task",
    "TenantQuota",
    "TenderNotice",
]
