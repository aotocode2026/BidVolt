#!/usr/bin/env bash
# 初始化（容器内直接运行）：空数据卷时初始化 PG 集群 → 幂等建库/用户 → alembic 迁移 → 启动 supervisor
set -euo pipefail

# 固定工作目录（alembic 读取 alembic.ini 依赖 cwd）
cd /opt/bidvolt 2>/dev/null || cd "$(dirname "${BASH_SOURCE[0]}")/.." || true

# 加载 .env（容器直装场景；生产环境由 Secret Manager 注入同名变量）
set -a
if [ -f /opt/bidvolt/.env ]; then
  # shellcheck disable=SC1091
  . /opt/bidvolt/.env
fi
set +a

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
if [ -z "$PGBIN" ]; then
  echo "未找到 PostgreSQL 安装目录（/usr/lib/postgresql/*/bin）" >&2
  exit 1
fi
PGDATA=/data/pgdata
DATA=/data/appdata
BACKUPS=/data/backups
PGUSER=postgres
APP_DB="${APP_DB:-bidvolt}"
APP_USER="${APP_USER:-bidvolt}"

mkdir -p "$PGDATA" "$DATA" "$BACKUPS" "$BACKUPS/wal" /var/log/bidvolt /etc/bidvolt
chown -R postgres:postgres "$PGDATA" "$BACKUPS"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[init] initdb: $PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA --auth-local=trust --auth-host=scram-sha-256 -U $PGUSER --encoding=UTF8 --locale=C.UTF-8"
  cat > /etc/bidvolt/postgresql.conf <<EOF
data_directory = '$PGDATA'
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = '/var/run/postgresql'
archive_mode = on
archive_command = 'cp %p /data/backups/wal/%f'
max_wal_size = 1GB
EOF
fi

# 幂等 bootstrap：PG 未运行时启动 → 建角色/库（如缺失）→ 恢复原状态
if "$PGBIN/pg_isready" -h /var/run/postgresql -q; then
  PG_WAS_RUNNING=true
else
  PG_WAS_RUNNING=false
  echo "[init] start PG for bootstrap"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c config_file=/etc/bidvolt/postgresql.conf' -w start"
fi

ROLE_EXISTS=$(su postgres -c "$PGBIN/psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'\"" | tr -d ' ')
if [ "$ROLE_EXISTS" != "1" ]; then
  echo "[init] create role $APP_USER"
  su postgres -c "$PGBIN/psql -v ON_ERROR_STOP=1 -c \"CREATE ROLE $APP_USER LOGIN PASSWORD '${APP_DB_PASSWORD:?APP_DB_PASSWORD 未设置}'\";"
fi
DB_EXISTS=$(su postgres -c "$PGBIN/psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$APP_DB'\"" | tr -d ' ')
if [ "$DB_EXISTS" != "1" ]; then
  echo "[init] create database $APP_DB"
  su postgres -c "$PGBIN/createdb -O $APP_USER $APP_DB"
fi

# 备份 cron（每日 02:00，保留 30 天，见 backup.sh）
echo "0 2 * * * /usr/local/bin/bidvolt-backup >> /var/log/bidvolt/backup.log 2>&1" > /etc/cron.d/bidvolt-backup

echo "[init] alembic migrations"
export DATABASE_URL="${DATABASE_URL:-postgresql://${APP_USER}:${APP_DB_PASSWORD}@127.0.0.1:5432/${APP_DB}}"
/opt/bidvolt/.venv/bin/alembic upgrade head

if [ "$PG_WAS_RUNNING" = false ]; then
  echo "[init] stop PG after migration"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -m fast -w stop"
fi

echo "[init] starting supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
