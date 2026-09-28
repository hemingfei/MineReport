"""管理端点：邀请创建与列表、连接器运行日志（仅 admin）。"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from ..auth import get_current_user, new_invitation_token, require_role
from ..config import get_settings
from ..db import get_db
from ..models import ConnectorRun, Invitation, Role, User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role(Role.ADMIN))])


class InvitationCreate(BaseModel):
    role: str
    email: str | None = None

    @field_validator("role")
    @classmethod
    def _role_in_vocabulary(cls, v: str) -> str:
        if v not in Role.ALL:
            raise ValueError(f"role must be one of {Role.ALL}")
        return v


class InvitationOut(BaseModel):
    id: int
    token: str
    role: str
    email: str | None
    expires_at: dt.datetime
    used_at: dt.datetime | None
    created_at: dt.datetime


@router.post("/invitations", response_model=InvitationOut, status_code=status.HTTP_201_CREATED)
def create_invitation(
    body: InvitationCreate, admin: User = Depends(get_current_user), db: OrmSession = Depends(get_db)
) -> Invitation:
    # 权限已在 router 级 require_role(ADMIN) 挡过；这里注入当前用户只为拿 created_by
    invitation = Invitation(
        token=new_invitation_token(),
        role=body.role,
        email=body.email,
        created_by=admin.id,
        expires_at=dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(days=get_settings().invitation_ttl_days),
    )
    db.add(invitation)
    db.flush()
    return invitation


@router.get("/invitations", response_model=list[InvitationOut])
def list_invitations(db: OrmSession = Depends(get_db)) -> list[Invitation]:
    return list(db.scalars(select(Invitation).order_by(Invitation.id.desc())))


class ConnectorRunOut(BaseModel):
    id: int
    connector_id: str
    subscription_id: int | None
    event: str
    ok: bool
    message: str
    stats: dict | None
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class ConnectorRunListOut(BaseModel):
    items: list[ConnectorRunOut]
    total: int


@router.get("/connector-runs", response_model=ConnectorRunListOut)
def list_connector_runs(
    event: str | None = None,
    subscription_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: OrmSession = Depends(get_db),
) -> ConnectorRunListOut:
    """连接器运行日志（spec 用户故事 24）：成功/失败/死信/告警/手动下载。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conds = []
    if event:
        conds.append(ConnectorRun.event == event)
    if subscription_id is not None:
        conds.append(ConnectorRun.subscription_id == subscription_id)
    total = db.scalar(select(func.count()).select_from(ConnectorRun).where(*conds))
    rows = db.scalars(
        select(ConnectorRun)
        .where(*conds)
        .order_by(ConnectorRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return ConnectorRunListOut(items=[ConnectorRunOut.model_validate(r) for r in rows], total=total)
