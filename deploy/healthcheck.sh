#!/usr/bin/env bash
# 容器健康检查（Issue #3 补全）：PG + API + alembic head + worker + ClamAV socket + 持久卷 + 磁盘 + 读写
set -uo pipefail

REPO="${REPO:-/data/bidvolt}"
fail=0
note() { echo "HEALTH WARN: $*" >&2; fail=1; }

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
[ -n "$PGBIN" ] && "$PGBIN/pg_isready" -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1 || note "PostgreSQL 未就绪"
curl -fsS http://127.0.0.1:8123/healthz >/dev/null 2>&1 || note "API /healthz 未就绪"

# alembic head：新代码配旧表结构（或反之）视为不健康
if [ -x "$REPO/.venv/bin/alembic" ] && [ -f "$REPO/alembic.ini" ]; then
  (cd "$REPO" && DATABASE_URL="${DATABASE_URL:-$(grep -E '^DATABASE_URL=' "$REPO/.env" 2>/dev/null | head -1 | cut -d= -f2-)}" \
    .venv/bin/alembic current 2>/dev/null | head -1 | cut -d' ' -f1) > /tmp/bidvolt-mig-current 2>/dev/null || true
  (cd "$REPO" && .venv/bin/alembic heads 2>/dev/null | head -1 | cut -d' ' -f1) > /tmp/bidvolt-mig-head 2>/dev/null || true
  if ! cmp -s /tmp/bidvolt-mig-current /tmp/bidvolt-mig-head; then
    note "alembic 未到 head（current=$(cat /tmp/bidvolt-mig-current 2>/dev/null) head=$(cat /tmp/bidvolt-mig-head 2>/dev/null)）"
  fi
fi

# worker：进程存活（supervisor 管理；未运行=任务无人消费）
pgrep -f "python -m app.services.worker" >/dev/null 2>&1 || note "worker 未运行"

# ClamAV socket（VIRUS_SCAN_REQUIRED=1 时为硬依赖）
[ -S /var/run/clamav/clamd.ctl ] || note "clamd socket 不存在"

# 持久卷：/data 必须可写且为独立挂载（与根同设备可能落容器层，重建即丢）
[ -d /data ] && [ -w /data ] || note "/data 不存在或不可写"
ROOT_DEV=$(stat -c '%d' / 2>/dev/null || echo "0")
DATA_DEV=$(stat -c '%d' /data 2>/dev/null || echo "1")
[ "$DATA_DEV" != "$ROOT_DEV" ] || note "/data 与根文件系统同设备（可能非外挂持久卷）"
[ -d /data/pgdata ] && [ -d /data/appdata ] && [ -d /data/backups ] || note "/data 子目录缺失"

# 关键读写：appdata 可创建文件
touch /data/appdata/.healthcheck-write 2>/dev/null && rm -f /data/appdata/.healthcheck-write || note "/data/appdata 不可写"

# 磁盘阈值（可用 < 10% 告警）
df -P /data | awk 'NR==2 { if ($5+0 > 90) exit 1 }' || note "磁盘使用率超过 90%"

exit $fail
