#!/usr/bin/env bash
# 升级流程（Issue #3 发布门禁）：预检 → 备份 → 构建新 venv → 停应用 → 迁移 → 切换 → 冒烟 → 失败回滚
# 用法：bidvolt-upgrade [ref]     # ref = tag/commit，缺省取 origin/main 最新并记录 commit
# 原则：普通应用升级不重启 PostgreSQL；迁移前必先备份；失败不留半升级状态。
set -euo pipefail

REPO="${REPO:-/data/bidvolt}"
HERMES_HOME="${HERMES_HOME:-/data/hermes}"
cd "$REPO"

log() { echo "[upgrade] $*"; }
die() { echo "FATAL: $*" >&2; exit 1; }

# ---------- 1. 预检 ----------
log "1/8 预检"
[ -d .git ] || die "仓库无 .git（应使用 git archive 制品 + 单独部署脚本升级，或先 git clone）"
[ -x .venv/bin/uvicorn ] || die "当前 .venv 不可用，无法构建回滚基线"
[ -f .env ] || die "缺少 .env"
df -k . | awk 'NR==2 { if ($4 < 1048576) exit 1 }' || die "磁盘可用空间 < 1GB"
free -m | awk 'NR==2 { if ($7 < 512) exit 1 }' 2>/dev/null || log "WARN: 可用内存 < 512MB"
"$REPO/.venv/bin/python" - <<'PY' || die "JWT_SECRET 仍为占位值，禁止升级"
import os, re
env = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
s = env.get("JWT_SECRET", "")
assert len(s) >= 16, "JWT_SECRET 过短"
assert "change" not in s.lower() and "dev-only" not in s.lower(), "占位值"
PY

# ---------- 2. 记录基线 ----------
log "2/8 记录发布基线"
PREV_COMMIT=$(git rev-parse HEAD)
git fetch --tags origin
TARGET_REF="${1:-}"
if [ -z "$TARGET_REF" ]; then
  TARGET_REF=$(git rev-parse origin/main)
  TARGET_LABEL="origin/main @ $TARGET_REF"
else
  TARGET_LABEL="$TARGET_REF"
fi
[ "$TARGET_REF" != "$PREV_COMMIT" ] || die "目标与当前版本相同（$TARGET_REF）"
git rev-parse --verify "$TARGET_REF^{commit}" >/dev/null 2>&1 || die "无法解析目标版本：$TARGET_REF"
.venv/bin/pip freeze > /tmp/bidvolt-prev-freeze.txt
echo "    当前：$PREV_COMMIT  目标：$TARGET_LABEL"

# ---------- 3. 备份 ----------
log "3/8 备份（数据库 + 业务文件）"
[ -x /usr/local/bin/bidvolt-backup ] && /usr/local/bin/bidvolt-backup "pre-upgrade" || log "WARN: 未找到 bidvolt-backup，跳过（请人工确认备份存在）"

# ---------- 4. 构建新 venv（不碰运行中版本） ----------
log "4/8 检出目标代码并构建新 venv"
git checkout -q "$TARGET_REF"
rm -rf .venv-new
python3 -m venv .venv-new
.venv-new/bin/pip install --upgrade pip >/dev/null
.venv-new/bin/pip install -r requirements.txt || { log "依赖安装失败，回滚代码"; git checkout -q "$PREV_COMMIT"; rm -rf .venv-new; die "依赖安装失败"; }
[ -f requirements-dev.txt ] && .venv-new/bin/pip install -r requirements-dev.txt || true
.venv-new/bin/python -m compileall -q app bidvolt_mcp || { git checkout -q "$PREV_COMMIT"; rm -rf .venv-new; die "语法检查失败"; }

# ---------- 5. 停应用（不重启 PG） ----------
log "5/8 停止 app/worker（PostgreSQL 保持运行）"
supervisorctl stop app worker 2>/dev/null || log "WARN: supervisorctl stop 返回非零（可能未运行）"

# ---------- 6. 迁移（失败即中止发布，不启动新应用） ----------
log "6/8 alembic 迁移"
export DATABASE_URL="${DATABASE_URL:-$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)}"
if ! .venv-new/bin/alembic upgrade head; then
  log "迁移失败：保持旧版本运行，数据库请按需从备份恢复（/data/backups/bidvolt_*.dump）"
  log "回滚代码到 $PREV_COMMIT"
  git checkout -q "$PREV_COMMIT"
  rm -rf .venv-new
  supervisorctl start app worker 2>/dev/null || true
  die "迁移失败，发布中止"
fi

# ---------- 7. 原子切换 venv 并启动 ----------
log "7/8 切换 venv 并启动新版本"
mv .venv .venv-prev
mv .venv-new .venv
# 幂等生成 supervisor 配置（新版本脚本可能变化）
sed -e "s|__REPO__|$REPO|g" -e "s|__HERMES_HOME__|$HERMES_HOME|g" \
    deploy/supervisord.conf > /etc/supervisor/conf.d/bidvolt.conf
cp deploy/backup.sh /usr/local/bin/bidvolt-backup
cp deploy/healthcheck.sh /usr/local/bin/bidvolt-healthcheck
chmod +x /usr/local/bin/bidvolt-backup /usr/local/bin/bidvolt-healthcheck
supervisorctl reread 2>/dev/null || true
supervisorctl update 2>/dev/null || true
supervisorctl start app worker

# ---------- 8. 冒烟验证（失败自动回滚） ----------
log "8/8 冒烟验证"
_rollback() {
  log "!! 冒烟失败：回滚到 $PREV_COMMIT"
  supervisorctl stop app worker 2>/dev/null || true
  git checkout -q "$PREV_COMMIT"
  rm -rf .venv
  mv .venv-prev .venv
  supervisorctl start app worker 2>/dev/null || true
  die "升级失败，已回滚（数据库迁移不回滚，必要时从备份恢复：/data/backups/bidvolt_*.dump）"
}
sleep 3
for i in 1 2 3 4 5; do
  curl -fsS http://127.0.0.1:8123/healthz >/dev/null 2>&1 && break
  [ "$i" = "5" ] && _rollback
  sleep 3
done
bash /usr/local/bin/bidvolt-healthcheck || _rollback
HEAD_MIGRATION=$(.venv/bin/alembic heads 2>/dev/null | head -1 | cut -d' ' -f1)
CURRENT_MIGRATION=$(.venv/bin/alembic current 2>/dev/null | head -1 | cut -d' ' -f1)
[ "$HEAD_MIGRATION" = "$CURRENT_MIGRATION" ] || _rollback
supervisorctl status app worker | grep -q RUNNING || _rollback

# 成功：清理旧 venv，保存发布记录
rm -rf .venv-prev
cat >> /var/log/bidvolt/releases.log <<EOF
$(date -Is) operator=${SUDO_USER:-root} from=$PREV_COMMIT to=$TARGET_REF migration=$CURRENT_MIGRATION backup=/data/backups/bidvolt_*.dump result=OK
EOF
log "升级完成：$PREV_COMMIT -> $TARGET_LABEL（迁移 $CURRENT_MIGRATION）"
log "发布记录见 /var/log/bidvolt/releases.log；回滚数据库用 /data/backups/bidvolt_*.dump"
