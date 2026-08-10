"""容器专属：PostgreSQL RLS 纵深防护验证（跨租户直连 SQL 返回空）。"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


@pytest.mark.container
def test_rls_tenant_isolation():
    database_url = os.environ.get("DATABASE_URL", "")
    if "postgresql" not in database_url:
        pytest.skip("仅 PostgreSQL 环境执行")
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg2") if "+asyncpg" in database_url else database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname FROM pg_class "
                "WHERE relname IN ('app_user','project','task') AND relrowsecurity = true "
                "ORDER BY relname"
            )
        ).fetchall()
        assert {r[0] for r in rows} == {"app_user", "project", "task"}, "RLS 未启用"

        conn.execute(text("SET LOCAL app.enterprise_id = '1'"))
        conn.execute(text("INSERT INTO project (enterprise_id, name, status) VALUES (1, 'RLS测试', 1)"))
        conn.execute(text("SET LOCAL app.enterprise_id = '1'"))
        own = conn.execute(text("SELECT count(*) FROM project WHERE name='RLS测试'")).scalar()
        assert own == 1
        conn.execute(text("SET LOCAL app.enterprise_id = '2'"))
        visible = conn.execute(text("SELECT count(*) FROM project WHERE name='RLS测试'")).scalar()
        assert visible == 0, "跨租户数据被 RLS 泄露"
    engine.dispose()
