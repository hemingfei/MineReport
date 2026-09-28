"""研报 API（#13）：上传（202+task）、列表过滤分页、详情/正文/文件、软删与恢复。

权限矩阵（spec）：
- 上传/删除/恢复：analyst 起（删除/恢复 analyst 仅自己的，admin 任意）
- 浏览/正文：任意登录用户
- 原始文件下载：analyst 起；reader 默认 403，ALLOW_READER_DOWNLOAD=true 放开
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from ..auth import require_role
from ..config import get_settings
from ..conversion import SUPPORTED_EXTENSIONS, normalize_title
from ..db import get_db
from ..models import ReportFile, ResearchReport, Role, Task, TaskStatus, User
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


def _find_active_by_identity(
    db: OrmSession, title_norm: str, broker: str, publish_date: dt.date, *, exclude_id: int | None = None
) -> ResearchReport | None:
    """ADR-0001 组合键查找（仅未删除行；exclude_id 供恢复撞键检查排除自身）。"""
    conds = [
        ResearchReport.title_norm == title_norm,
        ResearchReport.broker == broker,
        ResearchReport.publish_date == publish_date,
        ResearchReport.deleted_at.is_(None),
    ]
    if exclude_id is not None:
        conds.append(ResearchReport.id != exclude_id)
    return db.scalar(select(ResearchReport).where(*conds))


def _find_or_create_report(
    db: OrmSession, title: str, broker: str, publish_date: dt.date, user_id: int
) -> tuple[ResearchReport, bool]:
    """组合键找既有研报；并发撞唯一索引时回读并入（IntegrityError 兜底）。"""
    title_norm = normalize_title(title)
    existing = _find_active_by_identity(db, title_norm, broker, publish_date)
    if existing is not None:
        return existing, True

    report = ResearchReport(
        title=title, title_norm=title_norm, broker=broker,
        publish_date=publish_date, created_by=user_id,
    )
    db.add(report)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _find_active_by_identity(db, title_norm, broker, publish_date)
        if existing is None:
            raise
        return existing, True
    return report, False


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

    # 流式读入（同时算 sha256）后再校验大小：超限即拒，不落盘
    chunks: list[bytes] = []
    sha = hashlib.sha256()
    size = 0
    while chunk := await file.read(_READ_CHUNK):
        size += len(chunk)
        if size > s.upload_max_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"文件超过 {s.upload_max_mb}MB 上限",
            )
        sha.update(chunk)
        chunks.append(chunk)
    data = b"".join(chunks)

    report, merged = _find_or_create_report(
        db, title or Path(filename).stem, broker.strip(), publish_date, user.id
    )

    file_row = ReportFile(
        report_id=report.id,
        storage_key="",  # 先占位，flush 拿 id 后回填
        filename=filename,
        content_type=file.content_type,
        size_bytes=size,
        file_sha256=sha.hexdigest(),
        uploaded_by=user.id,
    )
    db.add(file_row)
    db.flush()
    file_row.storage_key = f"reports/{report.id}/files/{file_row.id}/{filename}"
    get_storage().put(file_row.storage_key, data)

    task = Task(
        kind="convert",
        status=TaskStatus.UPLOADED,
        payload={"report_id": report.id, "report_file_id": file_row.id},
    )
    db.add(task)
    db.commit()
    return ReportCreateOut(
        task_id=task.id, report_id=report.id, file_id=file_row.id, merged=merged
    )


@router.get("", response_model=ReportListOut)
def list_reports(
    broker: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> ReportListOut:
    """列表：券商精确匹配 + 发布日期闭区间过滤 + limit/offset 分页。

    题材/tag/标的/搜索词过滤由后续票接入（工单 #13 明确不在本票范围）。
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    conds = [ResearchReport.deleted_at.is_(None)]
    if broker:
        conds.append(ResearchReport.broker == broker)
    if date_from:
        conds.append(ResearchReport.publish_date >= date_from)
    if date_to:
        conds.append(ResearchReport.publish_date <= date_to)

    total = db.scalar(select(func.count()).select_from(ResearchReport).where(*conds))
    reports = db.scalars(
        select(ResearchReport)
        .where(*conds)
        .order_by(ResearchReport.publish_date.desc(), ResearchReport.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        ReportOut(
            id=r.id, title=r.title, broker=r.broker, publish_date=r.publish_date,
            created_by=r.created_by, created_at=r.created_at,
            files=[
                ReportFileOut.model_validate(f)
                for f in db.scalars(
                    select(ReportFile)
                    .where(ReportFile.report_id == r.id)
                    .order_by(ReportFile.id)
                )
            ],
        )
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
    return ReportOut(
        id=report.id, title=report.title, broker=report.broker,
        publish_date=report.publish_date, created_by=report.created_by,
        created_at=report.created_at, files=[ReportFileOut.model_validate(f) for f in files],
    )


@router.get("/{report_id}/markdown")
def get_report_markdown(
    report_id: int,
    user: User = Depends(require_role(Role.READER)),
    db: OrmSession = Depends(get_db),
) -> Response:
    """正文 markdown：取最近一个转换完成的文件（多来源文件后到优先）。"""
    _get_active_report(db, report_id)
    file = db.scalar(
        select(ReportFile)
        .where(ReportFile.report_id == report_id, ReportFile.converted_at.is_not(None))
        .order_by(ReportFile.converted_at.desc(), ReportFile.id.desc())
        .limit(1)
    )
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
