#!/usr/bin/env bash
# 初始化（容器内直接运行）：空数据卷时初始化 PG 集群 → 幂等建库/用户 → alembic 迁移 → 启动 supervisor
# 安全（Issue #3）：数据库口令不进入进程 argv；.env 强制 0600；弱默认密钥 fail-fast；/data 持久卷校验。
set -euo pipefail

# 代码/venv 路径（/data/bidvolt 为唯一真源，不依赖符号链接）
REPO="${REPO:-/data/bidvolt}"

# 固定工作目录（alembic 读取 alembic.ini 依赖 cwd）
cd "$REPO" 2>/dev/null || cd "$(dirname "${BASH_SOURCE[0]}")/.." || true

# 加载 .env（容器直装场景；生产环境由 Secret Manager 注入同名变量）
set -a
if [ -f "$REPO/.env" ]; then
  # shellcheck disable=SC1091
  . "$REPO/.env"
fi
set +a

# ---- fail-fast 预检（Issue #3）----
if [ -z "${APP_DB_PASSWORD:-}" ]; then
  echo "FATAL: APP_DB_PASSWORD 未设置" >&2
  exit 1
fi
if [ "${#APP_DB_PASSWORD}" -lt 8 ]; then
  echo "FATAL: APP_DB_PASSWORD 长度不足 8 位" >&2
  exit 1
fi
case "$APP_DB_PASSWORD" in
  *change_me*|*changeme*|*bidvolt*|*password*) echo "FATAL: APP_DB_PASSWORD 使用了弱口令/占位值" >&2; exit 1 ;;
esac
if [ -z "${JWT_SECRET:-}" ] || [ "${#JWT_SECRET}" -lt 16 ] \
   || [[ "$JWT_SECRET" == *"change"* ]] || [[ "$JWT_SECRET" == *"dev-only"* ]]; then
  echo "FATAL: JWT_SECRET 未设置/长度不足 16 位/仍为占位值" >&2
  exit 1
fi
if [ "${#JWT_SECRET}" -lt 32 ]; then
  echo "WARN: JWT_SECRET 长度不足 32 位（生产推荐 ≥32，请尽快轮换）" >&2
fi
if [ -n "${BIDVOLT_INTERNAL_TOKEN:-}" ] && [ "${#BIDVOLT_INTERNAL_TOKEN}" -lt 32 ]; then
  echo "WARN: BIDVOLT_INTERNAL_TOKEN 长度不足 32 位（生产推荐 ≥32，请尽快轮换）" >&2
fi
# .env 权限收紧（不依赖文档约定，脚本强制校验并修复）
if [ -f "$REPO/.env" ]; then
  MODE=$(stat -c '%a' "$REPO/.env" 2>/dev/null || echo "600")
  if [ "$MODE" != "600" ]; then
    echo "[init] 收紧 .env 权限 -> 0600"
    chmod 600 "$REPO/.env"
  fi
fi
# /data 必须为外挂持久卷（与根文件系统不同设备）；REQUIRE_PERSISTENT_DATA=1 时强制
ROOT_DEV=$(stat -c '%d' / 2>/dev/null || echo "0")
DATA_DEV=$(stat -c '%d' /data 2>/dev/null || echo "1")
if [ "$DATA_DEV" = "$ROOT_DEV" ]; then
  echo "WARN: /data 与根文件系统同一设备，可能不是外挂持久卷（容器重建会丢数据）" >&2
  if [ "${REQUIRE_PERSISTENT_DATA:-0}" = "1" ]; then
    echo "FATAL: REQUIRE_PERSISTENT_DATA=1 且 /data 非独立挂载" >&2
    exit 1
  fi
fi

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
if [ -z "$PGBIN" ]; then
  echo "未找到 PostgreSQL 安装目录（/usr/lib/postgresql/*/bin）" >&2
  exit 1
fi
PGDATA=/data/pgdata
DATA=/data/appdata
BACKUPS=/data/backups
LOGS=/data/logs/bidvolt
PGUSER=postgres
APP_DB="${APP_DB:-bidvolt}"
APP_USER="${APP_USER:-bidvolt}"

mkdir -p "$PGDATA" "$DATA" "$BACKUPS" "$BACKUPS/wal" "$LOGS" /var/log/bidvolt /etc/bidvolt

# 属主修复（条件化）：/data 为 DrvFS(9p) 挂载时 chown -R 极慢（每次启动数分钟），
# 先用 find 快速探测浅层属主漂移，全部正确则跳过；发现漂移才执行全量 chown。
_fix_owner_fast() {
  local dir="$1" owner="$2" dirty=""
  dirty=$(find "$dir" -maxdepth 2 \( ! -user "$owner" -o ! -group "$owner" \) -print -quit 2>/dev/null)
  if [ -n "$dirty" ]; then
    echo "[init] chown -R $owner:$owner $dir（检测到属主漂移：$dirty）"
    chown -R "$owner:$owner" "$dir"
  else
    echo "[init] $dir 属主已正确，跳过 chown"
  fi
}
_fix_owner_fast "$PGDATA" postgres
_fix_owner_fast "$BACKUPS" postgres

