"""入库回调（#19 从上传端点抽取）：手动上传与连接器拉取走同一套管道。

ADR-0001 组合键找重 → 建研报/挂文件（sha256 仅属性）→ 建 convert 任务 →
刷新全文索引。不提交事务：调用方（API 端点 / 调度器）决定提交时机。
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from . import search
from .conversion import normalize_title
from .models import ReportFile, ResearchReport, Task, TaskStatus
from .storage import Storage, get_storage


def find_active_by_identity(
    db: OrmSession,
    title_norm: str,
    broker: str,
    publish_date: dt.date,
    *,
    exclude_id: int | None = None,
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


def find_or_create_report(
    db: OrmSession, title: str, broker: str, publish_date: dt.date, user_id: int
) -> tuple[ResearchReport, bool]:
    """组合键找既有研报（命中即并入）；并发撞唯一索引时回读并入（IntegrityError 兜底）。"""
    title_norm = normalize_title(title)
    existing = find_active_by_identity(db, title_norm, broker, publish_date)
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
        existing = find_active_by_identity(db, title_norm, broker, publish_date)
        if existing is None:
            raise
        return existing, True
    return report, False


def ingest_report_file(
    db: OrmSession,
    *,
    title: str,
    broker: str,
    publish_date: dt.date,
    filename: str,
    content_type: str | None,
    data: bytes,
    user_id: int,
    storage: Storage | None = None,
) -> tuple[ResearchReport, ReportFile, Task, bool]:
    """研报 + 文件 + convert 任务一步落库（不 commit）；返回 (report, file, task, merged)。"""
    report, merged = find_or_create_report(db, title, broker, publish_date, user_id)
    file_row = ReportFile(
        report_id=report.id,
        storage_key="",  # 先占位，flush 拿 id 后回填
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        file_sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=user_id,
    )
    db.add(file_row)
    db.flush()
    file_row.storage_key = f"reports/{report.id}/files/{file_row.id}/{filename}"
    (storage or get_storage()).put(file_row.storage_key, data)

    task = Task(
        kind="convert",
        status=TaskStatus.UPLOADED,
        payload={"report_id": report.id, "report_file_id": file_row.id},
    )
    db.add(task)
    search.refresh_search_vector(db, report.id)  # 建档即索引标题（正文/总结随转换/分析补）
    return report, file_row, task, merged
