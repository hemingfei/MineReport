"""综合分析测试（#20 验收清单）。

缝合成数与 #15/#17 一致：HTTP API（真实 Postgres）+ mock LLM（httpx
MockTransport 预录响应，不碰网络）。覆盖：202+task 轮询、缓存命中不重算
（mock 调用数不变）、输入变化自动重算、手动刷新强制 version++、版本链、
证据边界（引用序位 ↔ 研报 id 回链）、题材入口 latest_synthesis、权限矩阵、
归一纪律（引用钳位/无引用丢弃/代码白名单）。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession

import app.worker as worker
from app import analysis
from app.models import ReportFile, ResearchReport, Role, Target, Theme
from app.synthesis import input_fingerprint, normalize_synthesis
from app.themes import ThemeSource, ThemeStatus, normalize_theme_name

from helpers import make_llm

# ---------- 造数 helpers ----------


@pytest.fixture()
def db_session(db_engine):
    from app.db import session_scope

    # 退出时 commit 是安全网：造数路径均已显式提交，无 teardown 丢弃语义
    with session_scope() as s:
        yield s


def _mk_target(s: OrmSession, code: str, name: str) -> Target:
    from app.targets import exchange_of, normalize_name

    t = Target(code=code, name=name, name_norm=normalize_name(name), exchange=exchange_of(code))
    s.add(t)
    s.flush()
    return t


def _mk_theme(s: OrmSession, name: str, status: str = ThemeStatus.ACTIVE) -> Theme:
    theme = Theme(name=name, name_norm=normalize_theme_name(name), status=status, source=ThemeSource.MANUAL)
    s.add(theme)
    s.flush()
    return theme


def _mk_report(
    s: OrmSession, owner_id: int, *, markdown: str, publish_date: dt.date
) -> tuple[ResearchReport, ReportFile]:
    from app.conversion import normalize_title

    report = ResearchReport(
        title=f"测试研报-{uuid.uuid4().hex[:8]}",
        title_norm=normalize_title(f"测试研报-{uuid.uuid4().hex[:8]}"),
        broker="测试券商",
        publish_date=publish_date,
        created_by=owner_id,
    )
    s.add(report)
    s.flush()
    f = ReportFile(
        report_id=report.id,
        storage_key=f"reports/{report.id}/files/1/a.pdf",
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=1,
        file_sha256="0" * 64,
        uploaded_by=owner_id,
        markdown_text=markdown,
        converted_at=dt.datetime.now(dt.timezone.utc),
    )
    s.add(f)
    s.flush()
    return report, f


def _analysis_raw(theme_name: str, summary: str, targets: list[dict]) -> dict:
    return {
        "broker": "测试券商",
        "authors": [],
        "publish_date": "2024-12-31",
        "report_type": "点评",
        "title": "x",
        "summary": summary,
        "themes": [{"name": theme_name, "reason": "主业归属"}],
        "targets": targets,
        "rating": {"action": "买入", "maintained": False},
        "risk_notes": "",
    }


def _analyze(s: OrmSession, report: ResearchReport, file: ReportFile, raw: dict) -> None:
    """走真实分析管道（题材/标的投影随版本落库），mock LLM 单次回放。"""
    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    a = analysis.run_analysis(s, report, file, llm=llm)
    s.commit()
    assert a.version >= 1


SYNTH_OK = {
    "common_conclusions": [
        {"text": "消费电子需求复苏是多家共识主线", "report_refs": [1, 2]},
    ],
    "consensus_targets": [
        {"name": "安洁科技", "code": "002635", "view": "精密件龙头，双轮驱动", "report_refs": [1, 2]},
    ],
    "divergences": [
        {"text": "对下半年出货量弹性判断分化", "report_refs": [1, 2]},
    ],
}


def _run_worker_task(api: TestClient, cookies: dict, task_id: int) -> dict:
    claimed = worker.run_once()
    assert claimed is not None and claimed.id == task_id
    return api.get(f"/api/tasks/{task_id}", cookies=cookies).json()


# ---------- 主流程：生成 / 缓存命中 / 输入变化重算 / 手动刷新版本链 ----------

def test_synthesis_flow_cache_and_versions(api, login, make_user, llm_env, db_session) -> None:
    analyst = make_user(Role.ANALYST)
    reader = make_user(Role.READER)
    cookies = login(analyst)

    _mk_target(db_session, "002635", "安洁科技")
    _mk_target(db_session, "002475", "立讯精密")
    theme = _mk_theme(db_session, "消费电子")
    db_session.commit()

    md = "安洁科技（002635）点评\n" + "消费电子需求复苏，AR/VR 终端放量。" * 40
    r1, f1 = _mk_report(db_session, analyst.id, markdown=md, publish_date=dt.date(2024, 12, 20))
    r2, f2 = _mk_report(db_session, analyst.id, markdown=md, publish_date=dt.date(2025, 1, 10))
    _analyze(db_session, r1, f1, _analysis_raw("消费电子", "复苏节奏偏缓", [
        {"code": "002635", "name": "安洁科技", "stance": "推荐", "view": "v1", "has_forecast": True},
    ]))
    _analyze(db_session, r2, f2, _analysis_raw("消费电子", "复苏节奏加快", [
        {"code": "002475", "name": "立讯精密", "stance": "提及", "view": "v2", "has_forecast": False},
    ]))

    synth_llm = make_llm([json.dumps(SYNTH_OK, ensure_ascii=False)])
    llm_env(synth_llm)

    # 首次生成：202 + task，轮询语义与既有任务一致
    r = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["cached"] is False and body["report_count"] == 2
    task_detail = _run_worker_task(api, cookies, body["task_id"])
    assert task_detail["status"] == "done", task_detail
    sid = task_detail["result"]["synthesis_id"]
    assert task_detail["result"]["version"] == 1
    assert len(synth_llm.mock_calls) == 1

    # 结果页：结构 + 证据边界（引用序位 = report_ids 序位，回链研报）+ 版本链
    g = api.get(f"/api/syntheses/{sid}", cookies=login(reader)).json()
    assert g["version"] == 1 and g["theme_name"] == "消费电子"
    assert g["report_ids"] == [r2.id, r1.id]  # 发布日期降序选集
    assert [x["id"] for x in g["reports"]] == g["report_ids"]
    assert all(x["title"] for x in g["reports"])
    c = g["result"]["common_conclusions"][0]
    assert c["report_refs"] == [1, 2]
    # 证据边界回链：引用序位映射回真实研报（R1 = 选集首位 = 最新发布）
    assert g["reports"][c["report_refs"][0] - 1]["id"] == r2.id
    t = g["result"]["consensus_targets"][0]
    assert t["code"] == "002635"  # 白名单内的代码保留
    assert g["result"]["divergences"][0]["report_refs"] == [1, 2]
    assert [v["version"] for v in g["versions"]] == [1]

    # 题材详情入口：latest_synthesis 指向最新版
    t2 = api.get(f"/api/themes/{theme.id}", cookies=cookies).json()
    assert t2["latest_synthesis"] == {"id": sid, "version": 1, "created_at": t2["latest_synthesis"]["created_at"]}

    # 缓存命中：输入指纹未变 → 200 + cached，不重算（mock 调用数不变）
    r = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert r.status_code == 200, r.text
    hit = r.json()
    assert hit["cached"] is True and hit["synthesis_id"] == sid and hit["version"] == 1
    assert len(synth_llm.mock_calls) == 1

    # 输入变化（新研报入题材）→ 指纹变了自动重算，version++
    r3, f3 = _mk_report(db_session, analyst.id, markdown=md, publish_date=dt.date(2025, 2, 1))
    _analyze(db_session, r3, f3, _analysis_raw("消费电子", "新订单能见度提升", []))
    r = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert r.status_code == 202, r.text
    task_detail = _run_worker_task(api, cookies, r.json()["task_id"])
    assert task_detail["status"] == "done"
    sid2 = task_detail["result"]["synthesis_id"]
    assert task_detail["result"]["version"] == 2
    g2 = api.get(f"/api/syntheses/{sid2}", cookies=cookies).json()
    assert r3.id in g2["report_ids"] and g2["version"] == 2
    assert [v["version"] for v in g2["versions"]] == [2, 1]

    # 手动刷新：输入未变也强制重算、version++（题材词表变更的兜底入口）
    r = api.post(f"/api/syntheses/{sid2}/refresh", cookies=cookies)
    assert r.status_code == 202, r.text
    assert r.json()["report_count"] == 3
    task_detail = _run_worker_task(api, cookies, r.json()["task_id"])
    assert task_detail["status"] == "done"
    sid3 = task_detail["result"]["synthesis_id"]
    assert task_detail["result"]["version"] == 3
    assert len(synth_llm.mock_calls) == 3  # v1 / v2 / 刷新各一次，缓存命中未烧 token
    g3 = api.get(f"/api/syntheses/{sid3}", cookies=cookies).json()
    assert [v["version"] for v in g3["versions"]] == [3, 2, 1]

    # 权限矩阵：读者可看结果，不可触发/刷新
    assert api.get(f"/api/syntheses/{sid3}", cookies=login(reader)).status_code == 200
    assert api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=login(reader)).status_code == 403
    assert api.post(f"/api/syntheses/{sid3}/refresh", cookies=login(reader)).status_code == 403


# ---------- 前置条件与错误路径 ----------

def test_synthesis_preconditions(api, login, make_user, llm_env, db_session) -> None:
    analyst = make_user(Role.ANALYST)
    cookies = login(analyst)
    llm_env(make_llm(["{}"]))  # 前置失败不触达 LLM

    # 题材不存在 / 死条目（合并、停用）
    assert api.post("/api/syntheses", json={"theme_id": 99999}, cookies=cookies).status_code == 404
    retired = _mk_theme(db_session, "已停用题材", ThemeStatus.RETIRED)
    merged = _mk_theme(db_session, "已合并题材", ThemeStatus.MERGED)
    db_session.commit()
    assert api.post("/api/syntheses", json={"theme_id": retired.id}, cookies=cookies).status_code == 409
    assert api.post("/api/syntheses", json={"theme_id": merged.id}, cookies=cookies).status_code == 409

    # 题材下没有可综合的研报（无当前分析关联）
    empty = _mk_theme(db_session, "空题材")
    db_session.commit()
    r = api.post("/api/syntheses", json={"theme_id": empty.id}, cookies=cookies)
    assert r.status_code == 409
    assert "暂无可综合" in r.json()["detail"]

    # 结果不存在
    assert api.get("/api/syntheses/424242", cookies=cookies).status_code == 404


def test_synthesis_llm_not_configured(api, login, make_user, db_session) -> None:
    """LLM 未配置：快速失败 503，不排队（与 reanalyze 同语义）。"""
    analyst = make_user(Role.ANALYST)
    theme = _mk_theme(db_session, "无LLM题材")
    db_session.commit()
    r = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=login(analyst))
    assert r.status_code == 503


def test_synthesis_inflight_dedup(api, login, make_user, llm_env, db_session) -> None:
    """同输入在途任务复用：并发/重复 POST 不再入第二个任务（不重复烧 token）；
    输入变化后的 POST 仍入新任务（携带新选集）。"""
    analyst = make_user(Role.ANALYST)
    cookies = login(analyst)
    llm_env(make_llm([json.dumps(SYNTH_OK, ensure_ascii=False)]))

    _mk_target(db_session, "002635", "安洁科技")
    theme = _mk_theme(db_session, "去重题材")
    db_session.commit()
    md = "安洁科技（002635）点评\n" + "消费电子复苏。" * 40
    r1, f1 = _mk_report(db_session, analyst.id, markdown=md, publish_date=dt.date(2025, 3, 1))
    _analyze(db_session, r1, f1, _analysis_raw("去重题材", "s", [
        {"code": "002635", "name": "安洁科技", "stance": "推荐", "view": "v", "has_forecast": False},
    ]))

    first = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert first.status_code == 202
    second = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert second.status_code == 202
    assert second.json()["task_id"] == first.json()["task_id"]

    # 任务完成后同输入 POST 走缓存命中（200），不再入队
    task_detail = _run_worker_task(api, cookies, first.json()["task_id"])
    assert task_detail["status"] == "done"
    again = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert again.status_code == 200 and again.json()["cached"] is True


def test_synthesize_task_fails_when_theme_governed_away(api, login, make_user, llm_env, db_session) -> None:
    """排队期间题材被停用：任务失败并给出明确 error_code（重走 POST 重新选集）。"""
    analyst = make_user(Role.ANALYST)
    cookies = login(analyst)
    llm_env(make_llm(["{}"]))

    _mk_target(db_session, "002635", "安洁科技")
    theme = _mk_theme(db_session, "排队题材")
    db_session.commit()
    md = "安洁科技（002635）点评\n" + "消费电子复苏。" * 40
    r1, f1 = _mk_report(db_session, analyst.id, markdown=md, publish_date=dt.date(2025, 3, 1))
    _analyze(db_session, r1, f1, _analysis_raw("排队题材", "s", [
        {"code": "002635", "name": "安洁科技", "stance": "推荐", "view": "v", "has_forecast": False},
    ]))

    r = api.post("/api/syntheses", json={"theme_id": theme.id}, cookies=cookies)
    assert r.status_code == 202

    # 任务在队时题材被停用（模拟治理与队列竞态）
    from app.themes import retire_theme

    retire_theme(db_session, theme, analyst.id)
    db_session.commit()

    task_detail = _run_worker_task(api, cookies, r.json()["task_id"])
    assert task_detail["status"] == "failed"
    assert task_detail["result"]["error_code"] == "theme_unavailable"


# ---------- 归一纪律（纯逻辑） ----------

def test_normalize_synthesis_discipline() -> None:
    raw = {
        "common_conclusions": [
            {"text": "有效结论", "report_refs": [2, 1, 1, "3"]},  # 去重排序、字符串容错
            {"text": "越界引用钳位", "report_refs": [1, 99]},  # 99 丢弃，保留 [1]
            {"text": "无引用结论应整条丢弃", "report_refs": []},
            {"text": "引用全越界也丢弃", "report_refs": [100]},
            {"text": "", "report_refs": [1]},  # 空文本丢弃
        ],
        "consensus_targets": [
            {"name": "安洁科技", "code": "002635", "view": "ok", "report_refs": [1]},
            {"name": "幻觉公司", "code": "999999", "view": "白名单外置 null", "report_refs": [1]},
            {"name": "无码标的", "code": None, "view": "", "report_refs": [1, 2]},
        ],
        "divergences": [
            {"text": "有效分歧", "report_refs": [1]},
            "not-a-dict",  # 形状漂移容错
        ],
    }
    out = normalize_synthesis(raw, n_reports=2, known_codes={"002635"})
    assert [c.text for c in out.common_conclusions] == ["有效结论", "越界引用钳位"]
    assert out.common_conclusions[0].report_refs == [1, 2]  # 3 越界、字符串容错、去重排序
    assert out.common_conclusions[1].report_refs == [1]
    codes = [t.code for t in out.consensus_targets]
    assert codes == ["002635", None, None]
    assert [d.text for d in out.divergences] == ["有效分歧"]

    # 输入规模元信息由 run_synthesis 填充
    assert out.input_total == 0 and out.truncated is False


def test_input_fingerprint_order_insensitive_and_discriminating() -> None:
    assert input_fingerprint([3, 1, 2]) == input_fingerprint([1, 2, 3])
    assert input_fingerprint([1, 2]) != input_fingerprint([1, 2, 3])
    assert len(input_fingerprint([1])) == 64
