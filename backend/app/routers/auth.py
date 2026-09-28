"""认证端点：凭邀请注册、登录、登出、当前用户。"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from ..auth import (
    SESSION_COOKIE,
    create_session,
    delete_session,
    get_current_user,
    hash_password,
    verify_password,
)
from ..config import get_settings
from ..db import get_db
from ..models import Invitation, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str


class RegisterRequest(BaseModel):
    token: str
    email: str
    password: str = Field(min_length=8)
    display_name: str | None = None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


def _set_session_cookie(response: Response, token: str, expires_at: dt.datetime) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int((expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=s.session_cookie_secure,
        path="/",
    )


def _validate_invitation(db: OrmSession, token: str, email: str) -> Invitation:
    """校验邀请码可用（存在/未用/未过期/邮箱匹配）；原子认领在 register 的条件 UPDATE 里完成。"""
    invitation = db.scalars(select(Invitation).where(Invitation.token == token)).one_or_none()
    if invitation is None or invitation.used_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid invitation")
    if invitation.expires_at <= dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invitation expired")
    if invitation.email is not None and invitation.email != email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invitation is bound to another email"
        )
    return invitation


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, response: Response, db: OrmSession = Depends(get_db)) -> User:
    invitation = _validate_invitation(db, body.token, body.email)

    if db.scalars(select(User.id).where(User.email == body.email)).one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.email.split("@")[0],
        role=invitation.role,
    )
    try:
        db.add(user)
        db.flush()  # 拿 user.id 供邀请认领回填；并发同名注册在此触发唯一约束
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email already registered"
        ) from None

    claimed = (
        db.query(Invitation)
        .filter(Invitation.id == invitation.id, Invitation.used_at.is_(None))
        .update({"used_at": dt.datetime.now(dt.timezone.utc), "used_by": user.id})
    )
    if claimed != 1:  # 并发注册同一邀请码：认领失败，整体回滚
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid invitation")

    token, expires_at = create_session(db, user.id, get_settings().session_ttl_days)
    _set_session_cookie(response, token, expires_at)
    return user


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, response: Response, db: OrmSession = Depends(get_db)) -> User:
    user = db.scalars(select(User).where(User.email == body.email)).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user disabled")

    token, expires_at = create_session(db, user.id, get_settings().session_ttl_days)
    _set_session_cookie(response, token, expires_at)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    mr_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: OrmSession = Depends(get_db),
) -> None:
    # 幂等登出：无 cookie / 会话已失效也返回 204 并清 cookie，不制造死 cookie 残留
    if mr_session is not None:
        delete_session(db, mr_session)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
