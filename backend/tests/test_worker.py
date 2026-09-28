"""worker 轮询任务表的行为测试（真实 Postgres 测试库）。"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Task, TaskStatus
from app import worker

DONE = TaskStatus.DONE
UPLOADED = TaskStatus.UPLOADED
CONVERTING = TaskStatus.CONVERTING


@pytest.fixture(autouse=True)
def _echo_handler(db_engine):
    """注册表注入测试用 echo 处理器：claim 语义测试不依赖真实转换管道。"""

    def _handle(task_id: int) -> None:
        with session_scope() as s:
            t = s.get(Task, task_id)
            t.status = DONE
            t.result = {"echo": t.payload}
            s.commit()

    worker.HANDLERS["echo"] = worker.TaskSpec(_handle, CONVERTING)
    yield
    worker.HANDLERS.pop("echo", None)


def test_enqueue_rejects_unregistered_kind(db_engine) -> None:
    """enqueue 是唯一入队口：未注册 kind 在入队时即刻拒绝，而非领取后 unknown_kind。"""
    with session_scope() as s:
        task = worker.enqueue(s, "echo", {"n": 1})
        assert task.kind == "echo"
        assert task.status == UPLOADED
        with pytest.raises(ValueError, match="unregistered"):
            worker.enqueue(s, "no_such_kind", {})


def test_claim_uses_registry_inflight_status(db_engine) -> None:
    """表驱动：每个注册 kind 领取后的在途状态与 TaskSpec 声明一致（case 由注册表渲染）。"""
    kinds = sorted(worker.HANDLERS)  # 含 fixture 注入的 echo
    with session_scope() as s:
        for kind in kinds:
            s.add(Task(kind=kind, status=UPLOADED, payload={}))
        s.commit()

    claimed: dict[str, str] = {}
    while (task := worker.claim_next_task()) is not None:
        claimed[task.kind] = task.status
    assert set(claimed) == set(kinds)
    for kind in kinds:
        assert claimed[kind] == worker.HANDLERS[kind].inflight_status


def test_import_tasks_run_not_converting(db_engine) -> None:
    """回归：import 类任务领取后在途状态是 running。

    曾长期走 SQL case 的 else_ 分支误入 converting，前端按钮显示"导入转换中…"。
    """
    with session_scope() as s:
        for kind in ("import_targets", "import_themes"):
            s.add(Task(kind=kind, status=UPLOADED, payload={}))
        s.commit()

    claimed: dict[str, str] = {}
    while (task := worker.claim_next_task()) is not None:
        claimed[task.kind] = task.status
    assert claimed == {
        "import_targets": TaskStatus.RUNNING,
        "import_themes": TaskStatus.RUNNING,
    }


def test_run_once_no_task(db_engine) -> None:
    assert worker.run_once() is None


def test_claim_oldest_first_and_exactly_once(db_engine) -> None:
    with session_scope() as s:
        s.add_all(
            [
                Task(kind="echo", status=UPLOADED, payload={"n": 1}),
                Task(kind="echo", status=UPLOADED, payload={"n": 2}),
            ]
        )
        s.commit()

    first = worker.run_once()
    assert first is not None
    assert first.payload == {"n": 1}  # 最旧的先被消费

    with session_scope() as s:
        rows = s.scalars(select(Task).order_by(Task.id)).all()
    assert rows[0].status == DONE  # 已消费
    assert rows[1].status == UPLOADED  # 留给下一轮


def test_processed_task_not_reclaimed(db_engine) -> None:
    with session_scope() as s:
        s.add(Task(kind="echo", status=UPLOADED, payload={"n": 1}))
        s.commit()

    assert worker.run_once() is not None
    assert worker.run_once() is None  # 已完成的任务不再被领取


def test_concurrent_claims_get_distinct_tasks(db_engine) -> None:
    """两个并发领取者各拿到不同任务，无重复领取（FOR UPDATE SKIP LOCKED 语义）。"""
    with session_scope() as s:
        s.add_all(
            [
                Task(kind="convert", status=UPLOADED, payload={"n": 1}),
                Task(kind="convert", status=UPLOADED, payload={"n": 2}),
            ]
        )
        s.commit()

    results: list[Task | None] = []

    def _claim() -> None:
        results.append(worker.claim_next_task())

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_ids = sorted(t.id for t in results if t is not None)
    with session_scope() as s:
        all_ids = sorted(s.scalars(select(Task.id)))
    assert claimed_ids == all_ids  # 两条任务各被领取一次，互不重复


def test_stale_converting_reclaimed_after_lease(db_engine) -> None:
    """converting 超过租约的任务视为 worker 遗弃，可被重新领取。"""
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    with session_scope() as s:
        s.add(Task(kind="convert", status=CONVERTING, payload={"n": 1}, claimed_at=stale))
        s.commit()

    claimed = worker.claim_next_task()
    assert claimed is not None
    assert claimed.attempts == 1
    with session_scope() as s:
        row = s.get(Task, claimed.id)
        assert row.claimed_at is not None
        assert row.claimed_at > stale  # 租约已刷新


def test_fresh_converting_not_reclaimed(db_engine) -> None:
    """租约内的 converting 任务不被动（正在被某个 worker 处理）。"""
    fresh = dt.datetime.now(dt.timezone.utc)
    with session_scope() as s:
        s.add(Task(kind="convert", status=CONVERTING, payload={"n": 1}, claimed_at=fresh))
        s.commit()

    assert worker.claim_next_task() is None
