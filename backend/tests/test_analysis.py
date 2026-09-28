"""分析管道测试（#15 验收清单）。

LLM 质量不测（spike 已人工验收）：按 spec 次缝合口 1，用 httpx.MockTransport
预录 OpenAI 兼容端点响应，夹具是 research/analysis-spike/out/ 的 18 份真实
LLM 输出（已拷贝入库 tests/fixtures/analysis-spike/）。覆盖：清洗后输入 →
调用 → schema 落库 → 版本链 → 审计字段 → publish_date 锚定 → code_source
瀑布 → 枚举漂移归一 → 分块兜底 → worker 链式流转 → reanalyze/批量/tag API。
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.analysis as analysis
from app import worker
from app.errors import AnalysisError
from app.llm import LLMClient
from app.models import Analysis, PromptTemplate, ResearchReport, Role, Task, TaskStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "analysis-spike"
DONGWU = "dongwu-002635-anjie-20241231.cleaned.fulltext.json"
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "research" / "markitdown-samples"
DONGWU_PDF = "dongwu-002635-anjie-20241231.pdf"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# mock 凭据运行时随机生成（仓库约定：源码不含任何凭据字面量，含假的）
def _mock_api_key() -> str:
    return "test-" + secrets.token_hex(8)


# 合成研报正文：首页含发布日期（带 PDF 拆字空格）与代码，正文提及 002635
MD_WITH_ANCHORS = (
    "证券研究报告·公司点评·电子\n"
    "安洁科技（002635）动态跟踪点评报告：竞争力稳步提升，静待下游复苏\n"
    "2024 年 12月 31日\n"
    + "公司为国际主流客户提供精密功能件与结构件，消费电子与新能源汽车双轮驱动。" * 200
)
MD_NO_DATE = "安洁科技（002635）点评\n" + "全球消费电子需求复苏，AR/VR 终端出货量高增。" * 200


# ---------- mock LLM（预录响应按序回放，不碰网络） ----------

def make_llm(responses: list) -> LLMClient:
    """responses 每项：str=正常内容 / int=HTTP 错误码。队列耗尽后重复末项。"""
    queue = list(responses)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, int):
            return httpx.Response(item, text="mock llm error")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": item}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 340},
                "model": "mock-llm",
            },
        )

    client = LLMClient(
        "https://llm-mock.invalid/v1", _mock_api_key(), "mock-llm",
        transport=httpx.MockTransport(handler),
    )
    client.mock_calls = calls  # 测试回读：断言调用次数与消息形状
    return client


# llm_env / make_report fixture 已上移 conftest.py（#16 测试复用）；make_llm 保留在本模块。


def _run_task(api: TestClient, cookies: dict, task_id: int) -> dict:
    claimed = worker.run_once()
    assert claimed is not None and claimed.id == task_id
    return api.get(f"/api/tasks/{task_id}", cookies=cookies).json()


def _upload(api: TestClient, cookies: dict) -> dict:
    r = api.post(
        "/api/reports",
        files={"file": (DONGWU_PDF, (SAMPLES_DIR / "pdf" / DONGWU_PDF).read_bytes(), "application/pdf")},
        data={"broker": "东吴证券", "publish_date": "2024-12-31", "title": "安洁科技点评"},
        cookies=cookies,
    )
    assert r.status_code == 202, r.text
    return r.json()


# ---------- 管道：整篇单次 + schema 落库 + 审计 ----------

def test_fulltext_pipeline_lands_spec_schema(make_report) -> None:
    report_id, file_id = make_report(MD_WITH_ANCHORS)
    raw = fixture_json(DONGWU)
    raw["publish_date"] = "2025-01-15"  # LLM 回填日期 ≠ 首页日期：锚定必须赢

    from app.db import SessionLocal
    from app.models import ReportFile

    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.get(ResearchReport, report_id)
        file = s.get(ReportFile, file_id)
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()

        r = a.result
        assert r["publish_date"] == "2024-12-31"
        assert r["publish_date_source"] == "regex_head"
        assert r["broker"] == "东吴证券"
        assert r["targets"][0]["code"] == "002635"
        assert r["targets"][0]["code_source"] == "text"  # 002635 在正文出现
        assert r["rating"] == {"action": "买入", "maintained": True}
        assert len(llm.mock_calls) == 1  # 整篇单次调用
        assert llm.mock_calls[0]["messages"][0]["role"] == "system"

        # 审计四件套 + 研报指向当前版本
        assert a.prompt_version == "v1"
        assert a.model == "mock-llm"
        assert (a.prompt_tokens, a.completion_tokens) == (1200, 340)
        assert a.duration_ms >= 0
        assert report.current_analysis_id == a.id


def test_prompt_template_seeded_exactly_once(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    make_report(MD_WITH_ANCHORS)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)] * 2)
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        analysis.run_analysis(s, report, file, llm=llm)
        analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
        versions = s.scalars(select(PromptTemplate.version)).all()
    assert versions == ["v1"]  # 幂等 seed，不重复插入


def test_version_chain_increments_and_pointer_moves(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    make_report(MD_WITH_ANCHORS)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)] * 2)
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        v1 = analysis.run_analysis(s, report, file, llm=llm)
        v2 = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
        assert (v1.version, v2.version) == (1, 2)
        assert report.current_analysis_id == v2.id
        total = s.scalars(select(Analysis).where(Analysis.report_id == report.id)).all()
    assert len(total) == 2  # 完整版本链留档，不覆盖


# ---------- publish_date 锚定与 LLM 兜底 ----------

def test_code_absent_in_text_marks_inferred(make_report) -> None:
    """spike 结论：LLM 凭世界知识补的码可能恰好对但不可信——正文没有即 inferred。"""
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    raw["targets"][0]["code"] = "600519"  # 不在正文中
    make_report(MD_WITH_ANCHORS)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
    assert a.result["targets"][0]["code"] == "600519"
    assert a.result["targets"][0]["code_source"] == "inferred"


def test_publish_date_llm_fallback_is_marked_not_trusted(make_report) -> None:
    """正则锚不到时 LLM 日期可用，但必须带 llm 来源标记（不直接采信）。"""
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    raw["publish_date"] = "2024-12-31"
    make_report(MD_NO_DATE)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
    assert a.result["publish_date"] == "2024-12-31"
    assert a.result["publish_date_source"] == "llm"


def test_publish_date_missing_everywhere_is_null(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    raw["publish_date"] = None
    make_report(MD_NO_DATE)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
    assert a.result["publish_date"] is None
    assert a.result["publish_date_source"] is None


def test_anchor_rejects_chart_axis_date_row() -> None:
    """股价走势图坐标轴刻度行（一行多日期）不是发布日期（真实样本校准的回归）。"""
    md = "标题\n2024/1/2 2024/5/2 2024/8/31 | 2024/12/30\n正文" * 10
    assert analysis.anchor_publish_date(md, 3000) is None
    # 单一数字日期行可锚
    assert analysis.anchor_publish_date("发布日期：2025-03-12\n正文", 3000) == dt.date(2025, 3, 12)


# ---------- 枚举漂移归一 ----------

def test_enum_drift_normalized_to_spec_vocab(make_report) -> None:
    """spike 结论：LLM 偶尔回吐训练词（stance=增持 等），归一到 spec 终版枚举。"""
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = {
        "broker": "X证券",
        "authors": [{"name": "张三", "cert": "S0600522090001"}, {"name": " ", "cert": None}],
        "publish_date": None,
        "report_type": "公司深度报告",
        "title": "测试标题",
        "summary": "总结",
        "themes": [{"name": "AI算力", "reason": "依据"}, {"name": "  ", "reason": "空名"}],
        "targets": [
            {"code": "002635", "name": "安洁科技", "stance": "增持", "view": "v", "has_forecast": True},
            {"code": None, "name": "贵州茅台", "stance": "超推荐", "view": "", "has_forecast": False},
            {"code": "2635", "name": "坏代码", "stance": "回避", "view": "", "has_forecast": False},
        ],
        "rating": {"action": "维持增持", "maintained": "维持"},
        "risk_notes": "风险",
    }
    make_report(MD_NO_DATE)
    llm = make_llm([json.dumps(raw, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()

    r = a.result
    assert [t["stance"] for t in r["targets"]] == ["推荐", "提及", "回避"]
    assert r["targets"][1]["code"] is None and r["targets"][1]["code_source"] is None
    assert r["targets"][2]["code"] is None  # 非 6 位数字丢弃
    assert r["rating"]["action"] is None  # 不在枚举内 → null（宁缺毋滥）
    assert r["rating"]["maintained"] is None  # 非 bool → null
    assert r["report_type"] == "深度"  # 子串归一
    assert [t["name"] for t in r["themes"]] == ["AI算力"]  # 空名丢弃
    assert [a["name"] for a in r["authors"]] == ["张三"]


# ---------- 分块兜底（>50k 字符） ----------

def test_chunk_fallback_for_overlong_documents(make_report) -> None:
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import ReportFile

    s_cfg = get_settings()
    md = MD_WITH_ANCHORS + "长文本填充。" * 9000  # ≈61k 字符 > 50k 阈值
    assert len(md) > s_cfg.analysis_max_input_chars
    make_report(md)

    merged = fixture_json(DONGWU)
    partials = [
        json.dumps({"title": f"第{i}块局部", "targets": []}, ensure_ascii=False)
        for i in range(3)
    ]
    llm = make_llm(partials + [json.dumps(merged, ensure_ascii=False)])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()

    assert len(llm.mock_calls) == 4  # 3 块局部 + 1 合并
    assert "1/3" in llm.mock_calls[0]["messages"][1]["content"]  # 分块提示
    assert "合并" in llm.mock_calls[3]["messages"][1]["content"]
    assert a.prompt_tokens == 1200 * 4 and a.completion_tokens == 340 * 4  # 用量累计
    assert a.result["title"] == merged["title"]  # 落库的是合并结果


# ---------- LLM 失败路径 ----------

def test_invalid_json_recovers_from_prose_wrapping(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    raw = fixture_json(DONGWU)
    wrapped = "好的，以下是提取结果：\n" + json.dumps(raw, ensure_ascii=False) + "\n以上。"
    make_report(MD_WITH_ANCHORS)
    llm = make_llm([wrapped])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        a = analysis.run_analysis(s, report, file, llm=llm)
        s.commit()
    assert a.result["broker"] == "东吴证券"  # 文字包裹的 JSON 也能恢复


def test_garbage_response_raises_stable_error_code(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    make_report(MD_WITH_ANCHORS)
    llm = make_llm(["完全不是 JSON 的输出"])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        with pytest.raises(AnalysisError) as e:
            analysis.run_analysis(s, report, file, llm=llm)
    assert e.value.error_code == "llm_invalid_json"


def test_http_error_raises_stable_error_code(make_report) -> None:
    from app.db import SessionLocal
    from app.models import ReportFile

    make_report(MD_WITH_ANCHORS)
    llm = make_llm([500])
    with SessionLocal() as s:
        report = s.scalars(select(ResearchReport)).one()
        file = s.scalars(select(ReportFile)).one()
        with pytest.raises(AnalysisError) as e:
            analysis.run_analysis(s, report, file, llm=llm)
    assert e.value.error_code == "llm_http"


def test_json_object_fallback_when_endpoint_rejects_response_format() -> None:
    """端点不支持 response_format 时自动降级重试（spike 同款兜底）。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, text='{"error":"response_format not supported"}')
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"broker": "X"}'}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "m"},
        )

    client = LLMClient("https://llm-mock.invalid/v1", _mock_api_key(), "m", transport=httpx.MockTransport(handler))
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == '{"broker": "X"}'
    assert len(seen) == 2  # 带格式失败 → 无格式重试


