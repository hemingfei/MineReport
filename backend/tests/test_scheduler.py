"""订阅调度器测试（spec 次缝合口 2 的调度半边：mock 连接器，验证查询展开、
ExternalRef/组合键双重去重、退避重试→死信→连续 5 轮告警、pubTime 游标、
auto_download 开关与入库回调接既有任务管道）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app import scheduler
from app.connectors import Connector, FetchedFile, ReportRef
from app.errors import ConnectorError
from app.models import (
    ConnectorRun,
    ExternalRef,
    ReportFile,
    ResearchReport,
    Subscription,
    Task,
    Theme,
)
from app.themes import normalize_theme_name

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


class FakeConnector(Connector):
    """可编程 stub：refs 为 discover 固定返回；fail_discover 模拟平台故障。"""

    connector_id = "fake"

    def __init__(self, refs=None, *, fail_discover=False, fail_fetch_ids=()):
        self.refs = list(refs or [])
        self.fail_discover = fail_discover
        self.fail_fetch_ids = set(fail_fetch_ids)
        self.discover_calls: list[tuple[str, dt.datetime | None, tuple[str, ...]]] = []
        self.fetch_calls: list[str] = []

    def discover(self, query, since=None, *, orgs=None):
        self.discover_calls.append((query, since, tuple(orgs or ())))
        if self.fail_discover:
            raise ConnectorError("fake_down", "平台故障")
        return list(self.refs)

    def fetch(self, ref):
        self.fetch_calls.append(ref.external_id)
        if ref.external_id in self.fail_fetch_ids:
            raise ConnectorError("fake_fetch", "下载失败")
        return FetchedFile(filename=f"{ref.external_id}.pdf", content_type="application/pdf", data=b"%PDF-fake")

    def quota_hint(self):
        return "fake 额度提示"


def make_ref(eid: str, *, title=None, broker="测试证券", day=9) -> ReportRef:
    published = dt.datetime(2026, 10, day, 8, 0, tzinfo=dt.timezone.utc)
    return ReportRef(
        external_id=eid,
        title=title or f"AI算力产业前瞻-{eid}",
        publish_date=published.date(),
        published_at=published,
        broker=broker,
        industry="信息技术",
        pages=15,
        snippet="命中段落",
    )


@pytest.fixture(autouse=True)
def _no_jitter(tweak_settings):
    """抖动归零：next_run_at 断言可精确。"""
    tweak_settings(subscription_jitter_seconds=0)


@pytest.fixture()
def make_theme(session_factory):
    def _make(name="AI算力", synonyms=("算力",), status="active"):
        with session_factory() as s:
            t = Theme(
                name=name,
                name_norm=normalize_theme_name(name),
                status=status,
                source="manual",
                synonyms=list(synonyms),
            )
            s.add(t)
            s.commit()
            return t.id

    return _make


@pytest.fixture()
def theme(make_theme):
    """每测试一个默认题材（AI算力 + 同义词[算力]）；需要多题材时用 make_theme。"""
    return make_theme()


@pytest.fixture()
def make_subscription(session_factory, make_user, theme):
    def _make(*, auto_download=True, theme_id=None, keywords=None, orgs=None, enabled=True, interval_hours=6, cursor=None, next_run=None):
        owner = make_user("analyst")
        theme_id = theme_id or theme
        with session_factory() as s:
            sub = Subscription(
                theme_id=theme_id,
                connector_id="fake",
                created_by=owner.id,
                enabled=enabled,
                interval_hours=interval_hours,
                auto_download=auto_download,
                keywords=list(keys) if (keys := keywords) else None,
                orgs=list(orgs) if orgs else None,
                cursor_pubtime=cursor,
                next_run_at=next_run if next_run is not None else NOW,
            )
            s.add(sub)
            s.commit()
            return sub.id, owner

    return _make


def run(sub_id, connector=None, now=NOW):
    return scheduler.run_subscription(sub_id, connector=connector, now=now)


def test_expand_queries_dedup_and_cap(session_factory) -> None:
    with session_factory() as s:
        theme = Theme(name="AI算力", name_norm="AI算力", status="active", source="manual",
                      synonyms=["算力", "AI 算力", ""])  # "AI 算力" 归一后与名重合
        queries = scheduler.expand_queries(theme, ["储能", "AI算力"])
    assert queries == ["AI算力", "算力", "储能"]


def test_expand_queries_cap(session_factory) -> None:
    with session_factory() as s:
        theme = Theme(name="T", name_norm="T", status="active", source="manual",
                      synonyms=[f"同义词{i}" for i in range(20)])
        assert len(scheduler.expand_queries(theme, None)) == scheduler.MAX_QUERIES


def test_run_ingests_and_external_ref_dedupes(make_subscription, session_factory) -> None:
    sub_id, owner = make_subscription()
    conn = FakeConnector(refs=[make_ref("r1"), make_ref("r2")])

    stats = run(sub_id, conn)
    assert stats["new"] == 2 and stats["downloaded"] == 2 and stats["ingested"] == 2
    with session_factory() as s:
        refs = s.scalars(select(ExternalRef).order_by(ExternalRef.external_id)).all()
        assert [r.status for r in refs] == ["ingested", "ingested"]
        assert [r.report_id is not None for r in refs] == [True, True]
        # 入库回调：研报 + 文件 + convert 任务（与手动上传同一套管道）
        reports = s.scalars(select(ResearchReport)).all()
        assert len(reports) == 2
        assert all(r.created_by == owner.id for r in reports)
        assert len(s.scalars(select(ReportFile)).all()) == 2
        tasks = s.scalars(select(Task)).all()
        assert [t.kind for t in tasks] == ["convert", "convert"]
        # 成功落账：attempt/连续失败归零，next_run = now + interval
        sub = s.get(Subscription, sub_id)
        assert sub.attempt == 0 and sub.consecutive_failures == 0
        assert sub.next_run_at == NOW + dt.timedelta(hours=6)
        assert sub.cursor_pubtime == make_ref("r1").published_at
        run_rows = s.scalars(select(ConnectorRun).where(ConnectorRun.event == "run")).all()
        assert len(run_rows) == 1 and run_rows[0].ok and run_rows[0].stats["downloaded"] == 2

    # 第二轮同命中（两组查询词各一遍 → 4 次命中全 seen）：不 fetch 不建研报
    stats2 = run(sub_id, FakeConnector(refs=[make_ref("r1"), make_ref("r2")]))
    assert stats2["seen"] == 4 and stats2["new"] == 0 and stats2["downloaded"] == 0
    with session_factory() as s:
        assert len(s.scalars(select(ResearchReport)).all()) == 2
        assert len(s.scalars(select(ConnectorRun).where(ConnectorRun.event == "run")).all()) == 2


def test_query_expansion_from_theme_synonyms(make_subscription) -> None:
    sub_id, _ = make_subscription(keywords=["额外词"], orgs=["测试证券"])
    conn = FakeConnector(refs=[])
    run(sub_id, conn)
    assert [c[0] for c in conn.discover_calls] == ["AI算力", "算力", "额外词"]
    assert conn.discover_calls[0][2] == ("测试证券",)  # orgs 透传


def test_composite_key_hit_skips_download(make_subscription, session_factory) -> None:
    """组合键去重（ADR-0001）：库内已有同（标题归一,券商,日期）研报 → 只挂引用不下载。"""
    from app.conversion import normalize_title

    sub_id, owner = make_subscription()
    with session_factory() as s:
        s.add(ResearchReport(
            title="AI算力产业前瞻", title_norm=normalize_title("AI算力产业前瞻"),
            broker="测试证券", publish_date=dt.date(2026, 10, 9), created_by=owner.id,
        ))
        s.commit()
    conn = FakeConnector(refs=[make_ref("r1", title="AI算力产业前瞻")])
    stats = run(sub_id, conn)
    assert stats["duplicates"] == 1 and conn.fetch_calls == []
    with session_factory() as s:
        ref = s.scalar(select(ExternalRef))
        assert ref.status == "duplicate" and ref.report_id is not None


def test_auto_download_off_records_metadata_only(make_subscription, session_factory) -> None:
    sub_id, _ = make_subscription(auto_download=False)
    conn = FakeConnector(refs=[make_ref("r1")])
    stats = run(sub_id, conn)
    assert stats["pending"] == 1 and conn.fetch_calls == []
    with session_factory() as s:
        ref = s.scalar(select(ExternalRef))
        assert ref.status == "seen" and ref.report_id is None
        assert s.scalars(select(ResearchReport)).first() is None


def test_per_ref_fetch_failure_does_not_kill_run(make_subscription, session_factory) -> None:
    sub_id, _ = make_subscription()
    conn = FakeConnector(refs=[make_ref("r1"), make_ref("r2")], fail_fetch_ids={"r1"})
    stats = run(sub_id, conn)
    assert stats["fetch_failed"] == 1 and stats["ingested"] == 1
    with session_factory() as s:
        statuses = {r.external_id: r.status for r in s.scalars(select(ExternalRef))}
        assert statuses == {"r1": "fetch_failed", "r2": "ingested"}
        failed = s.scalar(select(ExternalRef).where(ExternalRef.external_id == "r1"))
        assert "fake_fetch" in failed.last_error
        run_rows = s.scalars(select(ConnectorRun).where(ConnectorRun.event == "run")).all()
        assert run_rows[0].ok  # 整轮仍记成功


def test_backoff_retry_then_dead_letter(make_subscription, session_factory) -> None:
    sub_id, _ = make_subscription()
    conn = FakeConnector(fail_discover=True)

    run(sub_id, conn, now=NOW)
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.attempt == 1 and sub.next_run_at == NOW + dt.timedelta(seconds=60)
        assert s.scalars(select(ConnectorRun).where(ConnectorRun.event == "retry")).first() is not None

    run(sub_id, conn, now=NOW + dt.timedelta(minutes=1))
    run(sub_id, conn, now=NOW + dt.timedelta(minutes=6))
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.attempt == 3 and sub.next_run_at == NOW + dt.timedelta(minutes=6) + dt.timedelta(seconds=1500)

    # 第 4 次失败：死信，attempt 归零，下一轮回正常 interval
    run(sub_id, conn, now=NOW + dt.timedelta(minutes=31))
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.attempt == 0 and sub.consecutive_failures == 1
        assert sub.next_run_at == NOW + dt.timedelta(minutes=31) + dt.timedelta(hours=6)
        assert s.scalars(select(ConnectorRun).where(ConnectorRun.event == "dead_letter")).first() is not None


def test_consecutive_dead_letters_alert_at_five(make_subscription, session_factory) -> None:
    sub_id, _ = make_subscription()
    conn = FakeConnector(fail_discover=True)
    t = NOW
    for _ in range(4 * 5):  # 每个死信 = 4 次失败（3 退避 + 1 落死信）
        run(sub_id, conn, now=t)
        t += dt.timedelta(hours=1)
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.consecutive_failures == 5
        alerts = s.scalars(select(ConnectorRun).where(ConnectorRun.event == "alert")).all()
        assert len(alerts) == 1  # 恰在第 5 轮触发一次


def test_success_resets_failure_streak(make_subscription, session_factory) -> None:
    sub_id, _ = make_subscription()
    t = NOW
    fail = FakeConnector(fail_discover=True)
    for _ in range(4):
        run(sub_id, fail, now=t)  # 一个完整死信
        t += dt.timedelta(hours=1)
    ok = FakeConnector(refs=[make_ref("r9")])
    run(sub_id, ok, now=t)
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        assert sub.consecutive_failures == 0 and sub.attempt == 0 and sub.last_success_at == t


def test_cursor_drives_since_window(make_subscription) -> None:
    cursor = dt.datetime(2026, 10, 8, 18, 0, tzinfo=dt.timezone.utc)
    sub_id, _ = make_subscription(cursor=cursor)
    conn = FakeConnector(refs=[])
    run(sub_id, conn)
    # since = cursor − 1h 余量
    assert conn.discover_calls[0][1] == cursor - scheduler.CURSOR_OVERLAP
    # 无游标 → None（连接器默认窗口）
    sub2_id, _ = make_subscription()
    conn2 = FakeConnector(refs=[])
    run(sub2_id, conn2)
    assert conn2.discover_calls[0][1] is None


def test_tick_runs_only_due_enabled(make_subscription) -> None:
    due_id, _ = make_subscription(next_run=NOW - dt.timedelta(minutes=5))
    _future_id, _ = make_subscription(next_run=NOW + dt.timedelta(days=1))
    _disabled_id, _ = make_subscription(next_run=NOW - dt.timedelta(minutes=5), enabled=False)

    import app.scheduler as sched
    orig = sched.run_subscription
    ran: list[int] = []
    try:
        sched.run_subscription = lambda sid, **kw: ran.append(sid) or {}  # type: ignore[assignment]
        count = sched.tick(now=NOW)
    finally:
        sched.run_subscription = orig
    assert count == 1 and ran == [due_id]


def test_schedule_first_run_within_jitter(tweak_settings, session_factory) -> None:
    tweak_settings(subscription_jitter_seconds=120)
    with session_factory() as s:
        sub = Subscription(theme_id=1, connector_id="fake", created_by=1)
        scheduler.schedule_first_run(sub, now=NOW)
        assert NOW <= sub.next_run_at <= NOW + dt.timedelta(seconds=120)


def test_worker_starts_subscription_scheduler(session_factory) -> None:
    """worker 的 APSScheduler 挂载：tick 任务存在且可启停（执行语义由上面各用例覆盖）。"""
    from app import worker

    sched = worker.start_subscription_scheduler()
    assert sched is not None
    assert sched.get_job("subscription-tick") is not None
    sched.shutdown(wait=False)


def test_manual_download_ingests_and_idempotent(make_subscription, session_factory) -> None:
    sub_id, owner = make_subscription(auto_download=False)
    conn = FakeConnector(refs=[make_ref("r1")])
    run(sub_id, conn)

    with session_factory() as s:
        ref = s.scalar(select(ExternalRef))
        assert ref.status == "seen"
        downloaded, report, task = scheduler.manual_download(s, ref, connector=FakeConnector(), user_id=owner.id)
        s.commit()
        assert downloaded.status == "ingested" and downloaded.report_id == report.id
        assert task is not None and task.kind == "convert"
        log = s.scalars(select(ConnectorRun).where(ConnectorRun.event == "manual_download")).all()
        assert len(log) == 1 and log[0].stats["downloaded"] == 1
        assert s.scalars(select(Task).where(Task.kind == "convert")).first() is not None
        # 已 ingested 的 ref 再次手动下载：幂等返回，不重复扣额度（fetch 计数为证）
        conn2 = FakeConnector()
        scheduler.manual_download(s, ref, connector=conn2, user_id=owner.id)
        assert conn2.fetch_calls == []
