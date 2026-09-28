"""HTTP API 层测试（主缝合口）：FastAPI TestClient + 真实 Postgres 测试库。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Role


def test_health(api: TestClient) -> None:
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_task_roundtrip_poll_semantics(api: TestClient, make_user, login) -> None:
    """API 建任务 → worker 消费 → 轮询可见状态流转（spec：异步任务一律轮询）。"""
    from sqlalchemy import select

    import app.db as db_mod
    from app.models import Task, TaskStatus

    cookies = login(make_user(Role.ANALYST))

    # API 侧建任务（骨架期无 POST /api/reports，直接经共享任务表投递）
    with db_mod.SessionLocal() as db:
        task = Task(kind="convert", status=TaskStatus.UPLOADED, payload={"report": 1})
        db.add(task)
        db.commit()
        task_id = task.id

    r = api.get(f"/api/tasks/{task_id}", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["status"] == "uploaded"

    # worker 单轮消费
    from app import worker

    done = worker.run_once()
    assert done is not None and done.id == task_id

    r = api.get(f"/api/tasks/{task_id}", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"] == {"echo": {"report": 1}}
    assert body["attempts"] == 1


def test_task_404(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.READER))
    assert api.get("/api/tasks/999999", cookies=cookies).status_code == 404
