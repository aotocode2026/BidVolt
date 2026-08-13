#!/usr/bin/env bash
# 容器诊断：worker 日志 / 任务状态 / 数据库编码
set -uo pipefail

REPO="${REPO:-/data/bidvolt}"

echo "=== worker stderr ==="
tail -8 /var/log/bidvolt/worker* 2>/dev/null || echo "(no log)"

echo "=== task ==="
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
su postgres -c "$PGBIN/psql -d bidvolt -tAc \"SELECT id,status,task_type FROM task ORDER BY id DESC LIMIT 3;\""

echo "=== encoding ==="
set -a
. "$REPO/.env"
set +a
"$REPO/.venv/bin/python" /tmp/db_check.py 2>/dev/null || echo "(db_check.py 未就位，跳过)"
