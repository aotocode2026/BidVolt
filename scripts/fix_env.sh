#!/usr/bin/env bash
# 将 /opt/bidvolt/.env 转为 Unix 格式（去 BOM、CRLF→LF）
set -euo pipefail
sed -i '1s/^\xEF\xBB\xBF//' /opt/bidvolt/.env
sed -i 's/\r$//' /opt/bidvolt/.env
echo "env fixed"
