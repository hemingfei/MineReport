#!/bin/sh
set -e

# 迁移只由 api 容器执行一次；worker 等任务表就绪，避免双容器并发迁移竞速
wait_for_db() {
    echo "waiting for database..."
    for i in $(seq 1 60); do
        if uv run python -c "import sys
from sqlalchemy import create_engine, text
from app.config import get_settings
e = create_engine(get_settings().database_url)
with e.connect() as c:
    c.execute(text('SELECT 1'))
" >/dev/null 2>&1; then
            echo "database ready"
            return 0
        fi
        sleep 2
    done
    echo "database not ready after 120s" >&2
    return 1
}

wait_for_tasks_table() {
    echo "waiting for tasks table (api runs migrations)..."
    for i in $(seq 1 60); do
        if uv run python -c "import sys
from sqlalchemy import create_engine, text, inspect
from app.config import get_settings
e = create_engine(get_settings().database_url)
if inspect(e).has_table('tasks'):
    sys.exit(0)
sys.exit(1)
" >/dev/null 2>&1; then
            echo "tasks table ready"
            return 0
        fi
        sleep 2
    done
    echo "tasks table not ready after 120s" >&2
    return 1
}

wait_for_db

case "$1" in
    api)
        uv run alembic upgrade head
        exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
        ;;
    worker)
        wait_for_tasks_table
        exec uv run python -m app.worker
        ;;
    *)
        exec "$@"
        ;;
esac
