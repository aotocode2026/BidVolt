"""FastAPI 应用入口。"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    audit,
    auth,
    chat,
    deliverables,
    editor,
    enterprise,
    export,
    files,
    health,
    knowledge,
    locks,
    matches,
    projects,
    quotes,
    requirements,
    review,
    search,
    snapshots,
    tasks,
    tender_notices,
)
from app.config import settings

app = FastAPI(title="BidVolt API", version="0.1.0")

# CORS（Issue #6 P0 生产访问方案）：来源可配置（CORS_ORIGINS 逗号分隔），默认全部允许；
# Bearer 鉴权无 cookie 依赖，生产可收紧为白名单或改用同源反代。
_cors_origins = (
    [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if settings.cors_origins
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

_STATUS_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "unprocessable_entity",
    429: "rate_limited",
}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """错误 envelope（Issue #6 P2）：保留 detail + 稳定 code + request_id。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "code": _STATUS_CODES.get(exc.status_code, "http_error"),
            "request_id": str(uuid.uuid4()),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 校验错误（Issue #6 P2）：附带字段级错误明细。"""
    return JSONResponse(
        status_code=422,
        content={
            "detail": "请求参数校验失败",
            "code": "validation_error",
            "request_id": str(uuid.uuid4()),
            "field_errors": exc.errors()[:20],
        },
    )

app.include_router(health.router)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(snapshots.router, prefix="/api/v1")
app.include_router(locks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(enterprise.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(deliverables.router, prefix="/api/v1")
app.include_router(editor.router, prefix="/api/v1")
app.include_router(quotes.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(review.providers_router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
app.include_router(requirements.router, prefix="/api/v1")
app.include_router(requirements.projects_router, prefix="/api/v1")
app.include_router(matches.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(tender_notices.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")

app.mount("/demo", StaticFiles(directory="app/static", html=True), name="demo")


@app.get("/")
async def root() -> dict:
    return {"service": "BidVolt API", "docs": "/docs"}
