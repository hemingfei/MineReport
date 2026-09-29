"""worker：轮询共享任务表，按 kind 分派处理器。

转换（#13）分三阶段执行，每阶段完成即把 stages_done 落库：
  preflight（加密检测/解密重写 + 扫描闸门）→ convert（markitdown + 字符数闸门）
  → persist（清洗 + 写回 report_files）
失败任务可由 API 重置回 uploaded 重跑，已完成的阶段经 stages_done/产物探测跳过（分阶段重试）。

分析（#15）：convert 完成后链式进入 analyzing（LLM 未配置则记 skipped 后 done）；
reanalyze/批量重跑产生独立 analyze 任务（跳过转换，直接分析最近转换完成的文件）。

订阅调度（#19）：main() 里另起 APScheduler 定时扫 due 订阅（连接器拉取/去重/退避
编排见 app/scheduler.py）；调度与任务轮询互不阻塞，重启各自恢复。

综合分析（#20）：synthesize 任务按 POST 时点的快照研报集合跑题材二次分析
（缓存判据在入队时完成；LLM 编排见 app/synthesis.py）。
"""

import datetime as dt
import logging
import time
from typing import Callable, NamedTuple

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session as OrmSession

from . import analysis, db, masterdata, search, synthesis, themes
from .config import get_settings
from .conversion import (
    ConversionError,
    clean_markdown,
    convert_to_markdown,
    preflight_pdf,
)
from .errors import AnalysisError, MasterDataError
from .models import ReportFile, ResearchReport, Task, TaskStatus, Theme
from .storage import get_storage

log = logging.getLogger("minereport.worker")


# ---------- 任务注册表与入队口 ----------


class TaskSpec(NamedTuple):
    """kind 注册项：处理器 + 领取后进入的在途状态（兼任租约标记）。

    新增任务类型只改这里：入队校验、claim 的状态写入与租约恢复集合
    全部由本注册表渲染，别处不再持有 kind 词表。
    """

    handler: Callable[[int], None]
    inflight_status: str


HANDLERS: dict[str, TaskSpec] = {}


def enqueue(session: OrmSession, kind: str, payload: dict | None = None) -> Task:
    """唯一入队口：校验 kind 已注册后落 UPLOADED 任务（不 commit，调用方决定时机）。"""
    if kind not in HANDLERS:
        raise ValueError(f"unregistered task kind {kind!r}")
    task = Task(kind=kind, status=TaskStatus.UPLOADED, payload=payload)
    session.add(task)
    return task


def inflight_statuses() -> tuple[str, ...]:
    """注册表声明的全部在途状态（去重保序）：claim 租约恢复只认这些。"""
    return tuple(dict.fromkeys(spec.inflight_status for spec in HANDLERS.values()))


