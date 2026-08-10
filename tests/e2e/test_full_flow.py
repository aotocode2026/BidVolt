"""端到端：真实 uvicorn 服务跑完整业务流（注册→项目→成果→评标→报价→导出）。"""

from __future__ import annotations

import os
import io
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine

import app.models  # noqa: F401
from app.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DB = REPO_ROOT / ".test_e2e.db"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    E2E_DB.unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{E2E_DB}")
    Base.metadata.create_all(engine)
    engine.dispose()

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{E2E_DB}",
        "STORAGE_ROOT": str(REPO_ROOT / ".test_e2e_storage"),
        "TASK_WORKER_ENABLED": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=2).json() == {"status": "ok"}:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("服务启动超时")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for _ in range(10):
        try:
            E2E_DB.unlink()
            break
        except PermissionError:
            time.sleep(0.3)
    shutil.rmtree(REPO_ROOT / ".test_e2e_storage", ignore_errors=True)


@pytest.mark.e2e
def test_full_business_flow(live_server):
    base = live_server
    with httpx.Client(base_url=base, timeout=30) as c:
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": "e2e@test.com", "password": "Abc12345", "enterprise_name": "端到端企业"},
        )
        assert reg.status_code == 201
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

        pid = c.post("/api/v1/projects", json={"name": "E2E 项目"}, headers=headers).json()["project_id"]

        # 三份成果
        deliverable_ids = {}
        models = {
            1: {"nodes": [{"id": "n1", "type": "paragraph", "text": "商务响应"}]},
            2: {"nodes": [{"id": "n1", "type": "paragraph", "text": "技术方案"}]},
            3: {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["材料", "价格"], ["电缆", "120"]]}]},
        }
        for dtype, model in models.items():
            did = c.post(
                "/api/v1/deliverables",
                json={"project_id": pid, "deliverable_type": dtype, "title": f"成果{dtype}"},
                headers=headers,
            ).json()["deliverable_id"]
            c.post(
                f"/api/v1/deliverables/{did}/versions",
                json={"content": model, "version_type": 2},
                headers=headers,
            )
            deliverable_ids[dtype] = did

        # 上传项目材料（解析）
        upload = c.post(
            "/api/v1/files/upload",
            data={"target": "project", "project_id": str(pid)},
            files=[("files", ("招标文件.txt", "资格要求：电力施工总承包三级".encode("utf-8"), "text/plain"))],
            headers=headers,
        )
        assert upload.json()["files"][0]["status"] == 3

        # 评标闭环
        ev = c.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=headers)
        assert ev.status_code == 200
        score_id, item_ids = ev.json()["score_id"], ev.json()["item_ids"]
        confirm = c.post(
            f"/api/v1/projects/{pid}/scores/{score_id}/items/confirm",
            json={"item_ids": item_ids, "expected_version": ev.json()["snapshot_id"]},
            headers=headers,
        )
        assert all(x["status"] in ("succeeded", "skipped") for x in confirm.json()["results"])

        # 报价
        calc = c.post(
            "/api/v1/quotes/calculate",
            json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1, "project_id": pid},
            headers=headers,
        )
        assert calc.json()["result"]["sample_count"] >= 5
        applied = c.post(
            "/api/v1/quotes/apply",
            json={"calc_id": calc.json()["calc_id"], "deliverable_id": deliverable_ids[3], "expected_version_no": 1},
            headers=headers,
        )
        assert applied.status_code == 201

        # 终检 + 导出 + 交付包
        check = c.post(f"/api/v1/projects/{pid}/check", json={}, headers=headers)
        assert check.json()["passed"] is True

        exp = c.post(
            f"/api/v1/projects/{pid}/export",
            json={"formats": ["docx", "xlsx"], "with_manifest": True},
            headers=headers,
        )
        assert exp.json()["status"] == 2
        pkg = c.get(f"/api/v1/projects/{pid}/delivery-package", headers=headers)
        assert pkg.status_code == 200
        with zipfile.ZipFile(io.BytesIO(pkg.content)) as zf:
            assert "manifest.json" in zf.namelist()
