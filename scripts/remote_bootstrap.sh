#!/usr/bin/env bash
# 容器引导辅助：加载 .env → 对齐数据库密码（安全通道）→ 后台启动初始化
set -euo pipefail

REPO="${REPO:-/data/bidvolt}"

set -a
. "$REPO/.env"
set +a

if [ -n "${APP_DB_PASSWORD:-}" ]; then
  # 口令经 0600 临时 SQL 文件 + psql 变量传入，不进入进程 argv（Issue #3）
  PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
  SQL_FILE=$(mktemp /tmp/bidvolt-role.XXXXXX.sql)
  chmod 600 "$SQL_FILE"
  printf "\\set pw '%s'\nALTER ROLE bidvolt LOGIN PASSWORD :'pw';\n" \
    "${APP_DB_PASSWORD//\'/\'\'}" > "$SQL_FILE"
  su postgres -c "$PGBIN/psql -v ON_ERROR_STOP=1 -f $SQL_FILE"
  rm -f "$SQL_FILE"
fi

rm -f /var/log/bidvolt_init.log
setsid bash /usr/local/bin/bidvolt-init > /var/log/bidvolt_init.log 2>&1 < /dev/null &
disown
echo "launched"
