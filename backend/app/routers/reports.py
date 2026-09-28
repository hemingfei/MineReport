"""研报 API（#13）：上传（202+task）、列表过滤分页、详情/正文/文件、软删与恢复。
（#15）分析触发/批量重跑/版本历史 + 自由 tag 增删。

权限矩阵（spec）：
- 上传/删除/恢复：analyst 起（删除/恢复 analyst 仅自己的，admin 任意）
- 浏览/正文/分析版本回看：任意登录用户
- 触发/重跑分析、打 tag：analyst 起
- 原始文件下载：analyst 起；reader 默认 403，ALLOW_READER_DOWNLOAD=true 放开
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from .. import analysis
from ..analysis import latest_converted_file
from ..auth import require_role
from ..config import get_settings
from ..conversion import SUPPORTED_EXTENSIONS
from ..db import get_db
from .. import search
from ..ingest import find_active_by_identity as _find_active_by_identity
from ..ingest import ingest_report_file
from ..models import (
    Analysis,
    AnalysisTarget,
    ReportFile,
    ReportTag,
    ReportTheme,
    ResearchReport,
    Role,
    Tag,
    Target as TargetRow,
    TargetMatch,
    Task,
    TaskStatus,
    User,
)
from ..storage import get_storage

router = APIRouter(prefix="/api/reports", tags=["reports"])

_SOFT_DELETE_WINDOW = dt.timedelta(days=get_settings().report_soft_delete_days)

_READ_CHUNK = 1024 * 1024  # 1MB：流式读上传件，同时算 sha256


class ReportFileOut(BaseModel):
    id: int
    filename: str
    content_type: str | None
    size_bytes: int
    file_sha256: str
    converted_at: dt.datetime | None

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    id: int
    title: str
    broker: str
    publish_date: dt.date
    created_by: int
    created_at: dt.datetime
    files: list[ReportFileOut]
    tags: list[str] = []
    current_analysis_id: int | None = None


class ReportListOut(BaseModel):
    items: list[ReportOut]
    total: int
    limit: int
    offset: int


class ReportCreateOut(BaseModel):
    task_id: int
    report_id: int
    file_id: int
    merged: bool  # True = 并入既有研报（同组合键）


class ReanalyzeOut(BaseModel):
    task_id: int
    report_id: int
    report_file_id: int


class BatchReanalyzeIn(BaseModel):
    report_ids: list[int] = Field(min_length=1, max_length=200)


class BatchReanalyzeOut(BaseModel):
    tasks: list[ReanalyzeOut]
    skipped: list[dict]  # {"report_id": int, "reason": str}


class AnalysisOut(BaseModel):
    id: int
    report_id: int
    report_file_id: int
    version: int
    prompt_version: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    result: dict
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class AnalysisListOut(BaseModel):
    items: list[AnalysisOut]
    current_analysis_id: int | None


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class TagOut(BaseModel):
    id: int
    name: str
    linked: bool  # False = 关联已存在（幂等重打）


def _tags_by_report(db: OrmSession, report_ids: list[int]) -> dict[int, list[str]]:
    if not report_ids:
        return {}
    rows = db.execute(
        select(ReportTag.report_id, Tag.name)
        .join(Tag, Tag.id == ReportTag.tag_id)
        .where(ReportTag.report_id.in_(report_ids))
        .order_by(ReportTag.created_at, Tag.id)
    ).all()
    out: dict[int, list[str]] = {}
    for report_id, name in rows:
        out.setdefault(report_id, []).append(name)
    return out


def _report_out(
    db: OrmSession,
    r: ResearchReport,
    files: list[ReportFile],
    tags: list[str] | None = None,
) -> ReportOut:
    """详情输出组装；tags 可由列表层批量预取传入（免 N+1），缺省单篇自查。"""
    if tags is None:
        tags = _tags_by_report(db, [r.id]).get(r.id, [])
    return ReportOut(
        id=r.id, title=r.title, broker=r.broker, publish_date=r.publish_date,
        created_by=r.created_by, created_at=r.created_at,
        files=[ReportFileOut.model_validate(f) for f in files],
        tags=tags, current_analysis_id=r.current_analysis_id,
    )


def _safe_filename(filename: str | None) -> str:
    """剥路径部分只留 basename，防目录穿越与空名。"""
    name = Path(filename or "").name.strip()
    return name or "upload.bin"


def _get_active_report(db: OrmSession, report_id: int) -> ResearchReport:
    report = db.get(ResearchReport, report_id)
    if report is None or report.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    return report


def _latest_uploaded_file(db: OrmSession, report_id: int) -> ReportFile:
    """最新上传的文件（按 id）；/markdown 的"最新"语义不同——按转换完成时间优先。"""
    file = db.scalar(
        select(ReportFile)
        .where(ReportFile.report_id == report_id)
        .order_by(ReportFile.id.desc())
        .limit(1)
    )
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report has no files")
    return file


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=ReportCreateOut)
async def create_report(
    file: UploadFile,
    broker: str = Form(...),
    publish_date: dt.date = Form(...),
    title: str = Form(""),
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> ReportCreateOut:
    s = get_settings()
    filename = _safe_filename(file.filename)
    if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"仅支持 PDF/DOCX：{filename}",
        )

    # 流式读入（超限即拒，不落盘）；sha256 由入库回调统一计算
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(_READ_CHUNK):
        size += len(chunk)
        if size > s.upload_max_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"文件超过 {s.upload_max_mb}MB 上限",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    # 入库回调（#19 抽取到 app.ingest，与连接器拉取共用同一套管道）
    report, file_row, task, merged = ingest_report_file(
        db,
        title=title or Path(filename).stem,
        broker=broker.strip(),
        publish_date=publish_date,
        filename=filename,
        content_type=file.content_type,
        data=data,
        user_id=user.id,
    )
    db.commit()
    return ReportCreateOut(
        task_id=task.id, report_id=report.id, file_id=file_row.id, merged=merged
    )


@router.get("", response_model=ReportListOut)
def list_reports(
    q: str | None = None,
    broker: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    theme_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ReportListOut:
    """列表：全文搜索词（标题/正文/总结，zhparser 分词）+ 券商精确匹配 + 发布日期闭
    区间 + 题材（当前分析版本的词表关联，#17）过滤 + limit/offset 分页，条件间可组合。
    多个搜索词按分词结果取交集（plainto_tsquery 语义）。tag/标的过滤由后续票接入。"""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    conds = [ResearchReport.deleted_at.is_(None)]
    if q and q.strip():
        conds.append(search.matches(q.strip()))
    if broker:
        conds.append(ResearchReport.broker == broker)
    if date_from:
        conds.append(ResearchReport.publish_date >= date_from)
    if date_to:
        conds.append(ResearchReport.publish_date <= date_to)
    if theme_id is not None:
        conds.append(
            select(ReportTheme.id).where(
                ReportTheme.theme_id == theme_id,
                ReportTheme.report_id == ResearchReport.id,
                *analysis.live_current_conds(ReportTheme),
            ).exists()
        )

    total = db.scalar(select(func.count()).select_from(ResearchReport).where(*conds))
    reports = db.scalars(
        select(ResearchReport)
        .where(*conds)
        .order_by(ResearchReport.publish_date.desc(), ResearchReport.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    report_ids = [r.id for r in reports]
    files_by_report: dict[int, list[ReportFile]] = {}
    for f in db.scalars(
        select(ReportFile).where(ReportFile.report_id.in_(report_ids)).order_by(ReportFile.id)
    ):
        files_by_report.setdefault(f.report_id, []).append(f)
    tags_map = _tags_by_report(db, report_ids)
    items = [
        _report_out(db, r, files_by_report.get(r.id, []), tags=tags_map.get(r.id, []))
        for r in reports
    ]
    return ReportListOut(items=items, total=total, limit=limit, offset=offset)


@router.get("/{report_id}", response_model=ReportOut)
def get_report(
    report_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ReportOut:
    report = _get_active_report(db, report_id)
    files = db.scalars(
        select(ReportFile).where(ReportFile.report_id == report.id).order_by(ReportFile.id)
    ).all()
    return _report_out(db, report, files)


@router.get("/{report_id}/markdown")
def get_report_markdown(
    report_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> Response:
    """正文 markdown：最近一个转换完成的文件（多来源文件后到优先，与分析输入同语义）。"""
    _get_active_report(db, report_id)
    file = latest_converted_file(db, report_id)
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="markdown 未就绪（转换未完成或失败）"
        )
    return Response(
        content=file.markdown_text, media_type="text/markdown; charset=utf-8"
    )


@router.get("/{report_id}/file")
def get_report_file(
    report_id: int,
    file_id: int | None = None,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> Response:
    if Role.RANK[user.role] < Role.RANK[Role.ANALYST] and not get_settings().allow_reader_download:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="读者默认不可下载原始文件"
        )
    _get_active_report(db, report_id)
    if file_id is None:
        file = _latest_uploaded_file(db, report_id)
    else:
        file = db.get(ReportFile, file_id)
        if file is None or file.report_id != report_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
    data = get_storage().get(file.storage_key)
    quoted = quote(file.filename)
    return Response(
        content=data,
        media_type=file.content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


def _require_report_owner_or_admin(
    db: OrmSession, report: ResearchReport, user: User
) -> None:
    if user.role != Role.ADMIN and report.created_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="仅 admin 可操作他人上传的研报"
        )


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(
    report_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> None:
    report = _get_active_report(db, report_id)
    _require_report_owner_or_admin(db, report, user)
    report.deleted_at = dt.datetime.now(dt.timezone.utc)
    report.deleted_by = user.id
    db.commit()


@router.post("/{report_id}/restore", response_model=ReportOut)
def restore_report(
    report_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> ReportOut:
    report = db.get(ResearchReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
    if report.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="研报未处于删除状态")
    _require_report_owner_or_admin(db, report, user)
    if dt.datetime.now(dt.timezone.utc) - report.deleted_at > _SOFT_DELETE_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"恢复窗口（{get_settings().report_soft_delete_days} 天）已过",
        )
    # 软删期间同组合键可能已新建条目：恢复撞唯一索引时明确 409，交由人工取舍
    conflict = _find_active_by_identity(
        db, report.title_norm, report.broker, report.publish_date, exclude_id=report.id
    )
    if conflict is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="同（标题, 券商, 日期）的研报已重新入库，请先处理冲突",
        )
    report.deleted_at = None
    report.deleted_by = None
    db.commit()
    return get_report(report_id=report.id, user=user, db=db)


# ---------- 分析：触发/批量重跑/版本历史（#15） ----------

def _analysis_input(db: OrmSession, report: ResearchReport) -> tuple[ReportFile | None, str | None]:
    """分析的输入文件与不可分析原因（互斥：file 为 None 时 reason 必非空）。"""
    if not analysis.llm_ready():
        return None, "llm_not_configured"
    file = latest_converted_file(db, report.id)
    if file is None:
        return None, "markdown_missing"
    return file, None


def _require_analysis_ready(db: OrmSession, report: ResearchReport) -> ReportFile:
    """单篇重跑的 precondition：快速失败不排队（LLM 未配置 503 / 无正文 409）。"""
    file, reason = _analysis_input(db, report)
    if reason == "llm_not_configured":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM 未配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）",
        )
    if reason == "markdown_missing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="研报尚无转换完成的正文，无法分析",
        )
    assert file is not None
    return file


@router.post("/{report_id}/reanalyze", status_code=status.HTTP_202_ACCEPTED, response_model=ReanalyzeOut)
def reanalyze_report(
    report_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> ReanalyzeOut:
    """单篇重跑：新起 analyze 任务（版本链 version++），不动转换产物。"""
    report = _get_active_report(db, report_id)
    file = _require_analysis_ready(db, report)
    task = Task(
        kind="analyze",
        status=TaskStatus.UPLOADED,
        payload={
            "report_id": report.id,
            "report_file_id": file.id,
            "triggered_by": user.id,
        },
    )
    db.add(task)
    db.commit()
    return ReanalyzeOut(task_id=task.id, report_id=report.id, report_file_id=file.id)


@router.post("/reanalyze", status_code=status.HTTP_202_ACCEPTED, response_model=BatchReanalyzeOut)
def batch_reanalyze(
    body: BatchReanalyzeIn,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> BatchReanalyzeOut:
    """批量重跑（prompt 迭代后刷历史库）：逐篇建 analyze 任务；无正文/已删/不存在的跳过并报告原因。"""
    if not analysis.llm_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM 未配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）",
        )
    tasks: list[ReanalyzeOut] = []
    skipped: list[dict] = []
    for report_id in dict.fromkeys(body.report_ids):  # 去重保序
        report = db.get(ResearchReport, report_id)
        if report is None or report.deleted_at is not None:
            skipped.append({"report_id": report_id, "reason": "not_found"})
            continue
        file, reason = _analysis_input(db, report)
        if reason is not None:
            skipped.append({"report_id": report_id, "reason": reason})
            continue
        assert file is not None
        task = Task(
            kind="analyze",
            status=TaskStatus.UPLOADED,
            payload={
                "report_id": report.id,
                "report_file_id": file.id,
                "triggered_by": user.id,
                "batch": True,
            },
        )
        db.add(task)
        db.flush()
        tasks.append(
            ReanalyzeOut(task_id=task.id, report_id=report.id, report_file_id=file.id)
        )
    db.commit()
    return BatchReanalyzeOut(tasks=tasks, skipped=skipped)


@router.get("/{report_id}/analyses", response_model=AnalysisListOut)
def list_analyses(
    report_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> AnalysisListOut:
    """版本历史（最新在前）：每版带审计字段（prompt_version/model/tokens/耗时）。"""
    report = _get_active_report(db, report_id)
    items = db.scalars(
        select(Analysis)
        .where(Analysis.report_id == report.id)
        .order_by(Analysis.version.desc())
    ).all()
    return AnalysisListOut(
        items=[AnalysisOut.model_validate(a) for a in items],
        current_analysis_id=report.current_analysis_id,
    )


class AnalysisTargetOut(BaseModel):
    seq: int
    raw_name: str
    raw_code: str | None
    target_code: str | None
    target_name: str | None = None
    exchange: str | None = None
    sw_l1_name: str | None = None
    stance: str
    view: str
    has_forecast: bool
    code_source: str | None
    match_id: int | None
    match_status: str | None = None


class ReportTargetsOut(BaseModel):
    analysis_id: int | None
    items: list[AnalysisTargetOut]


@router.get("/{report_id}/targets", response_model=ReportTargetsOut)
def list_report_targets(
    report_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ReportTargetsOut:
    """当前分析的标的关联（#16 回写产物）：瀑布落成 + 队列状态，主数据名称/行业随行。"""
    report = _get_active_report(db, report_id)
    if report.current_analysis_id is None:
        return ReportTargetsOut(analysis_id=None, items=[])
    links = db.scalars(
        select(AnalysisTarget)
        .where(analysis.current_link(AnalysisTarget, report))
        .order_by(AnalysisTarget.seq)
    ).all()
    master = {
        t.code: t
        for t in db.scalars(
            select(TargetRow).where(TargetRow.code.in_([l.target_code for l in links if l.target_code]))
        )
    }
    matches = {
        m.id: m
        for m in db.scalars(
            select(TargetMatch).where(TargetMatch.id.in_([l.match_id for l in links if l.match_id]))
        )
    }
    items = []
    for l in links:
        row = master.get(l.target_code) if l.target_code else None
        match = matches.get(l.match_id) if l.match_id else None
        items.append(AnalysisTargetOut(
            seq=l.seq,
            raw_name=l.raw_name,
            raw_code=l.raw_code,
            target_code=l.target_code,
            target_name=row.name if row else None,
            exchange=row.exchange if row else None,
            sw_l1_name=row.sw_l1_name if row else None,
            stance=l.stance,
            view=l.view,
            has_forecast=l.has_forecast,
            code_source=l.code_source,
            match_id=l.match_id,
            match_status=match.status if match else None,
        ))
    return ReportTargetsOut(analysis_id=report.current_analysis_id, items=items)


