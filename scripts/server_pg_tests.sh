#!/bin/bash
# PostgreSQL 对等测试（Issue #13 复盘缺口④）：在服务器真实 PG 上跑关键回归用例。
# SQLite 对"多行标量子查询"等 PG 严格性错误静默放行，本地绿≠生产绿。
# 使用独立测试库 bidvolt_pytest（每次重建），不触碰生产库 bidvolt。
set -euo pipefail
cd /data/bidvolt

PGPASSWORD=$(grep -E '^DATABASE_URL=' .env | head -1 | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
export PGPASSWORD
PSQL="psql -U bidvolt -h 127.0.0.1"

echo "== 重建测试库 bidvolt_pytest =="
su postgres -c "psql -c 'DROP DATABASE IF EXISTS bidvolt_pytest;'" || true
su postgres -c "psql -c 'CREATE DATABASE bidvolt_pytest OWNER bidvolt;'"

export BIDVOLT_TEST_DATABASE_URL="postgresql+asyncpg://bidvolt:${PGPASSWORD}@127.0.0.1:5432/bidvolt_pytest"
# 保留 .env 的 PG DATABASE_URL：app.config 生产校验拒绝 SQLite 主库；
# 测试会话由 tests/conftest.py 的 client 夹具覆盖指向 bidvolt_pytest（app.db 引擎惰性不落库）。
export BIDVOLT_KEEP_DATABASE_URL=1

echo "== PG 对等测试：项目列表(基数约束)/任务SSE/解析/评审 =="
.venv/bin/python -m pytest \
  tests/module/test_project_api.py \
  tests/module/test_tasks_api.py \
  tests/module/test_parser.py \
  tests/module/test_review_api.py \
  -q --tb=short

echo "== 清理测试库 =="
su postgres -c "psql -c 'DROP DATABASE IF EXISTS bidvolt_pytest;'"

echo "== 完成 =="
