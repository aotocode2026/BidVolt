#!/usr/bin/env bash
# 环境诊断（Issue #3）：绝不输出完整 DATABASE_URL/密钥，只输出脱敏后的状态。
set -uo pipefail

REPO="${REPO:-/data/bidvolt}"
[ -f "$REPO/.env" ] && . "$REPO/.env" || echo "WARN: $REPO/.env 不存在"

mask_url() {
  # postgresql://user:pass@host:port/db -> postgresql://user:***@host:port/db
  local url="${1:-}"
  if [[ "$url" == *"@"* ]]; then
    local head="${url%%@*}"  tail="${url#*@}"
    if [[ "$head" == *":"* ]]; then
      echo "${head%%:*}:***@${tail}"
    else
      echo "${head}:***@${tail}"
    fi
  else
    echo "(未设置或无口令段)"
  fi
}

echo "DATABASE_URL=$(mask_url "${DATABASE_URL:-}")"
echo "APP_DB_PASSWORD_SET=$([ -n "${APP_DB_PASSWORD:-}" ] && echo yes || echo no)"
echo "JWT_SECRET_LEN=${#JWT_SECRET:-0}"
echo "BIDVOLT_INTERNAL_TOKEN_SET=$([ -n "${BIDVOLT_INTERNAL_TOKEN:-}" ] && echo yes || echo no)"
echo "STORAGE_ROOT=${STORAGE_ROOT:-/data/appdata}"
echo "BACKUP_ROOT=${BACKUP_ROOT:-/data/backups}"
