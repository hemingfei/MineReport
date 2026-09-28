"""worker：轮询共享任务表的骨架。

转换/分析/订阅调度后续都在这里跑。骨架期任务处理器只有注册表 + 心跳；
拿到任务即置 done（payload 回显），让端到端链路（API 建任务 → worker 消费）可观测。
"""

import datetime as dt
import logging
import time

from sqlalchemy import and_, func, or_, select, update

from . import db
from .config import get_settings
from .models import Task, TaskStatus

log = logging.getLogger("minereport.worker")


def claim_next_task() -> Task | None:
    """领取下一待处理任务（单条 UPDATE ... RETURNING 原子完成）。

    子查询 FOR UPDATE SKIP LOCKED：并发 worker 争抢时直接跳过已锁行取下一条，
    且锁释放后 EvalPlanQual 复检状态条件，不会重领已被改成 converting 的行；
    外层再挂一份状态复查兜底。converting 超过租约（claimed_at 过旧）的任务
    视为 worker 遗弃，可重新领取。
    """
    s = get_settings()
    lease_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=s.worker_lease_seconds)
    eligible = or_(
        Task.status == TaskStatus.UPLOADED,
        and_(
            Task.status == TaskStatus.CONVERTING,
            Task.claimed_at < lease_cutoff,
        ),
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
                status=TaskStatus.CONVERTING,
                attempts=Task.attempts + 1,
                claimed_at=func.now(),
            )
            .returning(Task)
        ).scalar_one_or_none()
        session.commit()
        return row


def run_once() -> Task | None:
    """单轮：领取任务并立即置 done（骨架行为，供测试与端到端验证）。"""
    task = claim_next_task()
    if task is None:
        return None
    with db.SessionLocal() as session:
        t = session.get(Task, task.id)
        t.status = TaskStatus.DONE
        t.result = {"echo": task.payload}
        session.commit()
    return task


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