def claim_next_task() -> Task | None:
    """领取下一待处理任务（单条 UPDATE ... RETURNING 原子完成）。

    子查询 FOR UPDATE SKIP LOCKED：并发 worker 争抢时直接跳过已锁行取下一条，
    外层再挂一份状态复查兜底。在途状态超过租约（claimed_at 过旧）的任务视为
    worker 遗弃，可重新领取；领取时按注册表（HANDLERS）声明的 inflight_status
    写入对应状态。
    """
    s = get_settings()
    lease_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=s.worker_lease_seconds)
    in_flight = Task.status.in_(inflight_statuses())
    eligible = or_(
        Task.status == TaskStatus.UPLOADED,
        and_(in_flight, Task.claimed_at < lease_cutoff),
    )
    candidate = (
        select(Task.id)
        .where(eligible)
        .order_by(Task.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    with db.session_scope() as session:
        row = session.execute(
            update(Task)
            .where(Task.id == candidate, eligible)
            .values(
                status=case(
                    *((Task.kind == kind, spec.inflight_status) for kind, spec in HANDLERS.items()),
                    else_=TaskStatus.RUNNING,
                ),
                attempts=Task.attempts + 1,
                claimed_at=func.now(),
            )
            .returning(Task)
        ).scalar_one_or_none()
        session.commit()
        return row


def _mark_failed(task_id: int, error_code: str, message: str, stage: str | None) -> None:
    with db.session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            return
        t.status = TaskStatus.FAILED
        t.result = {"error_code": error_code, "error": message, "stage": stage}
        s.commit()


def run_once() -> Task | None:
    """单轮：领取任务并按 kind 分派；失败落库（error_code 可编程判断），不让轮询循环退出。"""
    task = claim_next_task()
    if task is None:
        return None
    try:
        spec = HANDLERS.get(task.kind)
        if spec is None:
            _mark_failed(task.id, "unknown_kind", f"no handler for kind {task.kind!r}", None)
        else:
            spec.handler(task.id)
    except ConversionError as e:
        _mark_failed(task.id, e.error_code, e.message, getattr(e, "stage", None))
    except AnalysisError as e:
        _mark_failed(task.id, e.error_code, e.message, e.stage or "analyze")
    except MasterDataError as e:
        _mark_failed(task.id, e.error_code, e.message, task.kind)
    except Exception:
        log.exception("task %s 处理异常", task.id)
        _mark_failed(task.id, "internal", "处理异常，详见 worker 日志", None)
    return task


# ---------- convert：转换管道（三阶段） ----------

class ConvertStage:
    """convert 任务的三阶段名（payload.stages_done 与 result.stage 的词表）。"""

    PREFLIGHT = "preflight"
    CONVERT = "convert"
    PERSIST = "persist"
    ORDER = (PREFLIGHT, CONVERT, PERSIST)


def _decrypted_key(storage_key: str) -> str:
    return f"{storage_key}.decrypted.pdf"


def _raw_markdown_key(storage_key: str) -> str:
    return f"{storage_key}.raw.md"


def _save_stage(task: Task, stages: set[str]) -> None:
    task.payload = {**(task.payload or {}), "stages_done": sorted(stages)}


def handle_convert(task_id: int) -> None:
    storage = get_storage()
    s = get_settings()
    with db.session_scope() as session:
        task = session.get(Task, task_id)
        payload = dict(task.payload or {})
        file = session.get(ReportFile, payload["report_file_id"])
        if file is None:
            raise ConversionError("file_missing", f"report_file {payload['report_file_id']} 不存在")
        stages = set(payload.get("stages_done") or [])

        try:
            # 阶段 preflight：加密检测（空密码解密重写）+ 扫描版字符数闸门
            if ConvertStage.PREFLIGHT not in stages:
                original = storage.get(file.storage_key)
                if file.filename.lower().endswith(".pdf"):
                    preflighted = preflight_pdf(original, file.filename)
                    if preflighted is not original:
                        storage.put(_decrypted_key(file.storage_key), preflighted)
                stages.add(ConvertStage.PREFLIGHT)
                _save_stage(task, stages)
                session.commit()

            # 阶段 convert：markitdown + 输出字符数闸门（markitdown 静默空串防护）
            raw_key = _raw_markdown_key(file.storage_key)
            if ConvertStage.CONVERT not in stages or not storage.exists(raw_key):
                decrypted = _decrypted_key(file.storage_key)
                src_key = decrypted if storage.exists(decrypted) else file.storage_key
                raw = convert_to_markdown(storage.get(src_key), file.filename)
                if len(raw.strip()) < s.markdown_min_chars:
                    raise ConversionError(
                        "empty_output",
                        f"转换输出仅 {len(raw.strip())} 字符（低于闸门 {s.markdown_min_chars}），"
                        "疑似扫描版或空文档",
                    )
                storage.put(raw_key, raw.encode("utf-8"))
                stages.add(ConvertStage.CONVERT)
                _save_stage(task, stages)
                session.commit()

            # 阶段 persist：清洗落库（先提交——分析失败不回滚转换成果，重试时三阶段全跳过）
            raw = storage.get(raw_key).decode("utf-8")
            file.markdown_text = clean_markdown(raw)
            file.converted_at = func.now()
            search.refresh_search_vector(session, file.report_id)  # #18：正文随转换完成入索引
            task.result = {
                "report_id": file.report_id,
                "report_file_id": file.id,
                "chars_raw": len(raw),
                "chars_cleaned": len(file.markdown_text),
            }
            session.commit()

            # 链式分析（spec 状态机 converting→analyzing→done）：LLM 未配置或研报已被
            # 软删则记 skipped（转换成果不受影响；与 handle_analyze 的删除检查保持一致）
            report = session.get(ResearchReport, file.report_id)
            if analysis.llm_ready() and report is not None and report.deleted_at is None:
                task.status = TaskStatus.ANALYZING
                session.commit()
                a = analysis.run_analysis(session, report, file)
                task.result = {**task.result, "analysis": {"analysis_id": a.id, "version": a.version}}
            else:
                reason = (
                    "report_deleted" if analysis.llm_ready() else "llm_not_configured"
                )
                task.result = {**task.result, "analysis": {"skipped": reason}}
            task.status = TaskStatus.DONE
            session.commit()
        except ConversionError as e:
            e.stage = e.stage or _current_stage(stages)
            raise


def _current_stage(stages: set[str]) -> str:
    """失败时报告未完成的阶段（已完成的阶段数即失败点）。"""
    for stage in ConvertStage.ORDER:
        if stage not in stages:
            return stage
    return ConvertStage.PERSIST


HANDLERS["convert"] = TaskSpec(handle_convert, TaskStatus.CONVERTING)


# ---------- analyze：独立分析任务（reanalyze / 批量重跑） ----------

def handle_analyze(task_id: int) -> None:
    """跳过转换，直接分析 payload 指定文件（缺省取最近转换完成者）。
    失败不留半版本：Analysis 行仅在提取归一全部成功后插入。"""
    with db.session_scope() as session:
        task = session.get(Task, task_id)
        payload = dict(task.payload or {})
        report = session.get(ResearchReport, payload["report_id"])
        if report is None or report.deleted_at is not None:
            raise AnalysisError("report_missing", f"研报 {payload['report_id']} 不存在或已删除")

        file_id = payload.get("report_file_id")
        if file_id is not None:
            file = session.get(ReportFile, file_id)
            if file is None or file.report_id != report.id:
                raise AnalysisError("file_missing", f"report_file {file_id} 不属于该研报")
        else:
            file = analysis.latest_converted_file(session, report.id)
            if file is None:
                raise AnalysisError("markdown_missing", "研报尚无转换完成的文件，无法分析")

        a = analysis.run_analysis(session, report, file)
        task.status = TaskStatus.DONE
        task.result = {
            "report_id": report.id,
            "analysis": {"analysis_id": a.id, "version": a.version},
        }
        session.commit()


HANDLERS["analyze"] = TaskSpec(handle_analyze, TaskStatus.ANALYZING)


# ---------- synthesize：综合分析（#20） ----------

def handle_synthesize(task_id: int) -> None:
    """按 POST 时点的快照研报集合跑题材综合分析（缓存判据已在入队时完成）。

    题材在排队期间被合并/停用即失败（关联语义已变，重走 POST 重新选集；
    判据与 API 共用 synthesis.theme_unavailable_reason）；快照成员的可用性
    校验与证据边界记录见 synthesis.run_synthesis。"""
    with db.session_scope() as session:
        task = session.get(Task, task_id)
        payload = dict(task.payload or {})
        theme = session.get(Theme, payload["theme_id"])
        if theme is None:
            raise AnalysisError(
                "theme_unavailable", f"题材 {payload['theme_id']} 不存在，请重新发起"
            )
        if reason := synthesis.theme_unavailable_reason(theme):
            raise AnalysisError("theme_unavailable", f"{reason}，请重新发起")
        report_ids: list[int] = payload["report_ids"]

        row = synthesis.run_synthesis(
            session,
            theme,
            report_ids,
            created_by=payload["triggered_by"],
            input_total=int(payload.get("input_total") or len(report_ids)),
        )
        task.status = TaskStatus.DONE
        task.result = {
            "synthesis_id": row.id,
            "theme_id": theme.id,
            "version": row.version,
            "report_count": len(row.report_ids or []),
            "input_fingerprint": row.input_fingerprint,
        }
        session.commit()


HANDLERS["synthesize"] = TaskSpec(handle_synthesize, TaskStatus.ANALYZING)


# ---------- import_targets：标的主数据全量导入（#16） ----------

def handle_import_targets(task_id: int) -> None:
    """akshare 全量 + 申万 xls 全史 → 幂等 upsert；with_name_history 开启时慢速回填曾用名。"""
    with db.session_scope() as session:
        task = session.get(Task, task_id)
        payload = dict(task.payload or {})
        stocks, industry_rows = masterdata.fetch_all()
        name_changes = None
        if payload.get("with_name_history"):
            name_changes = masterdata.fetch_name_changes([s.code for s in stocks])
        stats = masterdata.import_master_data(session, stocks, industry_rows, name_changes)
        task.status = TaskStatus.DONE
        task.result = stats
        session.commit()


HANDLERS["import_targets"] = TaskSpec(handle_import_targets, TaskStatus.RUNNING)


# ---------- import_themes：题材种子导入（#17） ----------

def handle_import_themes(task_id: int) -> None:
    """东财概念（仓库内置快照）+ 申万二级（静态码表 + 主数据快照）→ 幂等 upsert。

    均零网络：东财 push2 对高频请求按 IP 断连，部署机在线抓取不可靠——快照由
    本地脚本 scripts/refresh_em_concept_snapshot.py 产出、提交后随版本发布。
    依赖标的主数据已导入（成员 FK 指向 targets）；主数据未导时种子题材照建、
    成分为空，主数据导入后重跑即可补齐。
    """
    with db.session_scope() as session:
        task = session.get(Task, task_id)
        em_seeds = themes.load_em_concept_seeds()
        sw_seeds = themes.fetch_sw_l2_seeds(session)
        stats = themes.import_theme_seeds(session, em_seeds + sw_seeds)
        task.status = TaskStatus.DONE
        task.result = stats
        session.commit()


HANDLERS["import_themes"] = TaskSpec(handle_import_themes, TaskStatus.RUNNING)


def start_subscription_scheduler() -> "BackgroundScheduler | None":
    """APScheduler（spec：调度进 worker 容器）：定时扫 due 订阅驱动连接器拉取。

    tick 间隔 settings.subscription_tick_seconds（默认 60s）；单订阅执行语义与
    退避/死信见 app/scheduler.py。启动失败不阻断轮询主循环（调度是增值面）。
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:  # pragma: no cover - 依赖缺失时降级为纯任务轮询
        log.warning("apscheduler 未安装，订阅调度未启动")
        return None
    s = get_settings()
    sched = BackgroundScheduler(timezone=dt.timezone.utc)
    sched.add_job(
        lambda: _safe_tick(),
        "interval",
        seconds=s.subscription_tick_seconds,
        max_instances=1,
        coalesce=True,
        id="subscription-tick",
    )
    sched.start()
    log.info("订阅调度已启动：tick 间隔 %.0fs", s.subscription_tick_seconds)
    return sched


def _safe_tick() -> None:
    from . import scheduler

    try:
        scheduler.tick()
    except Exception:  # noqa: BLE001 - 调度轮异常不杀 APScheduler 线程
        log.exception("subscription tick 异常")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    s = get_settings()
    log.info("worker %s 启动：轮询间隔 %.1fs，心跳间隔 %.0fs", s.worker_id, s.worker_poll_interval, s.worker_heartbeat_interval)
    sched = start_subscription_scheduler()
    last_heartbeat = dt.datetime.now() - dt.timedelta(seconds=s.worker_heartbeat_interval)
    try:
        while True:
            task = None
            try:
                task = run_once()
            except Exception:
                # 瞬时故障（DB 重启/死锁/网络抖动）不退出进程，退避后继续轮询
                log.exception("run_once 异常，退避后继续轮询")
                time.sleep(min(s.worker_poll_interval * 5, 30.0))
            now = dt.datetime.now()
            if (now - last_heartbeat).total_seconds() >= s.worker_heartbeat_interval:
                log.info("heartbeat: worker %s alive, poll interval %.1fs", s.worker_id, s.worker_poll_interval)
                last_heartbeat = now
            if task is None:
                time.sleep(s.worker_poll_interval)
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
