"""订阅与连接器 API（#19）：订阅 CRUD（analyst+，admin 全量）、手动单篇下载
（额度提示后的确认动作）、额度汇总；管理员日志在 /api/admin/connector-runs。

权限（spec 矩阵）：订阅管理 admin ✓ / analyst ✓（仅自己的）；手动下载 analyst 起；
额度提示 analyst 起（下载前展示）。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session as OrmSession

from .. import scheduler
from ..auth import require_role
from ..config import get_settings
from ..connectors import available_connectors, build_connector, connector_class
from ..db import get_db
from ..errors import ConnectorError, PipelineError
from ..models import ConnectorRun, ExternalRef, Role, Subscription, Theme, User

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


class SubscriptionCreate(BaseModel):
    theme_id: int
    connector_id: str = "fxbaogao"
    interval_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    auto_download: bool = True
    keywords: list[str] | None = None
    orgs: list[str] | None = None


class SubscriptionPatch(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    auto_download: bool | None = None
    keywords: list[str] | None = None
    orgs: list[str] | None = None


class SubscriptionOut(BaseModel):
    id: int
    theme_id: int
    theme_name: str = ""  # 组装层回填（ORM 行上无此列）
    connector_id: str
    created_by: int
    enabled: bool
    interval_hours: int
    auto_download: bool
    keywords: list[str] | None
    orgs: list[str] | None
    next_run_at: dt.datetime | None
    last_run_at: dt.datetime | None
    last_success_at: dt.datetime | None
    attempt: int
    consecutive_failures: int
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class SubscriptionListOut(BaseModel):
    items: list[SubscriptionOut]
    total: int


class RefOut(BaseModel):
    id: int
    connector_id: str
    external_id: str
    status: str
    title: str
    broker: str | None
    publish_date: dt.date
    industry: str | None
    pages: int | None
    snippet: str | None
    report_id: int | None
    subscription_id: int | None
    report_url: str | None
    discovered_at: dt.datetime
    fetched_at: dt.datetime | None
    last_error: str | None


class RefListOut(BaseModel):
    items: list[RefOut]
    total: int


class ManualDownloadOut(BaseModel):
    ref: RefOut
    report_id: int
    task_id: int


class QuotaOut(BaseModel):
    downloads_today: int
    downloads_total: int
    hints: dict[str, str]  # connector_id -> 下载前额度提示文案


def _ref_out(r: ExternalRef) -> RefOut:
    cls = connector_class(r.connector_id)
    report_url = cls.view_url(r.external_id) if cls is not None else None
    return RefOut(
        id=r.id, connector_id=r.connector_id, external_id=r.external_id, status=r.status,
        title=r.title, broker=r.broker, publish_date=r.publish_date, industry=r.industry,
        pages=r.pages, snippet=r.snippet, report_id=r.report_id,
        subscription_id=r.subscription_id, report_url=report_url,
        discovered_at=r.discovered_at, fetched_at=r.fetched_at, last_error=r.last_error,
    )


def _sub_out(sub: Subscription, theme_names: dict[int, str]) -> SubscriptionOut:
    out = SubscriptionOut.model_validate(sub)
    out.theme_name = theme_names.get(sub.theme_id, f"#{sub.theme_id}")
    return out


def _theme_names(db: OrmSession, subs: list[Subscription]) -> dict[int, str]:
    """批量预取题材名（列表层免 N+1，与 reports 的 tags 预取同一模式）。"""
    ids = {s.theme_id for s in subs}
    if not ids:
        return {}
    return dict(
        db.execute(select(Theme.id, Theme.name).where(Theme.id.in_(ids))).all()
    )


def _get_own_subscription(db: OrmSession, sub_id: int, user: User) -> Subscription:
    """取订阅并校验归属：admin 任意，analyst 仅自己的。"""
    sub = db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="subscription not found")
    if user.role != Role.ADMIN and sub.created_by != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="subscription not found")
    return sub


# ---------- 订阅 CRUD ----------


@router.post(
    "",
    response_model=SubscriptionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_subscription(
    body: SubscriptionCreate,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> SubscriptionOut:
    if body.connector_id not in available_connectors():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"未知连接器 {body.connector_id!r}，可用：{available_connectors()}",
        )
    theme = db.get(Theme, body.theme_id)
    if theme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="theme not found")
    if theme.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"题材不在册（status={theme.status}），不能订阅"
        )
    sub = Subscription(
        theme_id=theme.id,
        connector_id=body.connector_id,
        created_by=user.id,
        enabled=True,
        interval_hours=body.interval_hours or get_settings().subscription_default_interval_hours,
        auto_download=body.auto_download,
        keywords=[k for k in (body.keywords or []) if k.strip()] or None,
        orgs=[o for o in (body.orgs or []) if o.strip()] or None,
    )
    scheduler.schedule_first_run(sub)
    db.add(sub)
    db.commit()
    return _sub_out(sub, _theme_names(db, [sub]))


@router.get("", response_model=SubscriptionListOut)
def list_subscriptions(
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> SubscriptionListOut:
    """analyst 看自己的订阅；admin 看全部。"""
    query = select(Subscription).order_by(Subscription.id.desc())
    if user.role != Role.ADMIN:
        query = query.where(Subscription.created_by == user.id)
    subs = db.scalars(query).all()
    names = _theme_names(db, list(subs))
    return SubscriptionListOut(items=[_sub_out(s, names) for s in subs], total=len(subs))


@router.patch("/{sub_id}", response_model=SubscriptionOut)
def patch_subscription(
    sub_id: int,
    body: SubscriptionPatch,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> SubscriptionOut:
    """改订阅：enabled/interval/keywords/orgs 归属人即可；auto_download 是额度开关，
    仅 admin（spec 用户故事 23：管理员控制下载额度消耗）。"""
    sub = _get_own_subscription(db, sub_id, user)
    if body.auto_download is not None:
        if user.role != Role.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="自动下载开关仅管理员可改（额度管控）"
            )
        sub.auto_download = body.auto_download
    if body.enabled is not None:
        sub.enabled = body.enabled
    if body.interval_hours is not None:
        sub.interval_hours = body.interval_hours
    if body.keywords is not None:
        sub.keywords = [k for k in body.keywords if k.strip()] or None
    if body.orgs is not None:
        sub.orgs = [o for o in body.orgs if o.strip()] or None
    db.commit()
    return _sub_out(sub, _theme_names(db, [sub]))


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subscription(
    sub_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> None:
    sub = _get_own_subscription(db, sub_id, user)
    db.delete(sub)
    db.commit()


# ---------- 发现记录与手动下载 ----------


@router.get("/refs", response_model=RefListOut)
def list_refs(
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> RefListOut:
    """发现记录（analyst 看自己订阅的，admin 全量）：手动下载工作池。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conds = []
    if user.role != Role.ADMIN:
        own_ids = db.scalars(
            select(Subscription.id).where(Subscription.created_by == user.id)
        ).all()
        conds.append(ExternalRef.subscription_id.in_(own_ids or [-1]))
    if status_filter:
        conds.append(ExternalRef.status == status_filter)
    total = db.scalar(select(func.count()).select_from(ExternalRef).where(*conds))
    refs = db.scalars(
        select(ExternalRef).where(*conds).order_by(ExternalRef.id.desc()).limit(limit).offset(offset)
    ).all()
    return RefListOut(items=[_ref_out(r) for r in refs], total=total)