# ---------- worker：链式流转（转换 done 后 analyzing → done） ----------

def test_convert_chains_into_analysis_end_to_end(api, make_user, login, llm_env) -> None:
    cookies = login(make_user(Role.ANALYST))
    raw = fixture_json(DONGWU)
    llm_env(make_llm([json.dumps(raw, ensure_ascii=False)]))

    body = _upload(api, cookies)
    task = _run_task(api, cookies, body["task_id"])

    assert task["status"] == "done"
    assert task["result"]["analysis"]["version"] == 1
    assert task["result"]["chars_cleaned"] > 0  # 转换产物仍完整上报

    # 版本史端点：审计字段齐全，读者也可见
    reader = login(make_user(Role.READER))
    r = api.get(f"/api/reports/{body['report_id']}/analyses", cookies=reader)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["version"] == 1 and item["prompt_version"] == "v1"
    assert item["model"] == "mock-llm"
    assert item["prompt_tokens"] == 1200 and item["completion_tokens"] == 340
    assert item["duration_ms"] >= 0
    assert item["result"]["targets"][0]["code_source"] == "text"  # 002635 真在正文
    assert item["result"]["publish_date"] == "2024-12-31"  # "2024 年 12月 31日" 锚定
    assert data["current_analysis_id"] == item["id"]

    detail = api.get(f"/api/reports/{body['report_id']}", cookies=reader).json()
    assert detail["current_analysis_id"] == item["id"]
    assert detail["tags"] == []


