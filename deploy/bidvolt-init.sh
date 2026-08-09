#!/usr/bin/env bash
# 初始化（容器内直接运行）：空数据卷时初始化 PG 集群 → 建库/用户 → alembic 迁移 → 启动 supervisor
set -euo pipefail

PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
if [ -z "$PGBIN" ]; then
  echo "未找到 PostgreSQL 安装目录（/usr/lib/postgresql/*/bin）" >&2
  exit 1
fi
PGDATA=/var/lib/bidvolt/pgdata
DATA=/var/lib/bidvolt/data
BACKUPS=/var/lib/bidvolt/backups
PGUSER=postgres
APP_DB=bidvolt
APP_USER=bidvolt

mkdir -p "$PGDATA" "$DATA" "$BACKUPS" /var/lib/bidvolt/backups/wal /var/log/bidvolt /etc/bidvolt
chown -R postgres:postgres "$PGDATA" /var/lib/bidvolt/backups

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[init] initdb: $PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA --auth-local=trust --auth-host=scram-sha-256 -U $PGUSER"
  cat > /etc/bidvolt/postgresql.conf <<EOF
data_directory = '$PGDATA'
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = '/var/run/postgresql'
archive_mode = on
archive_command = 'cp %p /var/lib/bidvolt/backups/wal/%f'
max_wal_size = 1GB
EOF
  echo "[init] bootstrap PG (create user/db)"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c config_file=/etc/bidvolt/postgresql.conf' -w start"
  su postgres -c "$PGBIN/psql -v ON_ERROR_STOP=1 -c \"CREATE ROLE $APP_USER LOGIN PASSWORD '${APP_DB_PASSWORD:?APP_DB_PASSWORD 未设置}\";\""
  su postgres -c "$PGBIN/createdb -O $APP_USER $APP_DB"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -m fast -w stop"
  echo "[init] bootstrap done"
fi

# 备份 cron（每日 02:00，保留 30 天，见 backup.sh）
echo "0 2 * * * /usr/local/bin/bidvolt-backup >> /var/log/bidvolt/backup.log 2>&1" > /etc/cron.d/bidvolt-backup

echo "[init] alembic migrations"
export DATABASE_URL="${DATABASE_URL:-postgresql://${APP_USER}:${APP_DB_PASSWORD}@127.0.0.1:5432/${APP_DB}}"
/opt/venv/bin/alembic upgrade head

echo "[init] starting supervisord"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
