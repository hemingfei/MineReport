"""分析师覆盖查询 API（#17）：署名搜索 + 按分析师看其覆盖的题材与标的。

权限矩阵（spec）：查看（署名搜索/覆盖查询）——任意登录用户。

数据源是 analysis_authors 投影（LLM 从研报正文提取的署名原串，spec：作者/评级
只能从 PDF 抽）。只看当前分析版本；cert 可选精化、broker 可选区分同名。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session as OrmSession

from ..auth import require_role
from ..db import get_db
from .. import analysis
from ..models import (
    AnalysisAuthor,
    AnalysisTarget,
    ReportTheme,
    ResearchReport,
    Role,
    Target as TargetRow,
    Theme,
    User,
)
from ..themes import CURRENT_ANALYSIS_LINK

router = APIRouter(prefix="/api/authors", tags=["authors"])


class AuthorItem(BaseModel):
    name: str
    cert: str | None
    broker: str | None
    report_count: int


class AuthorListOut(BaseModel):
    items: list[AuthorItem]


class CoverageThemeItem(BaseModel):
    theme_id: int | None
    name: str  # 词表名；未关联（理论不出现）时退回原始串
    status: str | None
    report_ids: list[int]


class CoverageTargetItem(BaseModel):
    code: str
    name: str
    report_ids: list[int]


class AuthorCoverageOut(BaseModel):
    name: str
    cert: str | None
    broker: str | None
    reports_total: int
    themes: list[CoverageThemeItem]
    targets: list[CoverageTargetItem]


def _current_report_ids(
    db: OrmSession, name: str, cert: str | None, broker: str | None
) -> list[int]:
    conds = [
        AnalysisAuthor.name == name,
        CURRENT_ANALYSIS_LINK,
        ResearchReport.deleted_at.is_(None),
    ]
    if cert:
        conds.append(AnalysisAuthor.cert == cert)
    if broker:
        conds.append(ResearchReport.broker == broker)
    return list(
        db.scalars(
            select(AnalysisAuthor.report_id)
            .join(ResearchReport, ResearchReport.id == AnalysisAuthor.report_id)
            .where(*conds)
            .distinct()
        )
    )


@router.get("", response_model=AuthorListOut)
def search_authors(
    q: str = Query(min_length=1, max_length=64),
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> AuthorListOut:
    """署名搜索（自动补全用）：当前分析版本里的署名 + 归属券商与研报数。"""
    rows = db.execute(
        select(
            AnalysisAuthor.name,
            AnalysisAuthor.cert,
            ResearchReport.broker,
            func.count(distinct(AnalysisAuthor.report_id)),
        )
        .join(ResearchReport, ResearchReport.id == AnalysisAuthor.report_id)
        .where(
            AnalysisAuthor.name.like(f"%{q}%"),
            CURRENT_ANALYSIS_LINK,
            ResearchReport.deleted_at.is_(None),
        )
        .group_by(AnalysisAuthor.name, AnalysisAuthor.cert, ResearchReport.broker)
        .order_by(func.count(distinct(AnalysisAuthor.report_id)).desc())
        .limit(20)
    ).all()
    return AuthorListOut(items=[
        AuthorItem(name=n, cert=c, broker=b, report_count=cnt) for n, c, b, cnt in rows
    ])


@router.get("/coverage", response_model=AuthorCoverageOut)
def author_coverage(
    name: str = Query(min_length=1, max_length=128),
    cert: str | None = None,
    broker: str | None = None,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> AuthorCoverageOut:
    """覆盖查询（观点迁移追踪）：该分析师当前版本研报所涉题材与标的（含待审题材）。"""
    report_ids = _current_report_ids(db, name, cert, broker)

    theme_rows = db.execute(
        select(ReportTheme.theme_id, Theme, ReportTheme.report_id)
        .join(ResearchReport, ResearchReport.id == ReportTheme.report_id)
        .outerjoin(Theme, Theme.id == ReportTheme.theme_id)
        .where(
            *analysis.live_current_conds(ReportTheme),
            ReportTheme.report_id.in_(report_ids),
        )
    ).all()
    by_theme: dict[int, dict] = {}
    for theme_id, theme, report_id in theme_rows:
        entry = by_theme.setdefault(theme_id, {
            "theme_id": theme_id,
            "name": theme.name if theme else "",
            "status": theme.status if theme else None,
            "report_ids": set(),
        })
        entry["report_ids"].add(report_id)

    target_rows = db.execute(
        select(AnalysisTarget.target_code, TargetRow.name, AnalysisTarget.report_id)
        .join(ResearchReport, ResearchReport.id == AnalysisTarget.report_id)
        .join(TargetRow, TargetRow.code == AnalysisTarget.target_code)
        .where(
            *analysis.live_current_conds(AnalysisTarget),
            AnalysisTarget.report_id.in_(report_ids),
            AnalysisTarget.target_code.is_not(None),
        )
    ).all()
    by_target: dict[str, dict] = {}
    for code, target_name, report_id in target_rows:
        by_target.setdefault(code, {"code": code, "name": target_name, "report_ids": set()})[
            "report_ids"
        ].add(report_id)

    themes = sorted(by_theme.values(), key=lambda e: -len(e["report_ids"]))
    targets = sorted(by_target.values(), key=lambda e: (-len(e["report_ids"]), e["code"]))
    return AuthorCoverageOut(
        name=name,
        cert=cert,
        broker=broker,
        reports_total=len(report_ids),
        themes=[
            CoverageThemeItem(
                theme_id=e["theme_id"], name=e["name"], status=e["status"],
                report_ids=sorted(e["report_ids"], reverse=True),
            )
            for e in themes
        ],
        targets=[
            CoverageTargetItem(code=e["code"], name=e["name"], report_ids=sorted(e["report_ids"], reverse=True))
            for e in targets
        ],
    )
