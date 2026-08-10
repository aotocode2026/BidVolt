from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    tender_no: str | None = None
    deadline: datetime | None = None
    note: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    tender_no: str | None = None
    deadline: datetime | None = None
    note: str | None = None


class ProjectStatusUpdate(BaseModel):
    status: int


class ProjectResponse(BaseModel):
    project_id: int
    name: str
    tender_no: str | None
    deadline: datetime | None
    status: int
    note: str | None
    updated_at: datetime


class Page(BaseModel):
    items: list
    total: int
    page: int
    size: int
