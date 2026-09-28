"""研报入库 HTTP 端到端测试（#13 验收清单）。

主缝合口：TestClient + 真实 Postgres + 真实本地存储（tmp）+ 真实 markitdown 转换。
夹具 PDF 来自 research/markitdown-samples/（正常/空密码加密/扫描版三种形态）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.conversion import clean_markdown
from app.models import Role

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "research" / "markitdown-samples"

DONGWU = "dongwu-002635-anjie-20241231.pdf"
ENCRYPTED = "edge-encrypted.pdf"
SCANNED = "edge-scanned-image-only.pdf"


def _upload(
    api: TestClient,
    cookies: dict,
    filename: str = DONGWU,
    broker: str = "东吴证券",
    publish_date: str = "2024-12-31",
    title: str | None = None,
):
    if not (SAMPLES_DIR / "pdf" / DONGWU).exists():
        pytest.skip("research/markitdown-samples 本地资产不在仓库，相关测试跳过")
    data = {"broker": broker, "publish_date": publish_date}
    if title is not None:
        data["title"] = title
    return api.post(
        "/api/reports",
        files={"file": (filename, (SAMPLES_DIR / "pdf" / filename).read_bytes(), "application/pdf")},
        data=data,
        cookies=cookies,
    )


def _convert(api: TestClient, cookies: dict, r_json: dict) -> dict:
    """跑 worker 直到当前任务完结（uploaded → converting → done|failed 语义下 run_once 即一轮）。"""
    claimed = worker.run_once()
    assert claimed is not None and claimed.id == r_json["task_id"]
    return api.get(f"/api/tasks/{r_json['task_id']}", cookies=cookies).json()


# ---------- 上传与权限 ----------

def test_upload_role_gate(api: TestClient, make_user, login, sample_pdf) -> None:
    assert api.post(
        "/api/reports",
        files={"file": ("a.pdf", sample_pdf(SCANNED), "application/pdf")},
        data={"broker": "x", "publish_date": "2024-01-01"},
    ).status_code == 401
    reader = login(make_user(Role.READER))
    assert _upload(api, reader).status_code == 403


def test_upload_rejects_unsupported_extension(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.ANALYST))
    r = api.post(
        "/api/reports",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"broker": "x", "publish_date": "2024-01-01"},
        cookies=cookies,
    )
    assert r.status_code == 422


def test_upload_202_shape(api: TestClient, make_user, login) -> None:
    user = make_user(Role.ANALYST)
    cookies = login(user)
    r = _upload(api, cookies, title="安洁科技点评")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["merged"] is False
    assert body["task_id"] and body["report_id"] and body["file_id"]
    task = api.get(f"/api/tasks/{body['task_id']}", cookies=cookies).json()
    assert task["status"] == "uploaded"  # 前端 3~5s 轮询起点
    assert task["payload"]["triggered_by"] == user.id  # 审计：convert 与其余 kind 一致记触发者


# ---------- 转换管道（真实夹具三形态） ----------

def test_conversion_done_and_markdown_readable(api: TestClient, make_user, login) -> None:
    """正常 PDF：done + 浏览器可读清洗后正文（与参考 md 清洗结果一致）。"""
    cookies = login(make_user(Role.ANALYST))
    r = _upload(api, cookies, title="安洁科技点评")
    task = _convert(api, cookies, r.json())
    assert task["status"] == "done"
    assert task["attempts"] == 1

    md = api.get(f"/api/reports/{r.json()['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    expected = clean_markdown(
        (SAMPLES_DIR / "md" / "dongwu-002635-anjie-20241231.md").read_text(encoding="utf-8")
    )
    assert md.text == expected
    assert "免责条款" not in md.text  # 清洗生效
    assert task["result"]["chars_raw"] > task["result"]["chars_cleaned"]


def test_conversion_encrypted_empty_password_succeeds(api: TestClient, make_user, login) -> None:
    """空密码加密件：pypdf 解密重写后正常转换（spec：不许静默空串，也不误杀）。"""
    cookies = login(make_user(Role.ANALYST))
    r = _upload(api, cookies, filename=ENCRYPTED, title="加密件同文")
    task = _convert(api, cookies, r.json())
    assert task["status"] == "done"
    md = api.get(f"/api/reports/{r.json()['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 200 and "安洁科技" in md.text


def test_conversion_scanned_rejected_clearly(api: TestClient, make_user, login) -> None:
    """扫描版：failed + error_code=scanned + stage=preflight，正文 404 而非空串。"""
    cookies = login(make_user(Role.ANALYST))
    r = _upload(api, cookies, filename=SCANNED, title="扫描件")
    task = _convert(api, cookies, r.json())
    assert task["status"] == "failed"
    assert task["result"]["error_code"] == "scanned"
    assert task["result"]["stage"] == "preflight"
    md = api.get(f"/api/reports/{r.json()['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 404


# ---------- 组合键去重（ADR-0001） ----------

def test_dedup_same_identity_merges_files(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.ANALYST))
    first = _upload(api, cookies, title="安洁科技：ＡＩ 算力　深度").json()
    second = _upload(
        api, cookies, filename=ENCRYPTED, title="安洁科技：ai算力 深度"
    ).json()  # 全半角/空白差异 + 不同字节（加密重写版）

    assert second["merged"] is True
    assert second["report_id"] == first["report_id"]
    assert second["file_id"] != first["file_id"]

    detail = api.get(f"/api/reports/{first['report_id']}", cookies=cookies).json()
    assert len(detail["files"]) == 2
    assert detail["files"][0]["file_sha256"] != detail["files"][1]["file_sha256"]  # sha 仅属性不判重

    listing = api.get("/api/reports", cookies=cookies).json()
    assert listing["total"] == 1


def test_dedup_different_broker_or_date_stays_separate(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.ANALYST))
    _upload(api, cookies, title="晨报", broker="东吴证券", publish_date="2024-12-31")
    _upload(api, cookies, title="晨报", broker="国源证券", publish_date="2024-12-31")
    _upload(api, cookies, title="晨报", broker="东吴证券", publish_date="2025-01-02")
    listing = api.get("/api/reports", cookies=cookies).json()
    assert listing["total"] == 3


# ---------- 列表过滤与分页 ----------

def test_list_filter_and_pagination(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.ANALYST))
    _upload(api, cookies, title="A", broker="东吴证券", publish_date="2024-12-31", filename=DONGWU)
    _upload(api, cookies, title="B", broker="国源证券", publish_date="2024-11-30", filename=ENCRYPTED)
    _upload(api, cookies, title="C", broker="东吴证券", publish_date="2025-01-15", filename=SCANNED)

    r = api.get("/api/reports", params={"broker": "东吴证券"}, cookies=cookies).json()
    assert [i["title"] for i in r["items"]] == ["C", "A"]  # 发布日期倒序
    assert r["total"] == 2

    r = api.get(
        "/api/reports", params={"date_from": "2024-12-01", "date_to": "2024-12-31"}, cookies=cookies
    ).json()
    assert [i["title"] for i in r["items"]] == ["A"]

    r = api.get("/api/reports", params={"limit": 2, "offset": 1}, cookies=cookies).json()
    assert len(r["items"]) == 2 and r["total"] == 3  # 分页不吞 total


# ---------- 原始文件下载 ----------

def test_file_download_role_matrix(api: TestClient, make_user, login, sample_pdf) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    body = _upload(api, analyst, title="安洁科技点评").json()

    r = api.get(f"/api/reports/{body['report_id']}/file", cookies=reader)
    assert r.status_code == 403  # 读者默认拒
    r = api.get(f"/api/reports/{body['report_id']}/file", cookies=analyst)
    assert r.status_code == 200
    assert r.content == sample_pdf(DONGWU)
    assert "attachment" in r.headers["content-disposition"]


def test_file_download_reader_allowed_via_flag(api: TestClient, make_user, login, sample_pdf, tweak_settings) -> None:
    tweak_settings(allow_reader_download=True)
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    body = _upload(api, analyst, title="安洁科技点评").json()
    r = api.get(f"/api/reports/{body['report_id']}/file", cookies=reader)
    assert r.status_code == 200 and r.content == sample_pdf(DONGWU)


def test_markdown_readable_by_reader(api: TestClient, make_user, login) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    body = _upload(api, analyst, title="安洁科技点评").json()
    _convert(api, analyst, body)
    r = api.get(f"/api/reports/{body['report_id']}/markdown", cookies=reader)
    assert r.status_code == 200 and "安洁科技" in r.text


def test_markdown_latest_converted_file_wins(api: TestClient, make_user, login) -> None:
    """同研报第二份文件转换失败时正文不被清空；成功时后到者优先。"""
    cookies = login(make_user(Role.ANALYST))
    first = _upload(api, cookies, title="同文多来源").json()
    _convert(api, cookies, first)
    second = _upload(api, cookies, filename=SCANNED, title="同文多来源").json()  # 并入且必失败
    assert second["merged"] is True
    task = _convert(api, cookies, second)
    assert task["status"] == "failed"
    md = api.get(f"/api/reports/{first['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 200 and "安洁科技" in md.text


# ---------- 软删除与恢复 ----------

def test_soft_delete_matrix_and_restore(api: TestClient, make_user, login) -> None:
    admin = login(make_user(Role.ADMIN))
    owner = login(make_user(Role.ANALYST))
    other = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    body = _upload(api, owner, title="待删除研报").json()
    rid = body["report_id"]

    assert api.delete(f"/api/reports/{rid}", cookies=other).status_code == 403  # 分析师仅自己的
    assert api.delete(f"/api/reports/{rid}", cookies=reader).status_code == 403
    assert api.delete(f"/api/reports/{rid}", cookies=owner).status_code == 204

    assert api.get(f"/api/reports/{rid}", cookies=reader).status_code == 404
    assert api.get(f"/api/reports/{rid}/markdown", cookies=reader).status_code == 404
    assert api.get("/api/reports", cookies=reader).json()["total"] == 0

    assert api.post(f"/api/reports/{rid}/restore", cookies=owner).status_code == 200
    assert api.get(f"/api/reports/{rid}", cookies=reader).status_code == 200

    # admin 可删他人的
    assert api.delete(f"/api/reports/{rid}", cookies=admin).status_code == 204
    assert api.post(f"/api/reports/{rid}/restore", cookies=admin).status_code == 200


def test_restore_window_expired(api: TestClient, make_user, login) -> None:
    owner = login(make_user(Role.ANALYST))
    body = _upload(api, owner, title="过期研报").json()
    rid = body["report_id"]
    assert api.delete(f"/api/reports/{rid}", cookies=owner).status_code == 204

    from app.db import session_scope
    from app.models import ResearchReport

    with session_scope() as s:
        row = s.get(ResearchReport, rid)
        row.deleted_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
        s.commit()

    r = api.post(f"/api/reports/{rid}/restore", cookies=owner)
    assert r.status_code == 410


def test_restore_conflict_after_reupload(api: TestClient, make_user, login) -> None:
    owner = login(make_user(Role.ANALYST))
    first = _upload(api, owner, title="撞键研报").json()
    assert api.delete(f"/api/reports/{first['report_id']}", cookies=owner).status_code == 204

    second = _upload(api, owner, title="撞键研报").json()  # 软删后重传 → 新条目
    assert second["report_id"] != first["report_id"] and second["merged"] is False

    r = api.post(f"/api/reports/{first['report_id']}/restore", cookies=owner)
    assert r.status_code == 409


# ---------- 失败任务分阶段重试 ----------

def test_retry_resumes_from_failed_stage(api: TestClient, make_user, login, monkeypatch) -> None:
    cookies = login(make_user(Role.ANALYST))
    body = _upload(api, cookies, title="重试研报").json()
    task_id = body["task_id"]

    # convert 阶段炸一次（preflight 已完成）
    real_convert = worker.convert_to_markdown
    calls = {"n": 0}

    def flaky(data: bytes, filename: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("markitdown 瞬时故障")
        return real_convert(data, filename)

    preflight_calls = {"n": 0}
    real_preflight = worker.preflight_pdf

    def counted_preflight(data: bytes, filename: str) -> bytes:
        preflight_calls["n"] += 1
        return real_preflight(data, filename)

    monkeypatch.setattr(worker, "convert_to_markdown", flaky)
    monkeypatch.setattr(worker, "preflight_pdf", counted_preflight)

    claimed = worker.run_once()
    assert claimed is not None and claimed.id == task_id
    task = api.get(f"/api/tasks/{task_id}", cookies=cookies).json()
    assert task["status"] == "failed" and task["result"]["error_code"] == "internal"
    assert task["payload"]["stages_done"] == ["preflight"]
    assert preflight_calls["n"] == 1

    # retry：重置回 uploaded，worker 重跑时跳过 preflight
    r = api.post(f"/api/tasks/{task_id}/retry", cookies=cookies)
    assert r.status_code == 200 and r.json()["status"] == "uploaded"

    claimed = worker.run_once()
    assert claimed is not None
    task = api.get(f"/api/tasks/{task_id}", cookies=cookies).json()
    assert task["status"] == "done"
    assert task["attempts"] == 2
    assert preflight_calls["n"] == 1  # 分阶段重试：preflight 未重跑
    assert calls["n"] == 2

    md = api.get(f"/api/reports/{body['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 200


def test_retry_only_failed_tasks(api: TestClient, make_user, login) -> None:
    cookies = login(make_user(Role.ANALYST))
    body = _upload(api, cookies, title="不可重试").json()
    r = api.post(f"/api/tasks/{body['task_id']}/retry", cookies=cookies)
    assert r.status_code == 409  # uploaded 状态不可重试
