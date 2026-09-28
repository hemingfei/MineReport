"""pytest 基座：真实 Postgres 测试库，一键跑绿、可重复。

用法（backend/ 目录下）：
    uv run pytest          # 自动起 compose 的 postgres（若未起）
    uv run pytest -k storage

数据库要求见 conftest：
- 测试库 DATABASE_URL 默认指向 localhost:5432/minereport_test（compose 映射的同一实例）；
  每个会话开始 DROP 再 CREATE（幂等重跑），随后跑 alembic 迁移建表，会话结束 DROP。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def _psql(database: str, sql: str) -> None:
    """经 compose 里的 psql 执行管理语句（建/删测试库）。"""
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BACKEND_DIR.parent / "docker-compose.yml"),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def test_database_url() -> str:
    # 密码与 compose 同源（POSTGRES_PASSWORD，默认 postgres），不硬编码真实凭据
    pg_password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    url = f"postgresql+psycopg://postgres:{pg_password}@localhost:5432/minereport_test"
    # 幂等：先删后建（重复执行安全）
    _psql("postgres", "DROP DATABASE IF EXISTS minereport_test WITH (FORCE)")
    _psql("postgres", "CREATE DATABASE minereport_test")
    # 迁移建表（与生产同路径：alembic upgrade head）
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    yield url
    _psql("postgres", "DROP DATABASE IF EXISTS minereport_test WITH (FORCE)")
