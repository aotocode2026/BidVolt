#!/usr/bin/env bash
# 容器内执行数据库迁移：加载 .env 后运行 alembic upgrade head
set -euo pipefail
REPO="${REPO:-/data/bidvolt}"
cd "$REPO"
set -a
. ./.env
set +a
exec .venv/bin/alembic "$@"
