"""综合分析 API（#20）：题材一键生成（202+task / 缓存命中 200）、结果与版本链、手动刷新。

权限矩阵（spec）：触发/刷新综合分析 analyst 起；查看任意登录用户。
缓存语义：POST 时比对最新版输入指纹，未变命中缓存直接返回既有版本（不建任务
不烧 token）；同输入的在途任务也直接复用（防并发重复触发）。手动刷新走
POST /{id}/refresh：以刷新时点最新研报集强制重算并 version++。题材词表变更
不自动失效缓存——刷新按钮兜底。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .. import analysis, synthesis
from ..auth import require_role
from ..db import get_db
from ..models import ResearchReport, Role, Synthesis, Task, Theme, User
from ..worker import enqueue

router = APIRouter(prefix="/api/syntheses", tags=["syntheses"])


class SynthesisCreateIn(BaseModel):
    theme_id: int  # 一期仅题材输入（spec）；强制重算走 /{id}/refresh


class SynthesisCreateOut(BaseModel):
    cached: bool
    synthesis_id: int | None = None  # 缓存命中时指向既有版本
    version: int | None = None
    task_id: int | None = None  # 重新计算时指向 synthesize 任务（含复用在途任务）
    report_count: int  # 本次输入研报数（缓存命中 = 既有版本输入数）


class SynthesisReportRef(BaseModel):
    """证据边界里的研报条目（引用回链的渲染数据源）。"""

    id: int
    title: str
    broker: str
    publish_date: dt.date


class SynthesisVersionRef(BaseModel):
    id: int
    version: int
    created_at: dt.datetime


class SynthesisOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    theme_id: int
    version: int
    input_fingerprint: str
    report_ids: list[int]
    prompt_version: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    result: dict
    created_by: int
    created_at: dt.datetime


class SynthesisDetailOut(SynthesisOut):
    theme_name: str
    versions: list[SynthesisVersionRef]  # 本题材版本链（最新在前），前端版本切换用
    reports: list[SynthesisReportRef]  # 证据边界（按 report_ids 序位 = 引用编号）


class RefreshOut(BaseModel):
    task_id: int
    synthesis_id: int  # 刷新发起自哪个版本（新版本由任务产物给出）
    report_count: int


def _get_synthesizable_theme(db: OrmSession, theme_id: int) -> Theme:
    """题材存在且可综合（判据见 synthesis.theme_unavailable_reason）。"""
    theme = db.get(Theme, theme_id)
    if theme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="theme not found")
    if reason := synthesis.theme_unavailable_reason(theme):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    return theme


def _require_ready_and_select(db: OrmSession, theme: Theme) -> tuple[list[int], int]:
    """生成/刷新的公共前置：LLM 已配置（503）且题材下有可综合研报（409）。"""
    if not analysis.llm_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM 未配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）",
        )
    report_ids, total = synthesis.select_input_reports(db, theme)
    if not report_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="题材下暂无可综合的研报（需先完成分析并关联本题材）",
        )
    return report_ids, total


def _enqueue(
    db: OrmSession,
    theme: Theme,
    user: User,
    report_ids: list[int],
    input_total: int,
    *,
    force: bool,
) -> Task:
    """公共入队路径：选集快照进 payload，worker 按快照执行（选择即请求）；
    同题材同快照的在途任务直接复用（不重复烧 token）。"""
    existing = synthesis.inflight_task(db, theme.id, report_ids)
    if existing is not None:
        return existing
    task = enqueue(
        db,
        "synthesize",
        {
            "theme_id": theme.id,
            "report_ids": report_ids,
            "input_total": input_total,
            "force": force,
            "triggered_by": user.id,
        },
    )
    db.flush()
    return task


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=SynthesisCreateOut)
def create_synthesis(
    body: SynthesisCreateIn,
    response: Response,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> SynthesisCreateOut:
    """一键生成：输入指纹未变命中缓存（200 + cached=True，不重算）；变了才建
    synthesize 任务（202 + task_id，轮询语义与既有任务一致）。"""
    theme = _get_synthesizable_theme(db, body.theme_id)
    report_ids, _total = _require_ready_and_select(db, theme)

    latest = synthesis.latest_for_theme(db, theme.id)
    if latest is not None and latest.input_fingerprint == synthesis.input_fingerprint(report_ids):
        response.status_code = status.HTTP_200_OK
        return SynthesisCreateOut(
            cached=True,
            synthesis_id=latest.id,
            version=latest.version,
            report_count=len(latest.report_ids or []),
        )

    task = _enqueue(db, theme, user, report_ids, _total, force=False)
    db.commit()
    return SynthesisCreateOut(cached=False, task_id=task.id, report_count=len(report_ids))


def _get_synthesis(db: OrmSession, synthesis_id: int) -> Synthesis:
    row = db.get(Synthesis, synthesis_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="synthesis not found")
    return row


@router.get("/{synthesis_id}", response_model=SynthesisDetailOut)
def get_synthesis(
    synthesis_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> SynthesisDetailOut:
    """结果页数据：产出结构 + 证据边界（引用回链的研报清单）+ 版本链（切换用）。"""
    row = _get_synthesis(db, synthesis_id)
    theme = db.get(Theme, row.theme_id)
    versions = db.scalars(
        select(Synthesis)
        .where(Synthesis.theme_id == row.theme_id)
        .order_by(Synthesis.version.desc())
    ).all()
    # 证据边界研报已删的也保留 id（口径是"当时的输入"），元数据缺省占位
    reports = {
        r.id: r
        for r in db.scalars(select(ResearchReport).where(ResearchReport.id.in_(row.report_ids or [])))
    }
    return SynthesisDetailOut(
        **SynthesisOut.model_validate(row).model_dump(),
        theme_name=theme.name if theme else f"#{row.theme_id}",
        versions=[
            SynthesisVersionRef(id=v.id, version=v.version, created_at=v.created_at)
            for v in versions
        ],
        reports=[
            SynthesisReportRef(
                id=rid,
                title=reports[rid].title if rid in reports else f"#{rid}（已删除）",
                broker=reports[rid].broker if rid in reports else "",
                publish_date=reports[rid].publish_date if rid in reports else dt.date(1970, 1, 1),
            )
            for rid in row.report_ids or []
        ],
    )


@router.post(
    "/{synthesis_id}/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshOut,
)
def refresh_synthesis(
    synthesis_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> RefreshOut:
    """手动刷新：以刷新时点的最新研报集强制重算，version++（缓存兜底入口）。"""
    row = _get_synthesis(db, synthesis_id)
    theme = _get_synthesizable_theme(db, row.theme_id)
    report_ids, total = _require_ready_and_select(db, theme)
    task = _enqueue(db, theme, user, report_ids, total, force=True)
    db.commit()
    return RefreshOut(task_id=task.id, synthesis_id=row.id, report_count=len(report_ids))
