"""订阅调度器（#19）：APScheduler 定时扫 due 订阅，编排 discover → 去重 → 下载 → 入库。

去重是平台共通策略，收敛在此层（连接器不各写一份）：
1. ExternalRef(connector_id, external_id) 唯一——同报告被多关键词/多轮重复命中只处理一次；
2. ADR-0001 组合键——外部报告与库内既有研报同（标题归一, 券商, 日期）时只挂引用
   不下载（省下载额度）。

失败语义：单轮失败（discover/网络等）指数退避重试 3 次（1m/5m/25m）→ 死信入连接器
日志；连续 5 轮死信告警。单条 fetch 失败不杀整轮，只落 ref.last_error 供重试/人工。
pubTime 增量：cursor_pubtime 游标 − 1h 余量（平台索引延迟容忍，重复由 ExternalRef
兜底），窗口上限 7 天（防长停后一次性深翻页）。
"""

from __future__ import annotations

import datetime as dt
import logging
import random

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from . import db
from .config import get_settings
from .connectors import Connector, ReportRef, build_connector
from .conversion import normalize_title
from .errors import ConnectorError, PipelineError
from .ingest import find_active_by_identity, ingest_report_file
from .models import ConnectorRun, ExternalRef, ResearchReport, Subscription, Task, Theme
from .themes import normalize_theme_name

log = logging.getLogger("minereport.scheduler")

BACKOFF_SECONDS = (60.0, 300.0, 1500.0)  # 1m / 5m / 25m（spec 定案）
MAX_RETRIES = len(BACKOFF_SECONDS)
ALERT_AFTER_FAILURES = 5  # 连续死信轮数阈值（spec：连续 5 轮失败告警）
MAX_QUERIES = 8  # 单轮查询词上限（限速 1 req/s 下控制单轮时长）
MAX_WINDOW = dt.timedelta(days=7)  # 游标时间窗上限
CURSOR_OVERLAP = dt.timedelta(hours=1)  # 游标回看余量


class RunEvent:
    RUN = "run"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    ALERT = "alert"
    MANUAL_DOWNLOAD = "manual_download"


class RefStatus:
    SEEN = "seen"
    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    FETCH_FAILED = "fetch_failed"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def expand_queries(theme: Theme | None, extra_keywords: list | None) -> list[str]:
    """题材名 + 同义词 + 订阅额外词 → 去重保序的查询词表（同义词治理红利直接变召回率）。"""
    words: list[str] = []
    if theme is not None:
        words.append(theme.name)
        words.extend(theme.synonyms or [])
    words.extend(extra_keywords or [])
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        norm = normalize_theme_name(w)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out[:MAX_QUERIES]


def _window_start(sub: Subscription, now: dt.datetime) -> dt.datetime | None:
    """增量游标 → discover 的 since；无游标返回 None（连接器用默认窗口 last3day）。"""
    if sub.cursor_pubtime is None:
        return None
    return max(sub.cursor_pubtime - CURSOR_OVERLAP, now - MAX_WINDOW)


def _next_run_at(sub: Subscription, now: dt.datetime) -> dt.datetime:
    """下一轮时刻：interval + 错峰抖动（±settings.subscription_jitter_seconds）。"""
    jitter = get_settings().subscription_jitter_seconds
    delta = random.uniform(-jitter, jitter) if jitter > 0 else 0.0
    return now + dt.timedelta(seconds=sub.interval_hours * 3600 + delta)


def _log_run(
    session: OrmSession, sub: Subscription, event: str, *, ok: bool, message: str, stats: dict | None
) -> None:
    session.add(
        ConnectorRun(
            connector_id=sub.connector_id,
            subscription_id=sub.id,
            event=event,
            ok=ok,
            message=message,
            stats=stats,
        )
    )


def _ingest_fetched(
    session: OrmSession,
    row: ExternalRef,
    connector: Connector,
    ref: ReportRef,
    *,
    user_id: int,
    now: dt.datetime,
) -> tuple[ResearchReport, Task]:
    """fetch → 入库回调 → ref 置 ingested（下载 URL 带时效拿到即下载；额度在此消耗）。"""
    fetched = connector.fetch(ref)
    report, _file, task, _merged = ingest_report_file(
        session,
        title=ref.title,
        broker=ref.broker or "",
        publish_date=ref.publish_date,
        filename=fetched.filename,
        content_type=fetched.content_type,
        data=fetched.data,
        user_id=user_id,
    )
    row.status = RefStatus.INGESTED
    row.report_id = report.id
    row.fetched_at = now
    return report, task


