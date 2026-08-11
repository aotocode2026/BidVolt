#!/usr/bin/env bash
# 容器诊断：worker 日志 / 任务状态 / 数据库编码
set -uo pipefail

echo "=== worker stderr ==="
tail -8 /var/log/supervisor/worker-stderr--* 2>/dev/null || echo "(no log)"

echo "=== task ==="
su postgres -c "/usr/lib/postgresql/14/bin/psql -d bidvolt -tAc \"SELECT id,status,task_type FROM task ORDER BY id DESC LIMIT 3;\""

echo "=== encoding ==="
set -a
. /opt/bidvolt/.env
set +a
/opt/bidvolt/.venv/bin/python /tmp/db_check.py