def test_convert_without_llm_skips_analysis(api, make_user, login) -> None:
    """LLM 未配置：转换照常完成（研报可读），分析显式记 skipped 而非失败。"""
    cookies = login(make_user(Role.ANALYST))
    body = _upload(api, cookies)
    task = _run_task(api, cookies, body["task_id"])
    assert task["status"] == "done"
    assert task["result"]["analysis"] == {"skipped": "llm_not_configured"}
    assert api.get(f"/api/reports/{body['report_id']}/analyses", cookies=cookies).json()["items"] == []


def test_analysis_failure_keeps_conversion_and_retry_succeeds(api, make_user, login, llm_env) -> None:
    """链式分析失败：任务 failed（error_code/stage），转换成果保留；重试只重分析。"""
    cookies = login(make_user(Role.ANALYST))
    raw = fixture_json(DONGWU)
    llm_env(make_llm([500]))  # LLM 挂

    body = _upload(api, cookies)
    task = _run_task(api, cookies, body["task_id"])
    assert task["status"] == "failed"
    assert task["result"]["error_code"] == "llm_http"
    assert task["result"]["stage"] == "analyze"

    # 正文已可读（转换成果未被分析失败回滚）
    md = api.get(f"/api/reports/{body['report_id']}/markdown", cookies=cookies)
    assert md.status_code == 200 and "安洁科技" in md.text

    # 换正常 mock 后重试 → 只重跑分析，v1 落库
    llm_env(make_llm([json.dumps(raw, ensure_ascii=False)]))
    retried = api.post(f"/api/tasks/{body['task_id']}/retry", cookies=cookies)
    assert retried.status_code == 200
    task = _run_task(api, cookies, body["task_id"])
    assert task["status"] == "done"
    assert task["result"]["analysis"]["version"] == 1


