"""worker：轮询共享任务表，按 kind 分派处理器。

转换（#13）分三阶段执行，每阶段完成即把 stages_done 落库：
  preflight（加密检测/解密重写 + 扫描闸门）→ convert（markitdown + 字符数闸门）
  → persist（清洗 + 写回 report_files）
失败任务可由 API 重置回 uploaded 重跑，已完成的阶段经 stages_done/产物探测跳过（分阶段重试）。

分析（#15）：convert 完成后链式进入 analyzing（LLM 未配置则记 skipped 后 done）；
reanalyze/批量重跑产生独立 analyze 任务（跳过转换，直接分析最近转换完成的文件）。
"""

import datetime as dt
import logging
import time
from typing import Callable

from sqlalchemy import and_, case, func, or_, select, update

from . import analysis, db
from .config import get_settings
from .conversion import (
    ConversionError,
    clean_markdown,
    convert_to_markdown,
    preflight_pdf,
)
from .errors import AnalysisError
from .models import ReportFile, ResearchReport, Task, TaskStatus
from .storage import get_storage

log = logging.getLogger("minereport.worker")


def claim_next_task() -> Task | None:
    """领取下一待处理任务（单条 UPDATE ... RETURNING 原子完成）。

    子查询 FOR UPDATE SKIP LOCKED：并发 worker 争抢时直接跳过已锁行取下一条，
    且锁释放后 EvalPlanQual 复检状态条件，不会重领已被改走状态的行；
    外层再挂一份状态复查兜底。CONVERTING/ANALYZING 超过租约（claimed_at 过旧）
    的任务视为 worker 遗弃，可重新领取；领取时按任务类型写入正确的在途状态
    （convert → converting，analyze → analyzing）。
    """
    s = get_settings()
    lease_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=s.worker_lease_seconds)
    in_flight = Task.status.in_((TaskStatus.CONVERTING, TaskStatus.ANALYZING))
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
    with db.SessionLocal() as session:
        row = session.execute(
            update(Task)
            .where(Task.id == candidate, eligible)
            .values(
                status=case(
                    (Task.kind == "analyze", TaskStatus.ANALYZING),
                    else_=TaskStatus.CONVERTING,
                ),
                attempts=Task.attempts + 1,
                claimed_at=func.now(),
            )
            .returning(Task)
        ).scalar_one_or_none()
        session.commit()
        return row


# ---------- 任务处理器注册表 ----------

HANDLERS: dict[str, Callable[[int], None]] = {}


def _mark_failed(task_id: int, error_code: str, message: str, stage: str | None) -> None:
    with db.SessionLocal() as s:
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
        handler = HANDLERS.get(task.kind)
        if handler is None:
            _mark_failed(task.id, "unknown_kind", f"no handler for kind {task.kind!r}", None)
        else:
            handler(task.id)
    except ConversionError as e:
        _mark_failed(task.id, e.error_code, e.message, getattr(e, "stage", None))
    except AnalysisError as e:
        _mark_failed(task.id, e.error_code, e.message, e.stage or "analyze")
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
    with db.SessionLocal() as session:
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


HANDLERS["convert"] = handle_convert


# ---------- analyze：独立分析任务（reanalyze / 批量重跑） ----------

def handle_analyze(task_id: int) -> None:
    """跳过转换，直接分析 payload 指定文件（缺省取最近转换完成者）。
    失败不留半版本：Analysis 行仅在提取归一全部成功后插入。"""
    with db.SessionLocal() as session:
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

        task.status = TaskStatus.ANALYZING
        session.commit()
        a = analysis.run_analysis(session, report, file)
        task.status = TaskStatus.DONE
        task.result = {
            "report_id": report.id,
            "analysis": {"analysis_id": a.id, "version": a.version},
        }
        session.commit()


HANDLERS["analyze"] = handle_analyze


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    s = get_settings()
    log.info("worker %s 启动：轮询间隔 %.1fs，心跳间隔 %.0fs", s.worker_id, s.worker_poll_interval, s.worker_heartbeat_interval)
    last_heartbeat = dt.datetime.now() - dt.timedelta(seconds=s.worker_heartbeat_interval)
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


if __name__ == "__main__":
    main()
