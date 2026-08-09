#!/usr/bin/env bash
# 每日 pg_dump（保留 30 天），可选 rclone 转存外部对象存储
set -euo pipefail

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
[ -n "$PGBIN" ] || { echo "未找到 PostgreSQL 安装目录" >&2; exit 1; }

TS=$(date +%F_%H%M)
OUT=/data/backups/bidvolt_${TS}.dump

# 走 unix socket（initdb --auth-local=trust），避免 postgres 超级用户密码暴露
"$PGBIN/pg_dump" -h /var/run/postgresql -U postgres -d bidvolt -Fc -f "$OUT"
find /data/backups -maxdepth 1 -name 'bidvolt_*.dump' -mtime +30 -delete

# 可选：rclone 转存（配置 remote 后取消注释）
# rclone copy "$OUT" remote:bidvolt-backups/
