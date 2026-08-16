#!/usr/bin/env bash
# BidVolt 容器启动自举（幂等）：
# 由 sshd 包装器在容器启动时调用；也可由 /etc/ssh/sshrc 兜底调用。
# 若 supervisord 已在运行则直接退出；否则后台拉起 bidvolt-init（其内部幂等：
# 建库/迁移/启动 supervisor，PG 已运行时保持运行）。
set -u

exec >> /var/log/bidvolt/boot.log 2>&1

if pgrep -f 'supervisord -c /etc/supervisor/supervisord.conf' >/dev/null 2>&1; then
  echo "$(date -Is) [boot] supervisord 已在运行，跳过"
  exit 0
fi

if [ ! -x /usr/local/bin/bidvolt-init ]; then
  echo "$(date -Is) [boot] FATAL: /usr/local/bin/bidvolt-init 不存在" >&2
  exit 1
fi

echo "$(date -Is) [boot] 拉起 bidvolt-init"
nohup /usr/local/bin/bidvolt-init > /var/log/bidvolt/boot-init.log 2>&1 </dev/null &
echo "$(date -Is) [boot] launched pid=$!"
exit 0
