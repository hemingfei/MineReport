"""题材词表 API（#17）：列表/提议、审核/合并/停用（admin）、题材浏览、种子导入。

权限矩阵（spec）：
- 查看（列表/详情/研报/成员/覆盖查询）：任意登录用户
- 提议：analyst 起（LLM 提议走分析管道，同样落待审）
- 审核/合并/停用、种子导入：admin
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session as OrmSession

from ..auth import require_role
from ..db import get_db
from .. import analysis
from .. import synthesis
from ..models import (
    ReportTheme,
    ResearchReport,
    Role,
    Task,
    TaskStatus,
    Target as TargetRow,
    Theme,
    ThemeMembership,
    User,
)
from ..themes import (
    ThemeStatus,
    approve_theme,
    merge_theme,
    propose_theme,
    retire_theme,
)

router = APIRouter(prefix="/api/themes", tags=["themes"])


class ThemeLatestSynthesis(BaseModel):
    """题材最新综合分析引用（详情页入口；生成/刷新走 /api/syntheses）。"""

    id: int
    version: int
    created_at: dt.datetime


class ThemeOut(BaseModel):
    id: int
    name: str
    status: str
    definition: str
    synonyms: list[str]
    source: str
    seed_code: str | None
    merged_into_id: int | None
    created_at: dt.datetime
    report_count: int = 0
    member_count: int = 0
    latest_synthesis: ThemeLatestSynthesis | None = None  # 仅详情填充（列表免 N+1）


class ThemeListOut(BaseModel):
    items: list[ThemeOut]
    total: int
    limit: int
    offset: int


class ThemeProposeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    definition: str = ""
    synonyms: list[str] = Field(default_factory=list, max_length=32)


class ThemePatchIn(BaseModel):
    """action 语义词表：approve（审核在册）/ retire（停用）/ merge（合并）。
    approve 可同时补定义与同义词；merge 必须给 merge_into_id。"""

    action: Literal["approve", "retire", "merge"]
    definition: str | None = None
    synonyms: list[str] | None = None
    merge_into_id: int | None = None


class ThemeReportOut(BaseModel):
    id: int
    title: str
    broker: str
    publish_date: dt.date
    current_analysis_id: int | None


class ThemeReportListOut(BaseModel):
    items: list[ThemeReportOut]
    total: int


class ThemeMemberOut(BaseModel):
    code: str
    name: str
    exchange: str
    sw_l1_name: str | None
    source: str
    joined_at: dt.date
    is_active: bool


class ThemeMemberListOut(BaseModel):
    items: list[ThemeMemberOut]


class ImportOut(BaseModel):
    task_id: int


def _counts_by_theme(db: OrmSession) -> tuple[dict[int, int], dict[int, int]]:
    """report_count（当前版关联的未删研报数）与 member_count（活跃成员数），两条分组查询。"""
    report_counts = dict(
        db.execute(
            select(ReportTheme.theme_id, func.count(distinct(ReportTheme.report_id)))
            .join(ResearchReport, ResearchReport.id == ReportTheme.report_id)
            .where(*analysis.live_current_conds(ReportTheme))
            .group_by(ReportTheme.theme_id)
        ).all()
    )
    member_counts = dict(
        db.execute(
            select(ThemeMembership.theme_id, func.count())
            .where(ThemeMembership.is_active.is_(True))
            .group_by(ThemeMembership.theme_id)
        ).all()
    )
    return report_counts, member_counts


def _theme_out(theme: Theme, report_counts: dict[int, int], member_counts: dict[int, int]) -> ThemeOut:
    return ThemeOut(
        id=theme.id,
        name=theme.name,
        status=theme.status,
        definition=theme.definition or "",
        synonyms=[s for s in (theme.synonyms or [])],
        source=theme.source,
        seed_code=theme.seed_code,
        merged_into_id=theme.merged_into_id,
        created_at=theme.created_at,
        report_count=report_counts.get(theme.id, 0),
        member_count=member_counts.get(theme.id, 0),
    )


@router.get("", response_model=ThemeListOut)
def list_themes(
    status_filter: str = Query(default=ThemeStatus.ACTIVE, alias="status"),
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ThemeListOut:
    """词表列表：状态过滤（active|pending|retired|merged|all）+ 名称/同义词子串搜索。

    排序按研报数降序（浏览视角：最热的题材在前），同数按 id 稳定。计数走两条
    分组查询在内存 join（词表量 ~600，免逐条子查询）。
    """
    if status_filter not in (*ThemeStatus.ALL, "all"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="status 须为 active|pending|retired|merged|all",
        )
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    themes = list(db.scalars(select(Theme)))
    if status_filter != "all":
        themes = [t for t in themes if t.status == status_filter]
    if q:
        needle = unicodedata.normalize("NFKC", q).replace(" ", "").lower()
        themes = [
            t for t in themes
            if needle in (unicodedata.normalize("NFKC", t.name).replace(" ", "") + "".join(
                unicodedata.normalize("NFKC", s) for s in (t.synonyms or [])
            )).lower()
        ]

    report_counts, member_counts = _counts_by_theme(db)
    themes.sort(key=lambda t: (-report_counts.get(t.id, 0), t.id))
    window = themes[offset : offset + limit]
    return ThemeListOut(
        items=[_theme_out(t, report_counts, member_counts) for t in window],
        total=len(themes),
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=ThemeOut, status_code=status.HTTP_201_CREATED)
def propose_theme_endpoint(
    body: ThemeProposeIn,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> ThemeOut:
    """提议新题材：落待审（与在册/待审重名 409，不另立条目）。"""
    theme, conflict = propose_theme(
        db,
        name=body.name,
        definition=body.definition,
        synonyms=body.synonyms,
        proposed_by=user.id,
    )
    if theme is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=conflict or "题材名冲突",
        )
    db.commit()
    return _theme_out(theme, {}, {})


def _get_theme(db: OrmSession, theme_id: int) -> Theme:
    theme = db.get(Theme, theme_id)
    if theme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="theme not found")
    return theme


@router.post("/import", status_code=status.HTTP_202_ACCEPTED, response_model=ImportOut)
def trigger_theme_import(
    user: User = Depends(require_role(Role.ADMIN)),
    db: OrmSession = Depends(get_db),
) -> ImportOut:
    """触发题材种子导入（幂等）：东财概念（滤噪音）+ 申万二级骨架，成分股作 seed 成员。"""
    task = Task(
        kind="import_themes",
        status=TaskStatus.UPLOADED,
        payload={"triggered_by": user.id},
    )
    db.add(task)
    db.commit()
    return ImportOut(task_id=task.id)


@router.patch("/{theme_id}", response_model=ThemeOut)
def patch_theme(
    theme_id: int,
    body: ThemePatchIn,
    user: User = Depends(require_role(Role.ADMIN)),
    db: OrmSession = Depends(get_db),
) -> ThemeOut:
    """治理动作（admin）：approve / retire / merge。状态机违例返回 409。"""
    theme = _get_theme(db, theme_id)
    try:
        if body.action == "approve":
            approve_theme(
                db, theme, user.id,
                definition=body.definition, synonyms=body.synonyms,
            )
        elif body.action == "retire":
            retire_theme(db, theme, user.id)
        else:  # merge（Literal 收敛了词表，这里只剩一个分支）
            if body.merge_into_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="merge 需要 merge_into_id（合并去向题材）",
                )
            dst = _get_theme(db, body.merge_into_id)
            merge_theme(db, theme, dst, user.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    report_counts, member_counts = _counts_by_theme(db)
    return _theme_out(theme, report_counts, member_counts)


@router.get("/{theme_id}", response_model=ThemeOut)
def get_theme(
    theme_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ThemeOut:
    theme = _get_theme(db, theme_id)
    report_counts, member_counts = _counts_by_theme(db)
    out = _theme_out(theme, report_counts, member_counts)
    latest = synthesis.latest_for_theme(db, theme.id)
    if latest is not None:
        out.latest_synthesis = ThemeLatestSynthesis(
            id=latest.id, version=latest.version, created_at=latest.created_at
        )
    return out


@router.get("/{theme_id}/reports", response_model=ThemeReportListOut)
def list_theme_reports(
    theme_id: int,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ThemeReportListOut:
    """题材下研报：仅当前分析版本的关联（重跑换版后旧关联不计），未删除。"""
    theme = _get_theme(db, theme_id)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    conds = (
        ReportTheme.theme_id == theme.id,
        *analysis.live_current_conds(ReportTheme),
    )
    total = db.scalar(
        select(func.count(distinct(ResearchReport.id)))
        .join(ReportTheme, ReportTheme.report_id == ResearchReport.id)
        .where(*conds)
    )
    reports = db.scalars(
        select(ResearchReport)
        .join(ReportTheme, ReportTheme.report_id == ResearchReport.id)
        .where(*conds)
        .distinct()
        .order_by(ResearchReport.publish_date.desc(), ResearchReport.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return ThemeReportListOut(
        items=[
            ThemeReportOut(
                id=r.id, title=r.title, broker=r.broker,
                publish_date=r.publish_date, current_analysis_id=r.current_analysis_id,
            )
            for r in reports
        ],
        total=total or 0,
    )


@router.get("/{theme_id}/members", response_model=ThemeMemberListOut)
def list_theme_members(
    theme_id: int,
    active_only: bool = False,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ThemeMemberListOut:
    """题材标的池：ThemeMembership + 主数据名称/行业随行（活跃在前，代码序）。"""
    theme = _get_theme(db, theme_id)
    conds = [ThemeMembership.theme_id == theme.id]
    if active_only:
        conds.append(ThemeMembership.is_active.is_(True))
    rows = db.execute(
        select(ThemeMembership, TargetRow)
        .join(TargetRow, TargetRow.code == ThemeMembership.target_code)
        .where(*conds)
        .order_by(ThemeMembership.is_active.desc(), ThemeMembership.target_code)
    ).all()
    return ThemeMemberListOut(items=[
        ThemeMemberOut(
            code=m.target_code, name=t.name, exchange=t.exchange,
            sw_l1_name=t.sw_l1_name, source=m.source,
            joined_at=m.joined_at, is_active=m.is_active,
        )
        for m, t in rows
    ])