def _handle_ref(
    session: OrmSession, sub: Subscription, connector: Connector, ref: ReportRef, stats: dict, now: dt.datetime
) -> None:
    """单条命中：ExternalRef 认领 → 组合键查重 → 按需下载入库（逐条 commit，失败不互相牵连）。"""
    existing = session.scalar(
        select(ExternalRef).where(
            ExternalRef.connector_id == sub.connector_id,
            ExternalRef.external_id == ref.external_id,
        )
    )
    if existing is not None:
        stats["seen"] += 1
        return

    row = ExternalRef(
        connector_id=sub.connector_id,
        external_id=ref.external_id,
        status=RefStatus.SEEN,
        title=ref.title[:512],
        broker=(ref.broker or None),
        publish_date=ref.publish_date,
        industry=ref.industry,
        pages=ref.pages,
        snippet=ref.snippet,
        subscription_id=sub.id,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        # 并发认领撞唯一索引：另一轮已处理，视为已见
        session.rollback()
        stats["seen"] += 1
        return

    stats["new"] += 1
    try:
        dup = find_active_by_identity(
            session, normalize_title(ref.title), ref.broker or "", ref.publish_date
        )
        if dup is not None:
            row.status = RefStatus.DUPLICATE
            row.report_id = dup.id
            stats["duplicates"] += 1
        elif sub.auto_download:
            _ingest_fetched(session, row, connector, ref, user_id=sub.created_by, now=now)
            stats["downloaded"] += 1  # 额度消耗口径
            stats["ingested"] += 1
        else:
            stats["pending"] += 1  # auto_download 关：留待手动下载
    except PipelineError as e:
        row.status = RefStatus.FETCH_FAILED
        row.last_error = f"{e.error_code}: {e.message}"
        stats["fetch_failed"] += 1

    if ref.published_at is not None and (sub.cursor_pubtime is None or ref.published_at > sub.cursor_pubtime):
        sub.cursor_pubtime = ref.published_at
    session.commit()


def _record_failure(
    session: OrmSession, sub: Subscription, error: Exception, now: dt.datetime, stats: dict | None = None
) -> None:
    """失败落账：退避重试 or 死信；连续死信达 5 的倍数轮时告警。

    stats 为本轮已产生的计数（discover 中段失败时前面已真实下载过）：随事件落账，
    额度汇总不漏记。
    """
    message = (
        f"{error.error_code}: {error.message}" if isinstance(error, PipelineError) else repr(error)[:500]
    )
    sub.last_run_at = now
    sub.attempt += 1
    if sub.attempt <= MAX_RETRIES:
        delay = BACKOFF_SECONDS[sub.attempt - 1]
        sub.next_run_at = now + dt.timedelta(seconds=delay)
        _log_run(session, sub, RunEvent.RETRY, ok=False, message=message, stats=dict(stats) if stats else None)
    else:
        sub.attempt = 0
        sub.consecutive_failures += 1
        sub.next_run_at = _next_run_at(sub, now)
        _log_run(
            session, sub, RunEvent.DEAD_LETTER, ok=False, message=message,
            stats={"consecutive_failures": sub.consecutive_failures},
        )
        if sub.consecutive_failures >= ALERT_AFTER_FAILURES and sub.consecutive_failures % ALERT_AFTER_FAILURES == 0:
            _log_run(
                session, sub, RunEvent.ALERT, ok=False,
                message=f"订阅连续 {sub.consecutive_failures} 轮死信，请检查连接器 {sub.connector_id}",
                stats=None,
            )
    session.commit()


def run_subscription(
    subscription_id: int, *, connector: Connector | None = None, now: dt.datetime | None = None
) -> dict:
    """执行一轮订阅（手动"立即运行"与定时 tick 共用入口）。返回本轮 stats。

    单轮失败不改抛：退避/死信语义在 _record_failure 落账（返回 stats 带 failed 标记）。
    """
    now = now or utcnow()
    with db.SessionLocal() as session:
        sub = session.get(Subscription, subscription_id)
        if sub is None:
            raise ValueError(f"subscription {subscription_id} 不存在")
        try:
            conn = connector or build_connector(sub.connector_id)
        except ConnectorError as e:
            _record_failure(session, sub, e, now)
            return {"failed": True, "error": e.error_code}
        theme = session.get(Theme, sub.theme_id)
        queries = expand_queries(theme, sub.keywords)
        stats = {
            "queries": queries,
            "found": 0, "new": 0, "seen": 0, "duplicates": 0,
            "downloaded": 0, "ingested": 0, "pending": 0, "fetch_failed": 0,
        }
        try:
            since = _window_start(sub, now)
            for query in queries:
                refs = conn.discover(query, since, orgs=sub.orgs)
                stats["found"] += len(refs)
                for ref in refs:
                    _handle_ref(session, sub, conn, ref, stats, now)
            sub.last_run_at = now
            sub.last_success_at = now
            sub.attempt = 0
            sub.consecutive_failures = 0
            sub.next_run_at = _next_run_at(sub, now)
            _log_run(session, sub, RunEvent.RUN, ok=True, message=f"{len(queries)} 组查询词完成一轮", stats=dict(stats))
            session.commit()
            return stats
        except Exception as e:  # noqa: BLE001 - 单轮失败统一落账退避，不让 tick 退出
            log.exception("订阅 %s 第 %s 次尝试失败", sub.id, sub.attempt + 1)
            session.rollback()
            sub = session.get(Subscription, subscription_id)
            _record_failure(session, sub, e, now, stats=stats)
            stats["failed"] = True
            return stats


def tick(*, now: dt.datetime | None = None) -> int:
    """扫 due 订阅逐个执行（enabled 且 next_run_at 到期）；返回本轮执行的订阅数。

    worker 的 APScheduler 定时调用；单订阅异常已内部消化，这里只兜底防级联。
    """
    now = now or utcnow()
    with db.SessionLocal() as session:
        due_ids = [
            sid for (sid,) in session.execute(
                select(Subscription.id).where(
                    Subscription.enabled.is_(True),
                    Subscription.next_run_at.is_not(None),
                    Subscription.next_run_at <= now,
                ).order_by(Subscription.id)
            )
        ]
    for sid in due_ids:
        try:
            run_subscription(sid, now=now)
        except Exception:  # noqa: BLE001 - tick 永不因单个订阅中断
            log.exception("tick 执行订阅 %s 异常", sid)
    return len(due_ids)


def schedule_first_run(sub: Subscription, now: dt.datetime | None = None) -> None:
    """建订阅时排首轮：now + 随机错峰（0..jitter），避免同批订阅齐发打满限速。"""
    now = now or utcnow()
    jitter = get_settings().subscription_jitter_seconds
    sub.next_run_at = now + dt.timedelta(seconds=random.uniform(0, jitter) if jitter > 0 else 0.0)


def manual_download(
    session: OrmSession, ref: ExternalRef, *, connector: Connector | None = None, user_id: int, now: dt.datetime | None = None
) -> tuple[ExternalRef, ResearchReport, Task]:
    """手动单篇下载（额度提示后的确认动作）：fetch → 入库 → ref 状态落账 + 额度日志。

    duplicate/ingested 状态幂等返回（不重复扣额度，task 为 None）；fetch 失败抛
    ConnectorError 由调用方落 ref.last_error。返回 (ref, report, task)；不 commit。
    """
    now = now or utcnow()
    if ref.status in (RefStatus.DUPLICATE, RefStatus.INGESTED):
        assert ref.report_id is not None
        return ref, session.get(ResearchReport, ref.report_id), None
    conn = connector or build_connector(ref.connector_id)
    report_ref = ReportRef(
        external_id=ref.external_id, title=ref.title, publish_date=ref.publish_date,
        published_at=None, broker=ref.broker, industry=ref.industry, pages=ref.pages,
    )
    report, task = _ingest_fetched(session, ref, conn, report_ref, user_id=user_id, now=now)
    ref.last_error = None
    session.add(
        ConnectorRun(
            connector_id=ref.connector_id,
            subscription_id=ref.subscription_id,
            event=RunEvent.MANUAL_DOWNLOAD,
            ok=True,
            message=f"手动下载 {ref.external_id}：{ref.title[:100]}",
            stats={"downloaded": 1, "by": user_id},
        )
    )
    return ref, report, task