def test_stale_analyzing_task_is_reclaimed(db_engine) -> None:
    from app.db import SessionLocal

    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    with SessionLocal() as s:
        s.add(Task(kind="analyze", status=TaskStatus.ANALYZING, payload={"report_id": 1}, claimed_at=stale))
        s.commit()

    claimed = worker.claim_next_task()
    assert claimed is not None
    assert claimed.status == TaskStatus.ANALYZING  # analyze 任务重领仍是 analyzing
    assert claimed.attempts == 1


def test_fresh_analyzing_task_not_reclaimed(db_engine) -> None:
    from app.db import SessionLocal

    with SessionLocal() as s:
        s.add(Task(
            kind="analyze", status=TaskStatus.ANALYZING, payload={"report_id": 1},
            claimed_at=dt.datetime.now(dt.timezone.utc),
        ))
        s.commit()
    assert worker.claim_next_task() is None


# ---------- API：reanalyze / 批量重跑 ----------

def test_reanalyze_single_increments_version(api, make_user, login, llm_env) -> None:
    cookies = login(make_user(Role.ANALYST))
    raw = fixture_json(DONGWU)
    llm_env(make_llm([json.dumps(raw, ensure_ascii=False)] * 3))

    body = _upload(api, cookies)
    _run_task(api, cookies, body["task_id"])  # 链式 v1

    r = api.post(f"/api/reports/{body['report_id']}/reanalyze", cookies=cookies)
    assert r.status_code == 202
    task = _run_task(api, cookies, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["result"]["analysis"]["version"] == 2

    history = api.get(f"/api/reports/{body['report_id']}/analyses", cookies=cookies).json()
    assert [i["version"] for i in history["items"]] == [2, 1]  # 最新在前，历史留档
    assert history["current_analysis_id"] == history["items"][0]["id"]


def test_reanalyze_permission_and_preconditions(api, make_user, login, llm_env, make_report) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    raw = fixture_json(DONGWU)
    llm_env(make_llm([json.dumps(raw, ensure_ascii=False)]))

    converted_id, _ = make_report(MD_WITH_ANCHORS)
    unconverted_id, _ = make_report(None)

    assert api.post(f"/api/reports/{converted_id}/reanalyze", cookies=reader).status_code == 403
    assert api.post("/api/reports/999999/reanalyze", cookies=analyst).status_code == 404
    assert api.post(f"/api/reports/{unconverted_id}/reanalyze", cookies=analyst).status_code == 409

    r = api.post(f"/api/reports/{converted_id}/reanalyze", cookies=analyst)
    assert r.status_code == 202 and r.json()["report_id"] == converted_id


def test_reanalyze_503_when_llm_not_configured(api, make_user, login, make_report) -> None:
    cookies = login(make_user(Role.ANALYST))
    report_id, _ = make_report(MD_WITH_ANCHORS)
    assert api.post(f"/api/reports/{report_id}/reanalyze", cookies=cookies).status_code == 503


def test_batch_reanalyze_creates_tasks_and_reports_skips(api, make_user, login, llm_env, make_report) -> None:
    analyst = make_user(Role.ANALYST)
    cookies = login(analyst)
    raw = fixture_json(DONGWU)
    llm_env(make_llm([json.dumps(raw, ensure_ascii=False)] * 5))

    a_id, _ = make_report(MD_WITH_ANCHORS, title="批量A", owner=analyst)
    b_id, _ = make_report(MD_WITH_ANCHORS, title="批量B", owner=analyst)
    unconverted_id, _ = make_report(None, title="未转换", owner=analyst)
    deleted_id, _ = make_report(MD_WITH_ANCHORS, title="已删除", owner=analyst)
    assert api.delete(f"/api/reports/{deleted_id}", cookies=cookies).status_code == 204

    r = api.post(
        "/api/reports/reanalyze",
        json={"report_ids": [a_id, b_id, a_id, unconverted_id, deleted_id, 999999]},
        cookies=cookies,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["tasks"]) == 2  # a 去重 + b
    assert {s["reason"] for s in body["skipped"]} == {"markdown_missing", "not_found"}

    for t in body["tasks"]:
        task = _run_task(api, cookies, t["task_id"])
        assert task["status"] == "done"
        assert task["result"]["analysis"]["version"] == 1


def test_batch_reanalyze_validation_and_permission(api, make_user, login) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    assert api.post("/api/reports/reanalyze", json={"report_ids": []}, cookies=analyst).status_code == 422
    assert api.post(
        "/api/reports/reanalyze", json={"report_ids": list(range(201))}, cookies=analyst
    ).status_code == 422
    assert api.post("/api/reports/reanalyze", json={"report_ids": [1]}, cookies=reader).status_code == 403


# ---------- API：自由 tag ----------

def test_tag_add_list_remove_and_normalization(api, make_user, login, make_report) -> None:
    analyst = login(make_user(Role.ANALYST))
    report_id, _ = make_report(MD_WITH_ANCHORS)

    r = api.post(f"/api/reports/{report_id}/tags", json={"name": "  深度报告 "}, cookies=analyst)
    assert r.status_code == 201 and r.json()["name"] == "深度报告" and r.json()["linked"] is False

    # 幂等重打：200 + linked=True，且不产生第二个同名 tag
    dup = api.post(f"/api/reports/{report_id}/tags", json={"name": "深度报告"}, cookies=analyst)
    assert dup.status_code == 200 and dup.json()["linked"] is True
    assert dup.json()["id"] == r.json()["id"]

    # 全角数字归一到半角
    r2 = api.post(f"/api/reports/{report_id}/tags", json={"name": "２０２５财报季"}, cookies=analyst)
    assert r2.json()["name"] == "2025财报季"
    assert api.get(f"/api/reports/{report_id}", cookies=analyst).json()["tags"] == ["深度报告", "2025财报季"]

    assert api.delete(f"/api/reports/{report_id}/tags/深度报告", cookies=analyst).status_code == 204
    assert api.get(f"/api/reports/{report_id}", cookies=analyst).json()["tags"] == ["2025财报季"]
    assert api.delete(f"/api/reports/{report_id}/tags/深度报告", cookies=analyst).status_code == 404
    assert api.delete(f"/api/reports/{report_id}/tags/不存在", cookies=analyst).status_code == 404


def test_tag_permissions_and_not_found(api, make_user, login, make_report) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))
    report_id, _ = make_report(MD_WITH_ANCHORS)

    assert api.post(f"/api/reports/{report_id}/tags", json={"name": "x"}, cookies=reader).status_code == 403
    assert api.delete(f"/api/reports/{report_id}/tags/x", cookies=reader).status_code == 403
    assert api.post("/api/reports/999999/tags", json={"name": "x"}, cookies=analyst).status_code == 404
    assert api.post(f"/api/reports/{report_id}/tags", json={"name": ""}, cookies=analyst).status_code == 422
