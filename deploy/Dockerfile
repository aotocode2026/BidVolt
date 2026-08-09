# 单容器部署草案（待服务器容器环境确认后微调）
# 假设：debian bookworm、Python 3.11、PostgreSQL 16、平台不支持多容器（ADR D17）
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      postgresql-16 postgresql-contrib \
      supervisor cron \
      clamav-daemon \
      libreoffice-writer \
      unzip p7zip-full \
      rclone curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/bidvolt
COPY requirements.txt ./
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

COPY app ./app
COPY bidvolt_mcp ./bidvolt_mcp
COPY alembic ./alembic
COPY alembic.ini ./
COPY deploy/supervisord.conf /etc/supervisor/conf.d/bidvolt.conf
COPY deploy/bidvolt-init.sh /usr/local/bin/bidvolt-init
COPY deploy/backup.sh /usr/local/bin/bidvolt-backup
COPY deploy/healthcheck.sh /usr/local/bin/bidvolt-healthcheck
RUN chmod +x /usr/local/bin/bidvolt-init /usr/local/bin/bidvolt-backup /usr/local/bin/bidvolt-healthcheck

# 持久卷（硬前提）：pgdata / data / backups 三块子目录
VOLUME /var/lib/bidvolt

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
  CMD /usr/local/bin/bidvolt-healthcheck

ENTRYPOINT ["/usr/local/bin/bidvolt-init"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
