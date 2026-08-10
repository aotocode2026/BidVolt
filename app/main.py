"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import auth, deliverables, enterprise, files, health, locks, projects, quotes, tasks

app = FastAPI(title="BidVolt API", version="0.1.0")

app.include_router(health.router)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(locks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(enterprise.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(deliverables.router, prefix="/api/v1")
app.include_router(quotes.router, prefix="/api/v1")


@app.get("/")
async def root() -> dict:
    return {"service": "BidVolt API", "docs": "/docs"}
