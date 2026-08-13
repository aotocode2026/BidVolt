#!/usr/bin/env bash
# 每日备份（Issue #3 补全）：pg_dump（保留 30 天，校验可读） + /data/appdata 业务文件 tar + WAL 清理
# 用法：bidvolt-backup [标签]      # 标签可选（如 pre-upgrade），默认 daily
set -euo pipefail

TAG="${1:-daily}"
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
[ -n "$PGBIN" ] || { echo "未找到 PostgreSQL 安装目录" >&2; exit 1; }

TS=$(date +%F_%H%M)
DB_OUT=/data/backups/bidvolt_${TS}_${TAG}.dump
FILE_OUT=/data/backups/bidvolt_appdata_${TS}_${TAG}.tar.gz
LOGF=/var/log/bidvolt/backup.log

# 走 unix socket（initdb --auth-local=trust），避免 postgres 超级用户密码暴露
"$PGBIN/pg_dump" -h /var/run/postgresql -U postgres -d bidvolt -Fc -f "$DB_OUT"

# 校验 dump 非空且可由 pg_restore 读取（备份坏了必须当场报错，而不是恢复时才发现）
[ -s "$DB_OUT" ] || { echo "[backup] ERROR: dump 为空：$DB_OUT" >> "$LOGF"; exit 1; }
"$PGBIN/pg_restore" --list "$DB_OUT" >/dev/null 2>&1 || { echo "[backup] ERROR: dump 校验失败：$DB_OUT" >> "$LOGF"; exit 1; }

# 业务文件（上传/成果）一并备份：恢复 = pg_restore + 解包 appdata
if [ -d /data/appdata ]; then
  tar -czf "$FILE_OUT" -C /data appdata
  [ -s "$FILE_OUT" ] || { echo "[backup] ERROR: appdata 备份为空" >> "$LOGF"; exit 1; }
fi

# 保留 30 天；WAL 归档保留 7 天（基于每日 pg_dump 的恢复不需要更早的 WAL）
find /data/backups -maxdepth 1 -name 'bidvolt_*.dump' -mtime +30 -delete
find /data/backups -maxdepth 1 -name 'bidvolt_appdata_*.tar.gz' -mtime +30 -delete
find /data/backups/wal -type f -mtime +7 -delete

echo "[backup] $(date -Is) tag=$TAG db=$DB_OUT files=$FILE_OUT" >> "$LOGF"

# 可选：rclone 转存（配置 remote 后取消注释）
# rclone copy "$DB_OUT" remote:bidvolt-backups/
# rclone copy "$FILE_OUT" remote:bidvolt-backups/
