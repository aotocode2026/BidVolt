from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    tender_no: str | None = None
    buyer: str | None = None
    deadline: datetime | None = None
    note: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    tender_no: str | None = None
    buyer: str | None = None
    deadline: datetime | None = None
    note: str | None = None


class ProjectStatusUpdate(BaseModel):
    status: int


class ProjectResponse(BaseModel):
    project_id: int
    name: str
    tender_no: str | None
    buyer: str | None
    deadline: datetime | None
    status: int
    note: str | None
    updated_at: datetime
    summary: dict | None = None  # 可解释摘要（Issue #6 P1）：材料数/成果数/评审数/最新评分/缺失项


class Page(BaseModel):
    items: list
    total: int
    page: int
    size: int
