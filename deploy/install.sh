#!/usr/bin/env bash
# 服务器容器内直接安装（容器是现成提供的，不走 Dockerfile）
# 用法：仓库已复制到容器 /opt/bidvolt 后执行
#   cd /opt/bidvolt && bash deploy/install.sh
# 假设：Debian/Ubuntu 系发行版；包名以实际发行版为准，装不上时按报错调整。
set -euo pipefail

REPO="${REPO:-/opt/bidvolt}"
SYSTEM_PKGS="python3 python3-pip python3-venv postgresql postgresql-contrib supervisor cron clamav-daemon libreoffice-writer unzip p7zip-full rclone curl"

echo "==> 1/5 安装系统包（PostgreSQL/ClamAV/LibreOffice/supervisor 等）"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $SYSTEM_PKGS

echo "==> 2/5 创建 Python venv 并安装 requirements"
cd "$REPO"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> 3/5 准备 /data 持久卷内子目录（卷由平台外挂，脚本不建卷）"
mkdir -p /data/{pgdata,appdata,backups,backups/wal} /etc/bidvolt /var/log/bidvolt

echo "==> 4/5 安装 supervisor 配置与运维脚本"
cp deploy/supervisord.conf /etc/supervisor/conf.d/bidvolt.conf
cp deploy/bidvolt-init.sh /usr/local/bin/bidvolt-init
cp deploy/bidvolt-postgres.sh /usr/local/bin/bidvolt-postgres
cp deploy/backup.sh /usr/local/bin/bidvolt-backup
cp deploy/healthcheck.sh /usr/local/bin/bidvolt-healthcheck
chmod +x /usr/local/bin/bidvolt-init /usr/local/bin/bidvolt-postgres \
         /usr/local/bin/bidvolt-backup /usr/local/bin/bidvolt-healthcheck

echo "==> 5/5 初始化（PG 集群/建库/迁移）并启动 supervisor"
echo "    首次安装请先确认 /opt/bidvolt/.env 已就位（含 APP_DB_PASSWORD、DATABASE_URL 等）"
/usr/local/bin/bidvolt-init
