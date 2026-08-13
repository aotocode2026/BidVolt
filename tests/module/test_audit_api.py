"""A-11 审计查看：租户隔离 + audit.view 权限门禁。"""

from __future__ import annotations


def _register(client, email, ent):
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Abc12345", "enterprise_name": ent},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_audit_logs_tenant_scoped_and_permission_gated(client):
    ha = _register(client, "audit-a@test.com", "审计企业A")
    _register(client, "audit-b@test.com", "审计企业B")  # 企业 B：后续断言其看不到 A 的日志

    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=ha).json()["project_id"]
    # 默认用户无 audit.view
    r = client.get("/api/v1/audit/logs", headers=ha)
    assert r.status_code == 403

    # 授权 audit.view 后可见本租户日志
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///./.test_bidvolt.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_user SET permissions = '[\"audit.view\"]' "
                "WHERE email = 'audit-a@test.com'"
            )
        )
    engine.dispose()

    # 重新登录获取含 audit.view 的 token
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "audit-a@test.com", "password": "Abc12345"},
    )
    assert login.status_code == 200
    ha2 = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r2 = client.get("/api/v1/audit/logs", headers=ha2)
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert items, "应有 project.create 审计记录"
    assert all(item["project_id"] == pid or item["project_id"] is None for item in items)

    # B 即使获得 audit.view 也看不到 A 的日志
    engine = create_engine("sqlite:///./.test_bidvolt.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_user SET permissions = '[\"audit.view\"]' "
                "WHERE email = 'audit-b@test.com'"
            )
        )
    engine.dispose()
    login_b = client.post(
        "/api/v1/auth/login",
        json={"email": "audit-b@test.com", "password": "Abc12345"},
    )
    hb2 = {"Authorization": f"Bearer {login_b.json()['access_token']}"}
    r3 = client.get("/api/v1/audit/logs", headers=hb2)
    assert r3.status_code == 200
    b_items = r3.json()["items"]
    assert all(item["project_id"] != pid for item in b_items)
