# -*- coding: utf-8 -*-
"""真实 PostgreSQL + RLS 集成冒烟（容器内运行）。

验证：
1) RLS FORCE 策略对 business 表生效：SET app.enterprise_id 后，无企业过滤的查询
   也只会返回当前租户数据；跨租户 WHERE 查询返回 0。
2) 任务级 capability token：合法调用 200，篡改 token 403。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

import asyncpg
import httpx


def _env_value(path: str, key: str) -> str:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def _dsn(database_url: str) -> str:
    # postgresql+asyncpg://user:pass@host:port/db -> postgresql://...
    return database_url.replace("+asyncpg", "")


async def _rls_check(dsn: str, eid_a: int, eid_b: int) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT set_config('app.enterprise_id', $1, false)", str(eid_a))
        all_rows = await conn.fetchval("SELECT count(*) FROM project")
        own_rows = await conn.fetchval("SELECT count(*) FROM project WHERE enterprise_id = $1", eid_a)
        assert all_rows == own_rows, f"RLS 未隔离：无过滤查询 {all_rows} != 本企业 {own_rows}"
        cross = await conn.fetchval("SELECT count(*) FROM project WHERE enterprise_id = $1", eid_b)
        assert cross == 0, f"跨租户查询返回 {cross} 行（RLS 未强制）"
        print(f"RLS OK: enterprise {eid_a} 无过滤查询仅见本企业 {own_rows} 行，跨租户 0 行")
    finally:
        await conn.close()


async def _capability_check(base: str, headers: dict, pid: int) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        task = c.post(
            f"/api/v1/projects/{pid}/tasks",
            json={"task_type": "chat", "payload": {"message": "hi"}, "idempotency_key": f"cap-{int(time.time())}"},
            headers=headers,
        )
        task.raise_for_status()
        cap = task.json()["capability_token"]
        ok = c.get(
            f"/api/v1/files/projects/{pid}/materials",
            headers={"X-Bidvolt-Cap": cap},
        )
        assert ok.status_code == 200, ok.text
        tampered = c.get(
            f"/api/v1/files/projects/{pid}/materials",
            headers={"X-Bidvolt-Cap": cap + "x"},
        )
        assert tampered.status_code in (401, 403), tampered.status_code
        print(f"CAPABILITY OK: 合法调用 200，篡改 token {tampered.status_code}")


def run(base: str, env_path: str) -> None:
    database_url = _env_value(env_path, "DATABASE_URL")
    with httpx.Client(base_url=base, timeout=60) as c:
        reg_a = c.post(
            "/api/v1/auth/register",
            json={"email": f"rls-a-{int(time.time())}@test.com", "password": "Abc12345", "enterprise_name": "RLSA"},
        )
        reg_a.raise_for_status()
        eid_a = reg_a.json()["enterprise_id"]
        h_a = {"Authorization": f"Bearer {reg_a.json()['access_token']}"}
        pid_a = c.post("/api/v1/projects", json={"name": "RLSA项目"}, headers=h_a).json()["project_id"]

        reg_b = c.post(
            "/api/v1/auth/register",
            json={"email": f"rls-b-{int(time.time())}@test.com", "password": "Abc12345", "enterprise_name": "RLSB"},
        )
        reg_b.raise_for_status()
        eid_b = reg_b.json()["enterprise_id"]
        h_b = {"Authorization": f"Bearer {reg_b.json()['access_token']}"}
        pid_b = c.post("/api/v1/projects", json={"name": "RLSB项目"}, headers=h_b).json()["project_id"]

        # 跨企业 IDOR：B 不能读取 A 的项目
        idor = c.get(f"/api/v1/projects/{pid_a}", headers=h_b)
        assert idor.status_code in (403, 404), idor.status_code
        print(f"IDOR OK: B 读取 A 项目 -> {idor.status_code}")

        import asyncio

        asyncio.run(_rls_check(_dsn(database_url), eid_a, eid_b))
        asyncio.run(_capability_check(base, h_a, pid_a))
        print("PG+RLS SMOKE PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8123")
    ap.add_argument("--env", default="/data/bidvolt/.env")
    run(ap.parse_args().base, ap.parse_args().env)
