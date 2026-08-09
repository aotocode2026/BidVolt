#!/usr/bin/env bash
# 以检测到的 PG 版本以前台方式启动 postgres（供 supervisor 管理）
set -euo pipefail

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
if [ -z "$PGBIN" ]; then
  echo "未找到 PostgreSQL 安装目录（/usr/lib/postgresql/*/bin）" >&2
  exit 1
fi

exec "$PGBIN/postgres" -D /data/pgdata -c config_file=/etc/bidvolt/postgresql.conf
