"""db seam 契约：session_scope 事务语义、use_database 重绑、按值导入禁令。

守卫测试把"历史炸过两次"的纪律（顶层按值导入会话工厂写错库）变成可执行
检查：app/ 与 tests/ 里不允许再出现旧公共名（SessionLocal）、私有工厂名或
对 db 模块 engine 属性的直接引用——seam 化后按值导入导出函数是安全的
（调用时解析当前工厂），除此之外没有第二条合法拿会话的路径。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.db import session_scope
from app.models import Task, TaskStatus

BACKEND_DIR = Path(__file__).resolve().parent.parent
GUARD_SELF = Path(__file__).resolve()
DB_MODULE = BACKEND_DIR / "app" / "db.py"


def _python_sources():
    for sub in ("app", "tests", "scripts"):
        for path in (BACKEND_DIR / sub).rglob("*.py"):
            if "__pycache__" not in path.parts and path not in (GUARD_SELF, DB_MODULE):
                yield path


@pytest.mark.parametrize(
    "banned",
    [r"SessionLocal", r"_session_factory", r"\b_engine\b", r"\bdb(?:_mod)?\.engine\b"],
)
def test_no_byvalue_factory_references(banned: str) -> None:
    """工厂与 engine 在 db.py 之外零出现（按值导入无处可绑）。"""
    pattern = re.compile(banned)
    offenders = [
        str(p.relative_to(BACKEND_DIR))
        for p in _python_sources()
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_session_scope_commits_on_success(db_engine) -> None:
    with session_scope() as s:
        s.add(Task(kind="echo", status=TaskStatus.UPLOADED, payload={}))
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 1


def test_session_scope_rolls_back_on_exception(db_engine) -> None:
    with pytest.raises(RuntimeError):
        with session_scope() as s:
            s.add(Task(kind="echo", status=TaskStatus.UPLOADED, payload={}))
            raise RuntimeError("boom")
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 0


def test_use_database_swaps_factory(test_database_url: str) -> None:
    """重绑入口：换引擎后，session_scope 拿到的会话绑在新引擎上（晚解析契约）。"""
    import app.db as db_mod

    eng1 = db_mod.use_database(test_database_url)
    eng2 = db_mod.use_database(test_database_url)
    assert eng2 is not eng1
    with session_scope() as s:
        assert s.bind is eng2
        assert s.scalar(text("select 1")) == 1
    eng2.dispose()  # 与 conftest 会话级 teardown 对称，不留游离连接池
