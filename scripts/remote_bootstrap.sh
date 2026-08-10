#!/usr/bin/env bash
# 容器引导辅助：加载 .env → 对齐数据库密码 → 后台启动初始化
set -euo pipefail

set -a
. /opt/bidvolt/.env
set +a

if [ -n "${APP_DB_PASSWORD:-}" ]; then
  su postgres -c "psql -v ON_ERROR_STOP=1 -c \"ALTER ROLE bidvolt LOGIN PASSWORD '${APP_DB_PASSWORD}';\""
fi

rm -f /var/log/bidvolt_init.log
setsid bash /usr/local/bin/bidvolt-init > /var/log/bidvolt_init.log 2>&1 < /dev/null &
disown
echo "launched"
