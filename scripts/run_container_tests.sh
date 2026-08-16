#!/usr/bin/env bash
# 容器内跑测试：加载 .env（含 DATABASE_URL=PG）后透传 pytest 参数
# 说明：用例按 dev 语义编写；生产 .env 的 BIDVOLT_ENV=production 会触发 fail-fast，
# 与部分用例的 dev 假设冲突（alembic SQLite 演练、弱默认值用例等），因此测试进程
# 固定 BIDVOLT_ENV=dev（生产 fail-fast 由 tests/unit/test_config_prod.py 显式覆盖）。
# 可用 BIDVOLT_TEST_ENV 覆盖该默认值。
# 注意：容器内 worker 运行时会与本套件中任务领取类用例（capability 终态）竞争同一
# 任务队列，跑全量前建议 supervisorctl stop worker，跑完再 start。
set -euo pipefail
REPO="${REPO:-/data/bidvolt}"
cd "$REPO"
set -a
. ./.env
set +a
export BIDVOLT_ENV="${BIDVOLT_TEST_ENV:-dev}"
echo "RUN URL=$(echo "${DATABASE_URL:-EMPTY}" | sed -E 's#://[^:]+:[^@]+@#://***:***@#')" >&2
exec .venv/bin/python -m pytest "$@"
