#!/usr/bin/env bash
set -a
. /opt/bidvolt/.env
set +a
echo "DATABASE_URL=${DATABASE_URL:-EMPTY}"
echo "APP_DB_PASSWORD_SET=$([ -n "${APP_DB_PASSWORD:-}" ] && echo yes || echo no)"
