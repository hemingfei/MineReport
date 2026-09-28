"""邀请制认证与角色权限依赖（spec 权限矩阵的可复用基座）。

- 密码：stdlib scrypt（无第三方依赖），格式串 `scrypt$N$r$p$salt_b64$hash_b64`
- 会话：HttpOnly cookie 携带原始 token，sessions 表只存 sha256 哈希
- 角色：admin > analyst > reader 三级层级，`require_role("analyst")` 表示"至少 analyst"
  （矩阵各行的授权集合都是连续层级，故用最低角色而非集合表达）
- 语义：未认证 → 401；已认证但角色不足 → 403
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session as OrmSession

from .db import get_db
from .models import Role, User, UserSession

# scrypt 参数：N=2^14, r=8, p=1（OWASP 2023 建议的最低线，单次哈希 ~50ms）
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

SESSION_COOKIE = "mr_session"


# ---------- 密码 ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ---------- 会话 ----------

def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: OrmSession, user_id: int, ttl_days: int) -> tuple[str, dt.datetime]:
    """创建会话，返回 (原始 token, 过期时间)。原始 token 只出现在 cookie 里，库里存哈希。"""
    token = new_session_token()
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=ttl_days)
    db.add(UserSession(token_hash=_token_hash(token), user_id=user_id, expires_at=expires_at))
    return token, expires_at


def delete_session(db: OrmSession, token: str) -> None:
    db.query(UserSession).filter(UserSession.token_hash == _token_hash(token)).delete()


def new_invitation_token() -> str:
    return secrets.token_urlsafe(24)


# ---------- FastAPI 依赖 ----------

def get_current_user(
    mr_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: OrmSession = Depends(get_db),
) -> User:
    if not mr_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = (
        db.query(User)
        .join(UserSession, UserSession.user_id == User.id)
        .filter(
            UserSession.token_hash == _token_hash(mr_session),
            UserSession.expires_at > dt.datetime.now(dt.timezone.utc),
        )
        .one_or_none()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user disabled")
    return user


def require_role(minimum: str):
    """权限依赖工厂：要求当前用户角色 >= minimum（层级 admin > analyst > reader）。

    用法：`Depends(require_role(Role.ADMIN))`；需要"任意登录用户"时直接用 get_current_user。
    """

    def dependency(user: User = Depends(get_current_user)) -> User:
        if Role.RANK[user.role] < Role.RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role {minimum} or above",
            )
        return user

    return dependency
