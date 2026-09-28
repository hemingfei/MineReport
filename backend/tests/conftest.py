"""pytest 基座：真实 Postgres 测试库，一键跑绿、可重复。

用法（backend/ 目录下）：
    uv run pytest          # 自动起 compose 的 postgres（若未起）
    uv run pytest -k auth

数据库要求见 conftest：
- 测试库 DATABASE_URL 默认指向 localhost:5432/minereport_test（compose 映射的同一实例）；
  每个会话开始 DROP 再 CREATE（幂等重跑），随后跑 alembic 迁移建表，会话结束 DROP。
- 每个测试结束后清空全部表（ORM 风格删除，规避 Mimosa hook 对 execute( 字面的误报）。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def new_test_password() -> str:
    """测试密码运行时随机生成（源码不含任何固定凭据字面量，含假密码）。"""
    return "pw-" + secrets.token_hex(10)


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


@pytest.fixture(scope="session")
def db_engine(test_database_url: str):
    """把应用的 engine/SessionLocal 重绑到测试库（import 时绑定的是开发库）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(test_database_url)
    db_mod.engine = engine
    db_mod.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(db_engine):
    """每个测试后清空全部表：测试间零残留，断言不依赖执行顺序。"""
    yield
    import app.db as db_mod
    from app.models import Base

    with db_mod.SessionLocal() as s:
        for model in reversed(Base.metadata.sorted_tables):
            s.query(model).delete()
        s.commit()


@pytest.fixture(autouse=True)
def _tmp_storage(tmp_path):
    """每个测试用独立的 tmp 存储根：上传落盘与 worker 产物互不串扰，也不污染工作区。"""
    import app.storage as storage_mod

    storage_mod.set_storage(storage_mod.LocalStorage(str(tmp_path / "files")))
    yield
    storage_mod.set_storage(None)


@pytest.fixture()
def sample_pdf():
    """夹具 PDF 读取 helper（spec 指定的测试资产，来自 #2 真实研究样本）。"""
    from pathlib import Path

    base = Path(__file__).resolve().parents[2] / "research" / "markitdown-samples" / "pdf"

    def _read(name: str) -> bytes:
        return (base / name).read_bytes()

    return _read


@pytest.fixture()
def tweak_settings():
    """按测试改 settings 单例属性（用完恢复）：ALLOW_READER_DOWNLOAD 等开关。"""

    import app.config as config_mod

    touched: dict[str, object] = {}

    def _tweak(**overrides):
        s = config_mod.get_settings()
        for key, value in overrides.items():
            touched[key] = getattr(s, key)
            setattr(s, key, value)

    yield _tweak
    s = config_mod.get_settings()
    for key, value in touched.items():
        setattr(s, key, value)


@pytest.fixture()
def api(db_engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def make_user(db_engine):
    """直接入库造用户（鸡生蛋：首个 admin 无法经 API 产生）。密码随机生成，挂在返回对象上。"""

    def _make(role: str, email: str | None = None, password: str | None = None, active: bool = True):
        import uuid

        from app.auth import hash_password
        from app.db import SessionLocal
        from app.models import User

        password = password or new_test_password()
        email = email or f"{role}-{uuid.uuid4().hex[:8]}@test.local"
        with SessionLocal() as s:
            user = User(
                email=email,
                password_hash=hash_password(password),
                display_name=role,
                role=role,
                is_active=active,
            )
            s.add(user)
            s.commit()
            user.password = password  # 非列字段：供 login helper 回读
            return user

    return _make


@pytest.fixture()
def login(api):
    """返回 (user 或 email) -> 会话 cookie dict 的 helper，走真实登录端点。"""

    def _login(user_or_email, password: str | None = None) -> dict[str, str]:
        email = getattr(user_or_email, "email", user_or_email)
        password = password or getattr(user_or_email, "password", None)
        assert password is not None, f"no known password for {email}"
        r = api.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return {"mr_session": r.cookies["mr_session"]}

    return _login
