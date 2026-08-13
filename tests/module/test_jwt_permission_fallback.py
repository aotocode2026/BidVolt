from __future__ import annotations

import json

from sqlalchemy import create_engine, text

TEST_DB = "./.test_bidvolt.db"


def _register(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "jwt@test.com", "password": "Abc12345", "enterprise_name": "权限企业"},
    )
    assert r.status_code == 201
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data


def _set_user_permissions(user_id: int, permissions: list[str]) -> None:
    engine = create_engine(f"sqlite:///{TEST_DB}")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_user SET permissions = :p WHERE id = :id"),
            {"p": json.dumps(permissions), "id": user_id},
        )
    engine.dispose()


def test_jwt_fallback_enforces_tool_permission(client):
    h, data = _register(client)
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=h).json()["project_id"]

    # 默认企业权限包含 FILE_READ，list_project_materials 走 JWT 回退应放行
    ok = client.get(f"/api/v1/files/projects/{pid}/materials", headers=h)
    assert ok.status_code == 200

    # 去掉 FILE_READ 后，JWT 回退必须 403（回归修复：不再绕过权限检查）
    _set_user_permissions(data["user_id"], ["project.edit"])
    denied = client.get(f"/api/v1/files/projects/{pid}/materials", headers=h)
    assert denied.status_code == 403
