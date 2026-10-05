FROM python:3.13-slim

WORKDIR /srv/app

# uv 安装依赖（锁定可复现，两层缓存）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# 入口脚本区分 api / worker；运维脚本（回填/重转）随镜像带上，服务器可直接 exec
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
COPY scripts/*.py ./scripts/

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
