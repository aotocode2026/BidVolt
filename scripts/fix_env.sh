#!/usr/bin/env bash
# 将 $REPO/.env 转为 Unix 格式（去 BOM、CRLF→LF）
set -euo pipefail
REPO="${REPO:-/data/bidvolt}"
sed -i '1s/^\xEF\xBB\xBF//' "$REPO/.env"
sed -i 's/\r$//' "$REPO/.env"
chmod 600 "$REPO/.env"
echo "env fixed ($REPO/.env)"
