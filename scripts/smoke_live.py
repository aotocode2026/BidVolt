"""线上冒烟：对部署环境打完整业务流（注册→项目→成果→评标→报价→导出）。"""

from __future__ import annotations

import argparse
import io
import time
import zipfile

import httpx


def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        email = f"live-{int(time.time())}@test.com"
        reg = c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Abc12345", "enterprise_name": "冒烟企业"},
        )
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        print("[1] register ok")

        pid = c.post("/api/v1/projects", json={"name": "冒烟项目"}, headers=headers).json()["project_id"]
        print(f"[2] project ok: {pid}")

        models = {
            1: {"nodes": [{"id": "n1", "type": "paragraph", "text": "商务响应"}]},
            2: {"nodes": [{"id": "n1", "type": "paragraph", "text": "技术方案"}]},
            3: {"type": "sheet", "sheets": [{"name": "报价单", "rows": [["材料", "价格"], ["电缆", "120"]]}]},
        }
        quote_did = None
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
            if dtype == 3:
                quote_did = did
        print("[3] deliverables ok")

        upload = c.post(
            "/api/v1/files/upload",
            data={"target": "project", "project_id": str(pid)},
            files=[("files", ("招标.txt", "资格要求".encode(), "text/plain"))],
            headers=headers,
        )
        assert upload.json()["files"][0]["status"] == 3
        print("[4] upload+parse ok")

        ev = c.post(f"/api/v1/projects/{pid}/evaluate", json={}, headers=headers).json()
        confirm = c.post(
            f"/api/v1/projects/{pid}/scores/{ev['score_id']}/items/confirm",
            json={"item_ids": ev["item_ids"], "expected_version": ev["snapshot_id"]},
            headers=headers,
        )
        assert all(x["status"] in ("succeeded", "skipped") for x in confirm.json()["results"])
        print("[5] evaluate+confirm ok")

        calc = c.post(
            "/api/v1/quotes/calculate",
            json={"material_ref": "CABLE-YJV-3x95", "cost": 100, "min_profit_rate": 0.1, "project_id": pid},
            headers=headers,
        ).json()
        applied = c.post(
            "/api/v1/quotes/apply",
            json={"calc_id": calc["calc_id"], "deliverable_id": quote_did, "expected_version_no": 1},
            headers=headers,
        )
        applied.raise_for_status()
        print("[6] quote calculate+apply ok")

        check = c.post(f"/api/v1/projects/{pid}/check", json={}, headers=headers).json()
        assert check["passed"] is True
        c.post(
            f"/api/v1/projects/{pid}/export",
            json={"formats": ["docx", "xlsx"], "with_manifest": True},
            headers=headers,
        ).json()
        pkg = c.get(f"/api/v1/projects/{pid}/delivery-package", headers=headers)
        with zipfile.ZipFile(io.BytesIO(pkg.content)) as zf:
            names = zf.namelist()
        assert "manifest.json" in names
        print(f"[7] check+export ok, 交付包 {len(names)} 个条目")
        print("SMOKE PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://47.100.182.3:28123")
    args = parser.parse_args()
    run(args.base)
