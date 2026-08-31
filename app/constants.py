"""全局常量：权限点、状态机等。"""

from __future__ import annotations

from enum import IntEnum


class Permission:
    """权限点枚举（V1，ADR D18：权限点 + 用户权限集合）。"""

    FILE_READ = "file.read"
    FILE_DOWNLOAD = "file.download"
    FILE_UPLOAD = "file.upload"
    PROJECT_EDIT = "project.edit"
    DELIVERABLE_EDIT = "deliverable.edit"
    DELIVERABLE_EXPORT = "deliverable.export"
    QUOTE_CALCULATE = "quote.calculate"
    QUOTE_APPLY = "quote.apply"
    SCORE_VIEW = "score.view"
    SCORE_CONFIRM = "score.confirm"
    REVIEW_PROVIDER_CONFIG = "review_provider.config"
    ADMIN_USER = "admin.user"
    ADMIN_QUOTA = "admin.quota"
    AUDIT_VIEW = "audit.view"

    ALL = {
        FILE_READ,
        FILE_DOWNLOAD,
        FILE_UPLOAD,
        PROJECT_EDIT,
        DELIVERABLE_EDIT,
        DELIVERABLE_EXPORT,
        QUOTE_CALCULATE,
        QUOTE_APPLY,
        SCORE_VIEW,
        SCORE_CONFIRM,
        REVIEW_PROVIDER_CONFIG,
        ADMIN_USER,
        ADMIN_QUOTA,
        AUDIT_VIEW,
    }

    # 管理员专属
    RESTRICTED = {
        REVIEW_PROVIDER_CONFIG,
        ADMIN_USER,
        ADMIN_QUOTA,
        AUDIT_VIEW,
    }

    # 注册用户默认权限集 = 全部权限点 - 管理员专属
    DEFAULT = ALL - RESTRICTED


# 提问关问答窗口（分钟）：客户在此窗口内作答；超时后服务端注入
# 「已超时，由你自行决定」信号（纯信号，不替主会话决定），问卡仍可补答
QUESTION_GATE_WINDOW_MINUTES = 20


class ProjectStatus(IntEnum):
    DRAFT = 1
    PROCESSING = 2
    PARTIAL_DONE = 3
    DONE = 4
    ARCHIVED = 9


# 项目状态机（4.1.6.3）
PROJECT_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.DRAFT: {ProjectStatus.PROCESSING, ProjectStatus.ARCHIVED},
    ProjectStatus.PROCESSING: {ProjectStatus.PARTIAL_DONE, ProjectStatus.DRAFT, ProjectStatus.ARCHIVED},
    ProjectStatus.PARTIAL_DONE: {ProjectStatus.DONE, ProjectStatus.PROCESSING, ProjectStatus.ARCHIVED},
    ProjectStatus.DONE: {ProjectStatus.ARCHIVED},
    ProjectStatus.ARCHIVED: set(),
}


class TaskStatus(IntEnum):
    QUEUED = 1
    RUNNING = 2
    DONE = 3
    FAILED_RETRYABLE = 4
    CANCELLED = 5
    FAILED_TERMINAL = 6


class TaskType:
    ENTERPRISE_INGESTION = "enterprise_ingestion"
    TENDER_PARSE = "tender_parse"
    MATERIAL_MATCH = "material_match"
    BID_GENERATE = "bid_generate"
    BID_REVIEW = "bid_review"
    MOCK_EVALUATE = "mock_evaluate"
    TARGETED_EDIT = "targeted_edit"
    CHAT = "chat"
    # 新方案（Agent 主会话端到端，与旧任务类型完全隔离）
    AGENT_PIPELINE = "agent_pipeline"
    # 图片描述（入库后台任务）：sha256 缓存，每张图只描述一次
    IMAGE_DESCRIBE = "image_describe"

    ALL = {
        ENTERPRISE_INGESTION,
        TENDER_PARSE,
        MATERIAL_MATCH,
        BID_GENERATE,
        BID_REVIEW,
        MOCK_EVALUATE,
        TARGETED_EDIT,
        CHAT,
        AGENT_PIPELINE,
        IMAGE_DESCRIBE,
    }
