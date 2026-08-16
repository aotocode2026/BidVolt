#!/usr/bin/env bash
# 以检测到的 PG 版本以前台方式启动 postgres（供 supervisor 管理）
set -euo pipefail

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
if [ -z "$PGBIN" ]; then
  echo "未找到 PostgreSQL 安装目录（/usr/lib/postgresql/*/bin）" >&2
  exit 1
fi

# 容器重启后 /run 为全新 tmpfs：socket 目录必须重建，否则 postgres 拒绝启动
mkdir -p /var/run/postgresql
chown postgres:postgres /var/run/postgresql

exec "$PGBIN/postgres" -D /data/pgdata -c config_file=/etc/bidvolt/postgresql.conf
