"""#18 中文全文搜索：zhparser 分词召回、三源（标题/正文/总结）命中、组合过滤、
增量更新挂点（建档/转换 persist/分析指针移动）、权限（所有角色可搜）。

直连 ORM 造数后调 refresh_search_vector（生产由挂点自动触发；上传→worker 转换的
端到端路径单测覆盖）。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from test_analysis import make_llm


def _refresh(report_id: int) -> None:
    from app import search
    from app.db import session_scope

    with session_scope() as s:
        search.refresh_search_vector(s, report_id)
        s.commit()


def _ids(r: dict) -> set[int]:
    return {item["id"] for item in r["items"]}


def test_search_body_recall_zhparser(api: TestClient, make_user, login, make_report) -> None:
    """验收项：搜"算力"召回正文含"AI 算力"的研报（zhparser 分词质量）。"""
    hit, _ = make_report("全球 AI 算力需求持续爆发，算力基础设施投资加码。")
    miss, _ = make_report("消费板块温和复苏，关注品牌出海。")
    _refresh(hit)
    _refresh(miss)
    reader = login(make_user("reader"))
    r = api.get("/api/reports", params={"q": "算力"}, cookies=reader).json()
    assert _ids(r) == {hit}


def test_search_title_hit(api: TestClient, login, make_report, make_user) -> None:
    rid, _ = make_report(None, title="AI算力产业链深度研究")
    _refresh(rid)
    r = api.get("/api/reports", params={"q": "算力"}, cookies=login(make_user("reader"))).json()
    assert _ids(r) == {rid}


def test_search_summary_hit_via_analysis_pointer(api: TestClient, login, make_user, make_report) -> None:
    """总结源：当前分析版本的 result.summary 入索引。"""
    from app import search
    from app.db import session_scope
    from app.models import Analysis, ResearchReport

    rid, fid = make_report("公司经营稳健，盈利能力改善。", title="某公司年报点评")
    with session_scope() as s:
        a = Analysis(
            report_id=rid,
            report_file_id=fid,
            version=1,
            prompt_version="v1",
            model="mock-llm",
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
            result={"summary": "公司卡位人形机器人核心零部件，成长空间打开。"},
        )
        s.add(a)
        s.flush()
        s.get(ResearchReport, rid).current_analysis_id = a.id
        search.refresh_search_vector(s, rid)
        s.commit()

    r = api.get("/api/reports", params={"q": "人形机器人"}, cookies=login(make_user("reader"))).json()
    assert _ids(r) == {rid}


def test_search_multi_term_is_intersection(api: TestClient, login, make_user, make_report) -> None:
    """多词按分词交集（plainto_tsquery 语义）：全含才召回。"""
    both, _ = make_report("AI 算力与光模块共振。")
    half, _ = make_report("AI 应用端落地加速。")
    _refresh(both)
    _refresh(half)
    cookies = login(make_user("reader"))
    assert _ids(api.get("/api/reports", params={"q": "算力 光模块"}, cookies=cookies).json()) == {both}
    assert api.get("/api/reports", params={"q": "算力 消费"}, cookies=cookies).json()["total"] == 0


def test_search_combines_with_broker_and_date(api: TestClient, login, make_user, make_report) -> None:
    a, _ = make_report("AI 算力需求爆发。", broker="券商甲")
    b, _ = make_report("AI 算力需求爆发。", broker="券商乙")
    _refresh(a)
    _refresh(b)
    cookies = login(make_user("reader"))
    r = api.get("/api/reports", params={"q": "算力", "broker": "券商乙"}, cookies=cookies).json()
    assert _ids(r) == {b}
    r = api.get(
        "/api/reports",
        params={"q": "算力", "date_from": "2025-01-01"},
        cookies=cookies,
    ).json()
    assert r["total"] == 0  # 夹具都是 2024-12-31


def test_search_blank_q_returns_all(api: TestClient, login, make_user, make_report) -> None:
    a, _ = make_report("AI 算力。")
    b, _ = make_report("消费复苏。")
    r = api.get("/api/reports", params={"q": "   "}, cookies=login(make_user("reader"))).json()
    assert _ids(r) == {a, b}


def test_search_indexes_only_converted_files(api: TestClient, login, make_user, make_report) -> None:
    """正文只认最近"转换完成"的文件：未转换（converted_at NULL）的 markdown 不入索引。"""
    from app.db import session_scope
    from app.models import ReportFile

    import datetime as dt

    rid, fid = make_report(None)
    with session_scope() as s:
        f = s.get(ReportFile, fid)
        f.markdown_text = "草稿阶段的算力正文，尚未转换完成。"
        from app import search

        search.refresh_search_vector(s, rid)
        s.commit()
    cookies = login(make_user("reader"))
    assert api.get("/api/reports", params={"q": "草稿"}, cookies=cookies).json()["total"] == 0

    with session_scope() as s:
        f = s.get(ReportFile, fid)
        f.converted_at = dt.datetime.now(dt.timezone.utc)
        from app import search

        search.refresh_search_vector(s, rid)
        s.commit()
    assert _ids(api.get("/api/reports", params={"q": "草稿"}, cookies=cookies).json()) == {rid}


def test_search_follows_current_analysis_version(api: TestClient, login, make_user, make_report, llm_env) -> None:
    """run_analysis 挂点：总结随版本链头指针更新——旧版总结词退出、新版进入。"""
    from app.db import session_scope
    from app.models import ReportFile, ResearchReport
    from app import analysis

    rid, fid = make_report("正文与两个总结词均无关。", title="中性标题")
    llm_env(make_llm([
        json.dumps({"summary": "看好固态电池产业化进程。"}, ensure_ascii=False),
        json.dumps({"summary": "关注液冷散热渗透率提升。"}, ensure_ascii=False),
    ]))
    cookies = login(make_user("reader"))
    with session_scope() as s:
        report = s.get(ResearchReport, rid)
        file = s.get(ReportFile, fid)
        analysis.run_analysis(s, report, file)
        s.commit()
    assert _ids(api.get("/api/reports", params={"q": "固态电池"}, cookies=cookies).json()) == {rid}

    with session_scope() as s:
        report = s.get(ResearchReport, rid)
        file = s.get(ReportFile, fid)
        analysis.run_analysis(s, report, file)
        s.commit()
    # v2 成为当前版：旧总结词退出索引、新词进入
    assert api.get("/api/reports", params={"q": "固态电池"}, cookies=cookies).json()["total"] == 0
    assert _ids(api.get("/api/reports", params={"q": "液冷"}, cookies=cookies).json()) == {rid}


def test_search_end_to_end_after_conversion(
    api: TestClient, make_user, login, sample_pdf
) -> None:
    """上传 → worker 转换 persist → 正文可搜（真实 zhparser + 真实样本 PDF）。"""
    from app import worker

    analyst = login(make_user("analyst"))
    reader = login(make_user("reader"))
    data = sample_pdf("dongwu-002635-anjie-20241231.pdf")
    up = api.post(
        "/api/reports",
        files={"file": ("a.pdf", data, "application/pdf")},
        data={"broker": "东吴证券", "publish_date": "2024-12-31"},
        cookies=analyst,
    )
    assert up.status_code == 202, up.text
    rid = up.json()["report_id"]

    # 标题默认取文件名（无中文），转换完成前搜不到正文词
    assert api.get("/api/reports", params={"q": "安洁"}, cookies=reader).json()["total"] == 0

    claimed = worker.run_once()
    assert claimed is not None
    body = api.get(f"/api/tasks/{claimed.id}", cookies=analyst).json()
    assert body["status"] == "done", body

    ids = _ids(api.get("/api/reports", params={"q": "安洁"}, cookies=reader).json())
    assert ids == {rid}
