"""数据库引擎与会话工厂。

对外只有三个函数：session_scope（事务边界）、get_db（FastAPI 请求依赖）、
use_database（测试重绑入口）。引擎与会话工厂是模块私有状态，所有导出函数
都在调用时解析它们——按值导入本模块的函数在任何重绑之后仍指向当前库，
这是测试可以安全换库的前提（旧公共属性 SessionLocal 已废除，守卫见
tests/test_db_seam.py）。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def _new_engine(url: str) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


_engine, _session_factory = _new_engine(get_settings().database_url)


def use_database(url: str) -> Engine:
    """把整个应用切到指定库（测试基座用）：换引擎与工厂，dispose 旧引擎。"""
    global _engine, _session_factory
    old = _engine
    _engine, _session_factory = _new_engine(url)
    old.dispose(close=True)
    return _engine


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """事务边界：成功 commit、异常 rollback。

    调用方中途打点 commit 的模式兼容——退出时的 commit 对已提交事务是
    no-op 安全网（忘记打点则从静默丢失变为自动提交）。
    """
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话，成功提交、异常回滚。"""
    with session_scope() as db:
        yield db
