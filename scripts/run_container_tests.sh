#!/usr/bin/env bash
# 容器内跑测试：加载 .env（含 DATABASE_URL=PG）后透传 pytest 参数
set -euo pipefail
REPO="${REPO:-/data/bidvolt}"
cd "$REPO"
set -a
. ./.env
set +a
echo "RUN URL=$(echo "${DATABASE_URL:-EMPTY}" | sed -E 's#://[^:]+:[^@]+@#://***:***@#')" >&2
exec .venv/bin/python -m pytest "$@"
