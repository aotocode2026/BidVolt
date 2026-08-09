#!/usr/bin/env bash
# 每日 pg_dump（保留 30 天），可选 rclone 转存外部对象存储
set -euo pipefail

TS=$(date +%F_%H%M)
OUT=/var/lib/bidvolt/backups/bidvolt_${TS}.dump

# 走 unix socket（initdb --auth-local=trust），避免 postgres 超级用户密码暴露
/usr/lib/postgresql/16/bin/pg_dump -h /var/run/postgresql -U postgres -d bidvolt -Fc -f "$OUT"
find /var/lib/bidvolt/backups -maxdepth 1 -name 'bidvolt_*.dump' -mtime +30 -delete

# 可选：rclone 转存（配置 remote 后取消注释）
# rclone copy "$OUT" remote:bidvolt-backups/