# 日志落盘持久卷：/var/log/bidvolt -> /data/logs/bidvolt（容器重建不丢日志）
if [ ! -L /var/log/bidvolt ]; then
  if [ -d /var/log/bidvolt ]; then
    mv /var/log/bidvolt/supervisord.log "$LOGS"/supervisord.log 2>/dev/null || true
    rmdir /var/log/bidvolt 2>/dev/null || true
  fi
  ln -s "$LOGS" /var/log/bidvolt
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[init] initdb: $PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA --auth-local=trust --auth-host=scram-sha-256 -U $PGUSER --encoding=UTF8 --locale=C.UTF-8"
fi

# 配置落点（幂等）：容器销毁重建后可写层重置、/etc/bidvolt 丢失；
# 数据卷存在（跳过 initdb）时同样必须重建配置文件，否则 PG 无法启动。
if [ ! -f /etc/bidvolt/postgresql.conf ]; then
  echo "[init] 生成 /etc/bidvolt/postgresql.conf"
  cat > /etc/bidvolt/postgresql.conf <<EOF
data_directory = '$PGDATA'
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = '/var/run/postgresql'
archive_mode = on
archive_command = 'cp %p /data/backups/wal/%f'
max_wal_size = 1GB
EOF
  chown postgres:postgres /etc/bidvolt/postgresql.conf
fi

# socket 目录：容器重启后 /run 为全新 tmpfs，必须重建（PG 启动依赖）
mkdir -p /var/run/postgresql
chown postgres:postgres /var/run/postgresql

# 幂等 bootstrap：PG 未运行时启动 → 建角色/库（如缺失）→ 恢复原状态
if "$PGBIN/pg_isready" -h /var/run/postgresql -q; then
  PG_WAS_RUNNING=true
else
  PG_WAS_RUNNING=false
  echo "[init] start PG for bootstrap"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c config_file=/etc/bidvolt/postgresql.conf' -w start"
fi

# 建角色：口令经 0600 临时 SQL 文件 + psql 变量传入，不进入进程 argv、不暴露在 shell 历史
_sql_with_password() {
  # $1 = SQL 模板（含 :'pw' 占位）；$2 = 口令
  local sql_tpl="$1" pwd_val="$2" sql_file
  sql_file=$(mktemp /tmp/bidvolt-role.XXXXXX.sql)
  chmod 600 "$sql_file"
  # 转义单引号后写入 \set 变量，psql 的 :'pw' 引用保证任意特殊字符安全
  printf "\\set pw '%s'\n%s\n" "${pwd_val//\'/\'\'}" "$sql_tpl" > "$sql_file"
  su postgres -c "$PGBIN/psql -v ON_ERROR_STOP=1 -f $sql_file"
  rm -f "$sql_file"
}

ROLE_EXISTS=$(su postgres -c "$PGBIN/psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'\"" | tr -d ' ')
if [ "$ROLE_EXISTS" != "1" ]; then
  echo "[init] create role $APP_USER"
  _sql_with_password "CREATE ROLE $APP_USER LOGIN PASSWORD :'pw';" "$APP_DB_PASSWORD"
else
  # 角色已存在：同步口令（幂等；口令更新同样走安全通道）
  echo "[init] sync role password $APP_USER"
  _sql_with_password "ALTER ROLE $APP_USER LOGIN PASSWORD :'pw';" "$APP_DB_PASSWORD"
fi
DB_EXISTS=$(su postgres -c "$PGBIN/psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$APP_DB'\"" | tr -d ' ')
if [ "$DB_EXISTS" != "1" ]; then
  echo "[init] create database $APP_DB"
  su postgres -c "$PGBIN/createdb -O $APP_USER $APP_DB"
fi

# 备份 cron（每日 02:00，保留 30 天，见 backup.sh）
# 注意：/etc/cron.d 条目必须带用户字段（第 6 列 root），否则 cron 静默忽略
echo "0 2 * * * root /usr/local/bin/bidvolt-backup >> /var/log/bidvolt/backup.log 2>&1" > /etc/cron.d/bidvolt-backup
chmod 0644 /etc/cron.d/bidvolt-backup

# ClamAV 运行环境：socket 目录
mkdir -p /var/run/clamav
chown -R clamav:clamav /var/run/clamav 2>/dev/null || true

echo "[init] alembic migrations"
export DATABASE_URL="${DATABASE_URL:-postgresql://${APP_USER}:${APP_DB_PASSWORD}@127.0.0.1:5432/${APP_DB}}"
"$REPO/.venv/bin/alembic" upgrade head

if [ "$PG_WAS_RUNNING" = false ]; then
  echo "[init] stop PG after migration"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -m fast -w stop"
fi

echo "[init] starting supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
