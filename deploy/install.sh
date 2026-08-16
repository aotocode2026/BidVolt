#!/usr/bin/env bash
# 服务器容器内直接安装（容器是现成提供的，不走 Dockerfile）
# 用法：仓库已复制到容器后执行
#   cd /data/bidvolt && bash deploy/install.sh
# 假设：Debian/Ubuntu 系发行版；包名以实际发行版为准，装不上时按报错调整。
# 顺序：系统包 → venv/依赖 → /data 目录 → 配置 → Hermes（先于 supervisor 启动，避免
# [program:hermes] 缺目录重启风暴）→ 初始化 PG/迁移 → 启动 supervisor（前台常驻）。
set -euo pipefail

# 路径全部配置化：代码/venv 以 /data/bidvolt 为唯一真源（容器重建后仍在），
# 不依赖符号链接；旧部署若在 /opt/bidvolt 且 /data/bidvolt 不存在会自动迁移。
REPO="${REPO:-/data/bidvolt}"
HERMES_HOME="${HERMES_HOME:-/data/hermes}"
SYSTEM_PKGS="python3 python3-pip python3-venv postgresql postgresql-contrib supervisor cron clamav-daemon libreoffice-writer unzip p7zip-full rclone curl"

echo "==> 0/6 预检（Issue #3：配置 fail-fast）"
if [ ! -f "$REPO/.env" ]; then
  echo "    缺少 $REPO/.env：请先通过 Secret Manager/安全通道放入（含 APP_DB_PASSWORD、JWT_SECRET 等）" >&2
  exit 1
fi
MODE=$(stat -c '%a' "$REPO/.env" 2>/dev/null || echo "600")
[ "$MODE" = "600" ] || { echo "    收紧 .env 权限 -> 0600"; chmod 600 "$REPO/.env"; }

echo "==> 1/6 安装系统包（PostgreSQL/ClamAV/LibreOffice/supervisor 等）"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $SYSTEM_PKGS
# clamd 前台运行：supervisor 才能正确守护（clamd 默认 daemonize 会导致 supervisor 误判重启 + 孤儿进程）
sed -i 's/^#\?Foreground .*/Foreground true/' /etc/clamav/clamd.conf

echo "==> 2/6 代码位置与 Python venv（路径 = $REPO）"
if [ -d /opt/bidvolt ] && [ ! -d "$REPO" ]; then
  echo "    检测到旧路径 /opt/bidvolt，迁移到 $REPO"
  mv /opt/bidvolt "$REPO"
fi
if [ ! -d "$REPO" ]; then
  echo "    未找到代码目录 $REPO；请先把仓库制品复制到容器后重跑" >&2
  echo "    建议：git archive --format=tar <tag> | ssh <host> 'tar -x -C /data/bidvolt'，避免 scp -r 带入 .env/.git" >&2
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

echo "==> 3/6 准备 /data 持久卷内子目录（卷由平台外挂，脚本不建卷）"
mkdir -p /data/{pgdata,appdata,backups,backups/wal,logs/bidvolt} /etc/bidvolt /var/log/bidvolt

echo "==> 4/6 生成 supervisor 配置与运维脚本（路径来自 REPO/HERMES_HOME 变量）"
sed -e "s|__REPO__|$REPO|g" -e "s|__HERMES_HOME__|$HERMES_HOME|g" \
    deploy/supervisord.conf > /etc/supervisor/conf.d/bidvolt.conf
cp deploy/bidvolt-init.sh /usr/local/bin/bidvolt-init
cp deploy/bidvolt-boot.sh /usr/local/bin/bidvolt-boot
cp deploy/bidvolt-postgres.sh /usr/local/bin/bidvolt-postgres
cp deploy/backup.sh /usr/local/bin/bidvolt-backup
cp deploy/healthcheck.sh /usr/local/bin/bidvolt-healthcheck
cp deploy/upgrade.sh /usr/local/bin/bidvolt-upgrade
chmod +x /usr/local/bin/bidvolt-init /usr/local/bin/bidvolt-boot /usr/local/bin/bidvolt-postgres \
         /usr/local/bin/bidvolt-backup /usr/local/bin/bidvolt-healthcheck \
         /usr/local/bin/bidvolt-upgrade

echo "==> 4.5/6 容器自启动自举（平台重启容器时无需 SSH 登录自动拉起服务）"
# 背景：容器入口 CMD 为 `/usr/sbin/sshd -D`（平台提供镜像，无入口脚本/systemd 可改）。
# 方案一（主）：sshd 包装器——容器重启后 Docker 先运行包装脚本：幂等拉起 bidvolt-boot 后 exec 真实 sshd。
# 方案二（兜底）：/etc/ssh/sshrc——SSH 登录时触发同一 bidvolt-boot（幂等，双保险）。
SSHD_REAL=/usr/sbin/sshd.real
if [ -x "$SSHD_REAL" ]; then
  echo "    sshd 包装器已存在，跳过"
else
  if [ -x /usr/sbin/sshd ] && cp /usr/sbin/sshd "$SSHD_REAL" && chmod 0755 "$SSHD_REAL"; then
    cat > /usr/sbin/sshd <<'WRAP_EOF'
#!/bin/bash
# BidVolt 自举包装：容器重启后由 Docker CMD 以 pid1 运行本脚本，
# 先幂等拉起服务（bidvolt-boot），再 exec 真实 sshd，保持 pid1 为 sshd。
if [ ! -x /usr/sbin/sshd.real ]; then
  echo "$(date -Is) FATAL: /usr/sbin/sshd.real 缺失，sshd 无法启动" >&2
  exit 1
fi
nohup /usr/local/bin/bidvolt-boot >/dev/null 2>&1 </dev/null &
exec /usr/sbin/sshd.real "$@"
WRAP_EOF
    chmod 0755 /usr/sbin/sshd \
      && echo "    sshd 包装器已安装（原二进制保留为 $SSHD_REAL）" \
      || { rm -f /usr/sbin/sshd; mv "$SSHD_REAL" /usr/sbin/sshd; echo "    WARN: sshd 包装器写入失败，已回退原状" >&2; }
  else
    echo "    WARN: 未找到/无法备份 /usr/sbin/sshd，跳过包装器（仍依赖 SSH 登录兜底）" >&2
  fi
fi
cat > /etc/ssh/sshrc <<'SSHRC_EOF'
#!/bin/bash
# BidVolt 兜底：SSH 登录时若 supervisord 未运行则自动拉起（与 sshd 包装器同一入口，幂等）。
if [ -x /usr/local/bin/bidvolt-boot ]; then
  nohup /usr/local/bin/bidvolt-boot >/dev/null 2>&1 </dev/null &
fi
SSHRC_EOF
chmod 0755 /etc/ssh/sshrc
echo "    已安装 /etc/ssh/sshrc 兜底（SSH 登录触发，幂等）"

echo "==> 5/6 安装 Hermes Agent（先于 supervisor 启动；SKIP_HERMES=1 可跳过）"
if [ "${SKIP_HERMES:-0}" != "1" ]; then
  REPO="$REPO" HERMES_HOME="$HERMES_HOME" bash "$REPO/deploy/install-hermes.sh" \
    || echo "    Hermes 安装未完成，可稍后重跑: REPO=$REPO HERMES_HOME=$HERMES_HOME bash deploy/install-hermes.sh"
fi

echo "==> 6/6 初始化（PG 集群/建库/迁移）并启动 supervisor（前台常驻，本脚本到此结束）"
REPO="$REPO" /usr/local/bin/bidvolt-init