# ---------- 自由 tag（#15） ----------

def _normalize_tag_name(name: str) -> str:
    """NFKC + 去空白：全角/半角与空格差异不打散词表。"""
    normalized = unicodedata.normalize("NFKC", name)
    return " ".join(normalized.split())


@router.post("/{report_id}/tags", response_model=TagOut)
def add_tag(
    report_id: int,
    body: TagCreate,
    response: Response,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> TagOut:
    """打 tag：按名复用全局 tag（自由轻量，无状态机）。已关联则幂等返回 200。"""
    report = _get_active_report(db, report_id)
    name = _normalize_tag_name(body.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="tag 名归一后为空"
        )
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name, created_by=user.id)
        db.add(tag)
        db.flush()
    linked = db.get(ReportTag, (report.id, tag.id)) is not None
    if not linked:
        db.add(ReportTag(report_id=report.id, tag_id=tag.id, created_by=user.id))
        db.flush()
    db.commit()
    response.status_code = (
        status.HTTP_200_OK if linked else status.HTTP_201_CREATED
    )
    return TagOut(id=tag.id, name=tag.name, linked=linked)


@router.delete("/{report_id}/tags/{tag_name}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag(
    report_id: int,
    tag_name: str,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> None:
    report = _get_active_report(db, report_id)
    name = _normalize_tag_name(tag_name)
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tag not found")
    link = db.get(ReportTag, (report.id, tag.id))
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tag not linked")
    db.delete(link)
    db.commit()
