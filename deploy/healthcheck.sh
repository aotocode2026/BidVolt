#!/usr/bin/env bash
# 容器健康检查：PG 就绪 + API /healthz
set -uo pipefail

/usr/lib/postgresql/16/bin/pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1 || exit 1
curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 || exit 1
exit 0
