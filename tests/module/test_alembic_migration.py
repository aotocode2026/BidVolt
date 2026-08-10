from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_upgrade_creates_all_tables(tmp_path):
    db = tmp_path / "mig.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db}")
    tables = set(inspect(engine).get_table_names())
    required = {
        "enterprise",
        "app_user",
        "refresh_token",
        "enterprise_permission",
        "project_edit_lock",
        "project",
        "tenant_quota",
        "audit_log",
        "file_object",
        "archive_job",
        "task",
    }
    assert required.issubset(tables), f"缺少表：{required - tables}"
    engine.dispose()