@router.post("/refs/{ref_id}/download", response_model=ManualDownloadOut)
def download_ref(
    ref_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> ManualDownloadOut:
    """手动单篇下载（额度提示后的确认动作）：fetch → 入库 → convert 任务。

    duplicate/ingested 幂等返回；fetch 失败落 ref.last_error 并返回错误详情。
    """
    ref = db.get(ExternalRef, ref_id)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ref not found")
    if user.role != Role.ADMIN and ref.subscription_id is not None:
        sub = db.get(Subscription, ref.subscription_id)
        if sub is not None and sub.created_by != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ref not found")
    try:
        _ref, report, task = scheduler.manual_download(db, ref, user_id=user.id)
        task_id = task.id if task is not None else 0
    except PipelineError as e:
        db.rollback()
        ref = db.get(ExternalRef, ref_id)
        if ref is not None:
            scheduler.mark_fetch_failed(db, ref, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"下载失败（{e.error_code}）：{e.message}",
        ) from e
    db.commit()
    assert ref.report_id is not None
    return ManualDownloadOut(ref=_ref_out(ref), report_id=report.id, task_id=task_id)


# ---------- 额度 ----------


def _count_downloads(db: OrmSession) -> tuple[int, int]:
    """(今日, 总计) 下载次数：run 轮 + retry/dead_letter 轮 stats.downloaded 与
    manual_download 事件的 SQL 聚合（日志线性增长也不拖慢额度接口）。"""
    downloaded = func.coalesce(
        func.sum(cast(ConnectorRun.stats["downloaded"].as_integer(), Integer)), 0
    )
    base = select(downloaded).where(
        ConnectorRun.stats.is_not(None),
        ConnectorRun.stats["downloaded"].as_integer().is_not(None),
    )
    today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total = int(db.scalar(base) or 0)
    today = int(
        db.scalar(base.where(ConnectorRun.created_at >= today_start)) or 0
    )
    return today, total


@router.get("/quota", response_model=QuotaOut)
def connector_quota(
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> QuotaOut:
    """额度汇总 + 各连接器手动下载提示文案（前端下载确认框用）。"""
    hints: dict[str, str] = {}
    for connector_id in available_connectors():
        try:
            hint = build_connector(connector_id).quota_hint()
        except ConnectorError:
            continue  # 凭据未配置的连接器不出提示
        if hint:
            hints[connector_id] = hint
    today, total = _count_downloads(db)
    return QuotaOut(downloads_today=today, downloads_total=total, hints=hints)
