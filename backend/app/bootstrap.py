"""初始管理员引导：邀请制系统的冷启动入口。

api 容器 entrypoint 在迁移后调用。BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD
都设置且库中尚无 admin 时创建首个管理员；其余情况静默跳过（幂等，重启安全）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from . import db
from .auth import hash_password
from .config import Settings
from .models import Role, User

logger = logging.getLogger(__name__)


def ensure_bootstrap_admin() -> bool:
    # 直接实例化（不走 lru_cache）：entrypoint 每次启动读到当前环境变量
    cfg = Settings()
    if not cfg.bootstrap_admin_email or not cfg.bootstrap_admin_password:
        return False

    with db.session_scope() as session:
        # 多个 admin 是合法状态（管理员可再邀管理员），存在任一即跳过
        if session.scalars(select(User.id).where(User.role == Role.ADMIN)).first() is not None:
            return False
        session.add(
            User(
                email=cfg.bootstrap_admin_email,
                password_hash=hash_password(cfg.bootstrap_admin_password),
                display_name="admin",
                role=Role.ADMIN,
            )
        )
        session.commit()
    logger.info("bootstrap admin created: %s", cfg.bootstrap_admin_email)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_bootstrap_admin()
