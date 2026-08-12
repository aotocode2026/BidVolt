#!/usr/bin/env bash
# 服务器容器内直接安装（容器是现成提供的，不走 Dockerfile）
# 用法：仓库已复制到容器后执行
#   cd /opt/bidvolt && bash deploy/install.sh
# 假设：Debian/Ubuntu 系发行版；包名以实际发行版为准，装不上时按报错调整。
set -euo pipefail

# 路径全部配置化：代码/venv 以 /data/bidvolt 为唯一真源（容器重建后仍在），
# 不依赖符号链接；旧部署若在 /opt/bidvolt 且 /data/bidvolt 不存在会自动迁移。
REPO="${REPO:-/data/bidvolt}"
HERMES_HOME="${HERMES_HOME:-/data/hermes}"
SYSTEM_PKGS="python3 python3-pip python3-venv postgresql postgresql-contrib supervisor cron clamav-daemon libreoffice-writer unzip p7zip-full rclone curl"

echo "==> 1/5 安装系统包（PostgreSQL/ClamAV/LibreOffice/supervisor 等）"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $SYSTEM_PKGS
# clamd 前台运行：supervisor 才能正确守护（clamd 默认 daemonize 会导致 supervisor 误判重启 + 孤儿进程）
sed -i 's/^#\?Foreground .*/Foreground true/' /etc/clamav/clamd.conf

echo "==> 2/5 代码位置与 Python venv（路径 = $REPO）"
if [ -d /opt/bidvolt ] && [ ! -d "$REPO" ]; then
  echo "    检测到旧路径 /opt/bidvolt，迁移到 $REPO"
  mv /opt/bidvolt "$REPO"
fi
if [ ! -d "$REPO" ]; then
  echo "    未找到代码目录 $REPO；请先把仓库复制到容器（scp -r . $REPO）后重跑" >&2
  exit 1
fi
cd "$REPO"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
fi
# 幂等补齐运行/测试依赖（venv 已存在时跳过创建，但保证依赖最新）
.venv/bin/pip install -r requirements.txt
[ -f requirements-dev.txt ] && .venv/bin/pip install -r requirements-dev.txt

echo "==> 3/5 准备 /data 持久卷内子目录（卷由平台外挂，脚本不建卷）"
mkdir -p /data/{pgdata,appdata,backups,backups/wal,logs/bidvolt} /etc/bidvolt /var/log/bidvolt

echo "==> 4/5 生成 supervisor 配置与运维脚本（路径来自 REPO/HERMES_HOME 变量）"
sed -e "s|__REPO__|$REPO|g" -e "s|__HERMES_HOME__|$HERMES_HOME|g" \
    deploy/supervisord.conf > /etc/supervisor/conf.d/bidvolt.conf
cp deploy/bidvolt-init.sh /usr/local/bin/bidvolt-init
cp deploy/bidvolt-postgres.sh /usr/local/bin/bidvolt-postgres
cp deploy/backup.sh /usr/local/bin/bidvolt-backup
cp deploy/healthcheck.sh /usr/local/bin/bidvolt-healthcheck
chmod +x /usr/local/bin/bidvolt-init /usr/local/bin/bidvolt-postgres \
         /usr/local/bin/bidvolt-backup /usr/local/bin/bidvolt-healthcheck

echo "==> 5/5 初始化（PG 集群/建库/迁移）并启动 supervisor"
echo "    首次安装请先确认 $REPO/.env 已就位（含 APP_DB_PASSWORD、DATABASE_URL 等）"
REPO="$REPO" /usr/local/bin/bidvolt-init

echo "==> 6/6 安装 Hermes Agent（数据/venv 位于 $HERMES_HOME；SKIP_HERMES=1 可跳过）"
if [ "${SKIP_HERMES:-0}" != "1" ]; then
  REPO="$REPO" HERMES_HOME="$HERMES_HOME" bash "$REPO/deploy/install-hermes.sh" \
    || echo "    Hermes 安装未完成，可稍后重跑: REPO=$REPO HERMES_HOME=$HERMES_HOME bash deploy/install-hermes.sh"
fi
