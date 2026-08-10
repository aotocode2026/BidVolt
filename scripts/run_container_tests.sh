#!/usr/bin/env bash
# 容器内跑测试：加载 .env（含 DATABASE_URL=PG）后透传 pytest 参数
set -euo pipefail
cd /opt/bidvolt
set -a
. ./.env
set +a
echo "RUN URL=${DATABASE_URL:-EMPTY}" >&2
exec .venv/bin/python -m pytest "$@"
