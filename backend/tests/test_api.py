"""HTTP API 层测试（主缝合口）：FastAPI TestClient + 真实 Postgres 测试库。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Role


def test_health(api: TestClient) -> None:
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_task_roundtrip_poll_semantics(api: TestClient, make_user, login, sample_pdf) -> None:
    """上传建任务 → worker 消费 → 轮询可见状态流转（spec：异步任务一律轮询）。"""
    from app import worker

    cookies = login(make_user(Role.ANALYST))

    r = api.post(
        "/api/reports",
        files={"file": ("dongwu.pdf", sample_pdf("dongwu-002635-anjie-20241231.pdf"), "application/pdf")},
        data={"broker": "东吴证券", "publish_date": "2024-12-31", "title": "安洁科技点评"},
        cookies=cookies,
    )
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]

    r = api.get(f"/api/tasks/{task_id}", cookies=cookies)
    assert r.status_code == 200
    assert r.json()["status"] == "uploaded"

    done = worker.run_once()
    assert done is not None and done.id == task_id

    r = api.get(f"/api/tasks/{task_id}", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["chars_cleaned"] > 0
    assert body["attempts"] == 1


def test_task_404(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.READER))
    assert api.get("/api/tasks/999999", cookies=cookies).status_code == 404
