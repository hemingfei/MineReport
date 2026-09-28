"""标的主数据 API（#16）：主数据搜索、人工确认队列、主数据导入任务。

权限矩阵（spec）：
- 主数据搜索：任意登录用户（分析结果展示需要）
- 队列查看/确认/驳回：analyst 起（人工兜底是分析侧工作）
- 触发导入：admin（冷启动/定期刷新的主数据管理动作）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from ..auth import require_role
from ..db import get_db
from ..models import ResearchReport, Role, Target, TargetMatch, User
from ..targets import MatchStatus, pending_matches, resolve_match
from ..worker import enqueue

router = APIRouter(prefix="/api/targets", tags=["targets"])


class TargetOut(BaseModel):
    code: str
    name: str
    exchange: str
    sw_l1_name: str | None
    sw_l2_name: str | None
    sw_l3_name: str | None


class TargetListOut(BaseModel):
    items: list[TargetOut]


class CandidateOut(BaseModel):
    code: str
    name: str
    exchange: str
    sw_l1_name: str | None
    score: float


class MatchOut(BaseModel):
    id: int
    report_id: int
    report_title: str
    analysis_id: int
    raw_name: str
    raw_code: str | None
    reason: str
    status: str
    candidates: list[CandidateOut] = []


class MatchListOut(BaseModel):
    items: list[MatchOut]


class ConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ImportIn(BaseModel):
    """with_name_history=True 时逐股回填新浪曾用名（~0.2s/股全量约 30 分钟，幂等可断点续跑）。"""

    with_name_history: bool = False


class ImportOut(BaseModel):
    task_id: int


@router.get("", response_model=TargetListOut)
def search_targets(
    q: str = Query(min_length=1, max_length=64),
    limit: int = 20,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> TargetListOut:
    """主数据搜索：6 位代码前缀，或简称子串（经 name_norm 命中空格/全角/后缀变体）。
    曾用名不走此口——它在瀑布匹配词典里，确认队列的候选已覆盖。"""
    limit = max(1, min(limit, 50))
    like = f"%{q}%"
    conds = Target.code.startswith(q) | Target.name.like(like) | Target.name_norm.like(like)
    rows = db.scalars(
        select(Target)
        .where(conds, Target.name != "")
        .order_by(Target.code)
        .limit(limit)
    ).all()
    return TargetListOut(items=[TargetOut(
        code=t.code, name=t.name, exchange=t.exchange,
        sw_l1_name=t.sw_l1_name, sw_l2_name=t.sw_l2_name, sw_l3_name=t.sw_l3_name,
    ) for t in rows])


def _match_out(m: TargetMatch, report_title: str) -> MatchOut:
    return MatchOut(
        id=m.id,
        report_id=m.report_id,
        report_title=report_title,
        analysis_id=m.analysis_id,
        raw_name=m.raw_name,
        raw_code=m.raw_code,
        reason=m.reason,
        status=m.status,
        candidates=[CandidateOut(**c) for c in (m.candidates or [])],
    )


@router.get("/matches", response_model=MatchListOut)
def list_matches(
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> MatchListOut:
    """人工确认队列（pending）：候选 + 研报上下文。"""
    return MatchListOut(items=[_match_out(m, r.title) for m, r in pending_matches(db)])


def _get_pending_match(db: OrmSession, match_id: int) -> TargetMatch:
    m = db.get(TargetMatch, match_id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="match not found")
    if m.status != MatchStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"队列条目已终审（{m.status}），不可重复操作",
        )
    return m


@router.post("/matches/{match_id}/confirm", response_model=MatchOut)
def confirm_match_endpoint(
    match_id: int,
    body: ConfirmIn,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> MatchOut:
    """确认到指定 6 位代码（必须存在于主数据——保护标的池不被未知代码污染）。"""
    m = _get_pending_match(db, match_id)
    if db.get(Target, body.code) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"代码 {body.code} 不在标的主数据中，请先导入或检查代码",
        )
    report = db.get(ResearchReport, m.report_id)
    title = report.title if report else ""
    resolve_match(db, m, user.id, code=body.code)
    db.commit()
    return _match_out(m, title)


@router.post("/matches/{match_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_match(
    match_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> None:
    """驳回：认定该串不是真实标的（LLM 幻觉/指数名/板块名等），移出队列。"""
    m = _get_pending_match(db, match_id)
    resolve_match(db, m, user.id, code=None, dismiss=True)
    db.commit()


@router.post("/import", status_code=status.HTTP_202_ACCEPTED, response_model=ImportOut)
def trigger_import(
    body: ImportIn | None = None,
    user: User = Depends(require_role(Role.ADMIN)),
    db: OrmSession = Depends(get_db),
) -> ImportOut:
    """触发主数据全量导入（幂等）：worker 拉取 akshare + 申万 xls 后 upsert。"""
    task = enqueue(
        db,
        "import_targets",
        {
            "triggered_by": user.id,
            "with_name_history": bool(body and body.with_name_history),
        },
    )
    db.commit()
    return ImportOut(task_id=task.id)
