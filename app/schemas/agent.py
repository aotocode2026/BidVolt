"""成文产物（AgentArtifact）响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentArtifactSummary(BaseModel):
    """成果目录列表项：稳定字段契约，供前端渲染正式成果目录。"""

    artifact_id: int
    project_id: int
    task_id: int
    kind: str
    name: str
    group: str
    filename: str
    mime: str
    bytes: int
    version_no: int
    is_internal: bool
    created_at: datetime
    updated_at: datetime
    download_url: str


class AgentArtifactListResponse(BaseModel):
    """产物清单响应：保留 artifacts 字段兼容 MCP，同时提供分页元信息。"""

    artifacts: list[AgentArtifactSummary]
    total: int = Field(description="当前筛选条件下产物总数")
    page: int = 1
    size: int = 100


class AgentArtifactInspect(BaseModel):
    """产物详情响应基座；docx/xlsx/zip 的预览字段通过 extra=allow 兼容。"""

    model_config = ConfigDict(extra="allow")

    artifact_id: int
    project_id: int
    task_id: int
    kind: str
    name: str
    group: str
    filename: str
    mime: str
    bytes: int
    version_no: int
    is_internal: bool
    created_at: datetime
    updated_at: datetime
    download_url: str
