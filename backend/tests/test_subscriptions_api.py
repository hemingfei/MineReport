"""订阅/连接器 API 测试（HTTP 缝合口）：CRUD 与权限矩阵、auto_download 管理员专控、
手动单篇下载（额度提示）、连接器日志与额度汇总。

连接器用注册表注入的 fake（与 worker 测试注册 echo handler 同一模式）；
run 端点 monkeypatch scheduler.run_subscription（真连接器不进 HTTP 测试）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

import app.scheduler as scheduler
from app.db import session_scope
from app.connectors import Connector, FetchedFile, ReportRef, register
from app.errors import ConnectorError


class ApiFakeConnector(Connector):
    """注册进连接器注册表的 fake：refs 固定返回、fetch 出 PDF 字节。"""

    connector_id = "apifake"

    def __init__(self) -> None:
        self.refs = [
            ReportRef(
                external_id="ext-1",
                title="AI算力产业前瞻",
                publish_date=dt.date(2026, 10, 9),
                published_at=dt.datetime(2026, 10, 9, 8, tzinfo=dt.timezone.utc),
                broker="测试证券",
                industry="信息技术",
                pages=10,
                snippet="命中段落",
            )
        ]

    def discover(self, query, since=None, *, orgs=None):
        return list(self.refs)

    def fetch(self, ref):
        return FetchedFile(filename="f.pdf", content_type="application/pdf", data=b"%PDF-fake")

    def quota_hint(self):
        return "fake 会扣额度"


@pytest.fixture(autouse=True)
def _fake_connector():
    register(ApiFakeConnector)
    yield
    from app.connectors import _REGISTRY

    _REGISTRY.pop("apifake", None)


@pytest.fixture()
def active_theme(db_engine):
    from app.models import Theme
    from app.themes import normalize_theme_name

    with session_scope() as s:
        t = Theme(
            name="AI算力", name_norm=normalize_theme_name("AI算力"),
            status="active", source="manual", synonyms=["算力"],
        )
        s.add(t)
        s.commit()
        return t.id


def make_ref_row(subscription_id: int, *, status: str = "seen", external_id: str = "ext-9") -> int:
    from app.db import session_scope
    from app.models import ExternalRef

    with session_scope() as s:
        ref = ExternalRef(
            connector_id="apifake",
            external_id=external_id,
            status=status,
            title="AI算力深度",
            broker="测试证券",
            publish_date=dt.date(2026, 10, 8),
            subscription_id=subscription_id,
        )
        s.add(ref)
        s.commit()
        return ref.id


# ---------- CRUD 与权限矩阵 ----------


def test_reader_cannot_manage_subscriptions(api, make_user, login, active_theme) -> None:
    cookie = login(make_user("reader"))
    r = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    )
    assert r.status_code == 403


def test_create_and_list_own_subscriptions(api, make_user, login, active_theme) -> None:
    analyst = make_user("analyst")
    cookie = login(analyst)
    r = api.post(
        "/api/subscriptions",
        json={"theme_id": active_theme, "connector_id": "apifake", "keywords": ["额外词"]},
        cookies=cookie,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["theme_name"] == "AI算力" and body["auto_download"] is True
    assert body["next_run_at"] is not None  # 首轮已排程（错峰窗口内）
    assert body["keywords"] == ["额外词"]

    # analyst 只看到自己的
    other = make_user("analyst")
    api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=login(other)
    )
    mine = api.get("/api/subscriptions", cookies=cookie).json()
    assert mine["total"] == 1 and mine["items"][0]["created_by"] == analyst.id
    # admin 看全部
    admin_cookie = login(make_user("admin"))
    assert api.get("/api/subscriptions", cookies=admin_cookie).json()["total"] == 2


def test_create_validates_connector_and_theme(api, make_user, login, active_theme, db_engine) -> None:
    from app.models import Theme
    from app.themes import normalize_theme_name

    cookie = login(make_user("analyst"))
    assert api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "ghost"}, cookies=cookie
    ).status_code == 422
    assert api.post(
        "/api/subscriptions", json={"theme_id": 999999, "connector_id": "apifake"}, cookies=cookie
    ).status_code == 404
    with session_scope() as s:
        s.add(Theme(name="待审题材", name_norm=normalize_theme_name("待审题材"), status="pending", source="manual"))
        s.commit()
        pending_id = s.scalar(select(Theme.id).where(Theme.name == "待审题材"))
    assert api.post(
        "/api/subscriptions", json={"theme_id": pending_id, "connector_id": "apifake"}, cookies=cookie
    ).status_code == 409


def test_patch_auto_download_admin_only(api, make_user, login, active_theme) -> None:
    analyst = make_user("analyst")
    cookie = login(analyst)
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]

    denied = api.patch(
        "/api/subscriptions/%d" % sub_id, json={"auto_download": False}, cookies=cookie
    )
    assert denied.status_code == 403

    admin_cookie = login(make_user("admin"))
    ok = api.patch(
        "/api/subscriptions/%d" % sub_id, json={"auto_download": False, "enabled": False}, cookies=admin_cookie
    )
    assert ok.status_code == 200
    assert ok.json()["auto_download"] is False and ok.json()["enabled"] is False

    # 归属人可改 enabled/interval，不可见他刊订阅
    mine = api.patch("/api/subscriptions/%d" % sub_id, json={"interval_hours": 12}, cookies=cookie)
    assert mine.status_code == 200 and mine.json()["interval_hours"] == 12
    stranger = login(make_user("analyst"))
    assert api.patch(
        "/api/subscriptions/%d" % sub_id, json={"interval_hours": 3}, cookies=stranger
    ).status_code == 404
    assert api.delete("/api/subscriptions/%d" % sub_id, cookies=stranger).status_code == 404


def test_delete_subscription(api, make_user, login, active_theme) -> None:
    cookie = login(make_user("analyst"))
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    assert api.delete("/api/subscriptions/%d" % sub_id, cookies=cookie).status_code == 204
    assert api.get("/api/subscriptions", cookies=cookie).json()["total"] == 0


def test_delete_subscription_with_history(api, make_user, login, active_theme, db_engine) -> None:
    """跑过一轮（有日志与发现记录）的订阅也能删：FK ondelete=SET NULL，历史保留可溯。"""
    cookie = login(make_user("analyst"))
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    ref_id = make_ref_row(sub_id)
    with session_scope() as s:
        from app.models import ConnectorRun

        s.add(ConnectorRun(connector_id="apifake", subscription_id=sub_id, event="run", ok=True, message="ok", stats={"downloaded": 1}))
        s.commit()

    assert api.delete("/api/subscriptions/%d" % sub_id, cookies=cookie).status_code == 204
    with session_scope() as s:
        from app.models import ConnectorRun, ExternalRef

        run = s.scalars(select(ConnectorRun)).first()
        assert run.subscription_id is None  # 日志保留，订阅引用置空
        ref = s.get(ExternalRef, ref_id)
        assert ref is not None and ref.subscription_id is None
        # 额度计数不因订阅删除丢失
    quota = api.get("/api/connector/quota", cookies=cookie).json()
    assert quota["downloads_total"] == 1


# ---------- 发现记录 / 手动下载 / 额度 ----------


def test_refs_listing_scoped_to_owner(api, make_user, login, active_theme) -> None:
    analyst = make_user("analyst")
    cookie = login(analyst)
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    make_ref_row(sub_id)
    # 他人订阅的 ref 不可见；admin 全量可见
    other = login(make_user("analyst"))
    assert api.get("/api/subscriptions/refs", cookies=other).json()["total"] == 0
    mine = api.get("/api/subscriptions/refs", cookies=cookie).json()
    assert mine["total"] == 1 and mine["items"][0]["report_url"] is None  # apifake 无阅读链接
    admin = login(make_user("admin"))
    assert api.get("/api/subscriptions/refs", cookies=admin).json()["total"] == 1


def test_manual_download_flow(api, make_user, login, active_theme, db_engine) -> None:
    analyst = make_user("analyst")
    cookie = login(analyst)
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    ref_id = make_ref_row(sub_id)

    # 额度提示（下载确认框数据源）
    quota = api.get("/api/connector/quota", cookies=cookie).json()
    assert quota["hints"]["apifake"] == "fake 会扣额度"
    assert quota["downloads_today"] == 0 and quota["downloads_total"] == 0

    r = api.post("/api/refs/%d/download" % ref_id, cookies=cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ref"]["status"] == "ingested" and body["report_id"] > 0 and body["task_id"] > 0

    # 额度计数 + 日志（admin）
    quota = api.get("/api/connector/quota", cookies=cookie).json()
    assert quota["downloads_total"] == 1 and quota["downloads_today"] == 1
    admin = login(make_user("admin"))
    runs = api.get("/api/admin/connector-runs", cookies=admin).json()
    assert runs["total"] == 1 and runs["items"][0]["event"] == "manual_download"
    # 非事件过滤 + 非 admin 拒绝
    assert api.get(
        "/api/admin/connector-runs", cookies=admin, params={"event": "run"}
    ).json()["total"] == 0
    assert api.get("/api/admin/connector-runs", cookies=cookie).status_code == 403


def test_manual_download_failure_marks_ref(api, make_user, login, active_theme, db_engine, monkeypatch) -> None:
    cookie = login(make_user("analyst"))
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    ref_id = make_ref_row(sub_id)

    class Failing(ApiFakeConnector):
        def fetch(self, ref):
            raise ConnectorError("apifake_down", "平台故障")

    from app.routers import subscriptions as subs_router

    monkeypatch.setattr(
        subs_router.scheduler, "build_connector", lambda cid: Failing(), raising=False
    )
    r = api.post("/api/refs/%d/download" % ref_id, cookies=cookie)
    assert r.status_code == 502
    with session_scope() as s:
        from app.models import ExternalRef

        ref = s.get(ExternalRef, ref_id)
        assert ref.status == "fetch_failed" and "apifake_down" in ref.last_error


def test_fxbaogao_ref_url_derived(api, make_user, login, active_theme, db_engine) -> None:
    cookie = login(make_user("analyst"))
    sub_id = api.post(
        "/api/subscriptions", json={"theme_id": active_theme, "connector_id": "apifake"}, cookies=cookie
    ).json()["id"]
    with session_scope() as s:
        from app.models import ExternalRef

        s.add(ExternalRef(
            connector_id="fxbaogao", external_id="12345", status="seen", title="外网报告",
            broker=None, publish_date=dt.date(2026, 10, 1), subscription_id=sub_id,
        ))
        s.commit()
    items = api.get("/api/subscriptions/refs", cookies=cookie).json()["items"]
    url = next(i["report_url"] for i in items if i["connector_id"] == "fxbaogao")
    assert url == "https://www.fxbaogao.com/view?id=12345"
