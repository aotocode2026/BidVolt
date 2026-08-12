"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (
    auth,
    audit,
    deliverables,
    enterprise,
    export,
    files,
    health,
    locks,
    matches,
    projects,
    quotes,
    requirements,
    review,
    search,
    snapshots,
    tasks,
)

app = FastAPI(title="BidVolt API", version="0.1.0")

app.include_router(health.router)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(snapshots.router, prefix="/api/v1")
app.include_router(locks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(enterprise.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(deliverables.router, prefix="/api/v1")
app.include_router(quotes.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(review.providers_router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
app.include_router(requirements.router, prefix="/api/v1")
app.include_router(requirements.projects_router, prefix="/api/v1")
app.include_router(matches.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")

app.mount("/demo", StaticFiles(directory="app/static", html=True), name="demo")


@app.get("/")
async def root() -> dict:
    return {"service": "BidVolt API", "docs": "/docs"}
