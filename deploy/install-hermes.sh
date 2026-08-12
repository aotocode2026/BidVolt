#!/usr/bin/env bash
# 容器内安装/更新 Hermes Agent（数据与 venv 全部位于 /data/hermes，重建容器后免重装依赖）。
# 依赖：/opt/bidvolt 代码与 .env 已就位（/opt/bidvolt 可为指向 /data/bidvolt 的符号链接）。
# 用法：bash deploy/install-hermes.sh
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/data/hermes}"
HERMES_VENV="$HERMES_HOME/venv"
HERMES="$HERMES_VENV/bin/hermes"
REPO="${REPO:-/data/bidvolt}"
BIDVOLT_ENV="${BIDVOLT_ENV:-$REPO/.env}"
UV_INDEX="${UV_DEFAULT_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

echo "==> 1/7 uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x /root/.local/bin/uv ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$PATH:/root/.local/bin"

echo "==> 2/7 Python 3.11 venv（/data/hermes/venv）"
mkdir -p "$HERMES_HOME"
if [ ! -x "$HERMES" ]; then
  uv python install 3.11
  uv venv "$HERMES_VENV" --python 3.11
fi

echo "==> 3/7 hermes-agent[mcp]"
export UV_DEFAULT_INDEX="$UV_INDEX"
uv pip install --python "$HERMES_VENV" "hermes-agent[mcp]"

echo "==> 4/7 生成 .env（仅首次；密钥来自 /opt/bidvolt/.env 或环境变量）"
if [ ! -f "$HERMES_HOME/.env" ]; then
  "$HERMES_VENV/bin/python" - "$BIDVOLT_ENV" "$HERMES_HOME/.env" <<'PY'
import os, re, sys

src, dst = sys.argv[1], sys.argv[2]
KEYS = ["MINIMAX_API_KEY", "MINIMAX_BASE_URL", "DASHSCOPE_API_KEY", "BIDVOLT_API_BASE", "BIDVOLT_INTERNAL_TOKEN"]
vals = {}
try:
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in KEYS:
                vals[k.strip()] = v.strip()
except FileNotFoundError:
    pass
vals.setdefault("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
vals.setdefault("BIDVOLT_API_BASE", "http://127.0.0.1:8123")
for k in KEYS:
    vals.setdefault(k, os.environ.get(k, ""))
with open(dst, "w", encoding="utf-8", newline="\n") as f:
    for k in KEYS:
        f.write(f"{k}={vals.get(k, '')}\n")
os.chmod(dst, 0o600)
print("wrote", dst)
PY
fi

echo "==> 5/7 配置模型/视觉/目录（幂等）"
export HERMES_HOME
"$HERMES" config set model.default "${MINIMAX_MODEL:-MiniMax-Text-01}" >/dev/null 2>&1 || true
"$HERMES" config set model.provider minimax >/dev/null 2>&1 || true
"$HERMES" config set model.max_tokens 8000 >/dev/null 2>&1 || true
"$HERMES" config set model_catalog.enabled false >/dev/null 2>&1 || true
"$HERMES" config set display.language zh >/dev/null 2>&1 || true

"$HERMES_VENV/bin/python" - "$HERMES_HOME/config.yaml" "$BIDVOLT_ENV" "$REPO" "$HERMES_HOME" <<'PY'
import os, re, sys, yaml

cfg_path, env_path, repo, hermes_home = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(cfg_path, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
aux = cfg.setdefault("auxiliary", {}).setdefault("vision", {})
aux.update({
    "provider": "custom",
    "model": "qwen-vl-max",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
})
token = os.environ.get("BIDVOLT_INTERNAL_TOKEN", "")
if env_path and os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DASHSCOPE_API_KEY="):
                aux["api_key"] = line.split("=", 1)[1].strip()
            elif line.startswith("BIDVOLT_INTERNAL_TOKEN="):
                token = line.split("=", 1)[1].strip()
cfg["mcp_servers"] = {
    "bidvolt": {
        "command": f"{repo}/.venv/bin/python",
        "args": ["-m", "bidvolt_mcp"],
        "env": {
            "PYTHONPATH": repo,
            "BIDVOLT_API_BASE": "http://127.0.0.1:8123",
            "BIDVOLT_INTERNAL_TOKEN": token,
        },
        "supports_parallel_tool_calls": True,
    }
}
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
print("patched", cfg_path)
PY

echo "==> 6/7 安装 bidvolt skills"
mkdir -p "$HERMES_HOME/skills/bidvolt"
for d in tender-parse material-match bid-generate mock-evaluate targeted-edit; do
  if [ -d "$REPO/docs/hermes/skills/$d" ]; then
    cp -r "$REPO/docs/hermes/skills/$d" "$HERMES_HOME/skills/bidvolt/"
  fi
done

echo "==> 7/7 supervisor [program:hermes]（幂等）"
"$HERMES_VENV/bin/python" - /etc/supervisor/conf.d/bidvolt.conf "$REPO/deploy/supervisord.conf" "$HERMES_HOME" <<'PY'
import re, sys

hermes_home = sys.argv[-1]
block = """
[program:hermes]
command=%s/venv/bin/hermes serve --skip-build --host 127.0.0.1 --port 9119
directory=%s
environment=HERMES_HOME="%s",PYTHONUNBUFFERED="1",PATH="%s/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
autorestart=true
priority=6
stopasgroup=true
""" % (hermes_home, hermes_home, hermes_home, hermes_home)
for path in sys.argv[1:-1]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r"(?ms)^# \[program:hermes\].*?(?=^\S|\Z)", "", content)
    if re.search(r"(?m)^\[program:hermes\]", content):
        continue
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip("\n") + block)
    print("patched", path)
PY

echo "==> 完成。验证：hermes --version；supervisorctl status hermes"
