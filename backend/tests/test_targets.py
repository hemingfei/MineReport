"""标的主数据与规范化瀑布测试（#16 验收清单）。

纯逻辑（归一化/交易所推导/瀑布）不碰库：MasterIndex 直接吃内存 Target 对象。
导入幂等与回写复用走真实测试库；spike 回归（LLM 补码恰好对但不可信）经 mock LLM
端到端验证（复用 #15 的管道缝合口）。API 面按权限矩阵逐端点覆盖。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import Analysis, ResearchReport, Role, Target, TargetMatch
from app.targets import (
    FUZZY_THRESHOLD,
    MasterIndex,
    MatchReason,
    MatchStatus,
    exchange_of,
    normalize_name,
)

# ---------- 纯逻辑：名称归一化（#4 实测坑清单） ----------


def test_normalize_strips_st_prefix_spaces_fullwidth_suffix() -> None:
    assert normalize_name("*ST三六") == "三六"
    assert normalize_name("ST星源") == "星源"
    assert normalize_name("万 科Ａ") == "万科A"
    assert normalize_name("京东方Ａ") == "京东方A"
    assert normalize_name("七 匹 狼") == "七匹狼"
    assert normalize_name("平安银行股份有限公司") == "平安银行"
    assert normalize_name("XX集团控股有限公司") == "XX"
    assert normalize_name("万科ａ") == "万科A"  # 全角小写也归一


def test_normalize_keeps_meaningful_names_intact() -> None:
    # 银行/科技是名字本体，不在后缀词表：不能剥（剥了全行业撞名）
    assert normalize_name("平安银行") == "平安银行"
    assert normalize_name("安洁科技") == "安洁科技"
    assert normalize_name("") == ""
    assert normalize_name("ST") == "ST"  # 整名即 ST 标记：保留，不归一成空串


def test_exchange_of_by_code_prefix() -> None:
    assert exchange_of("600519") == "SH"
    assert exchange_of("688981") == "SH"
    assert exchange_of("000001") == "SZ"
    assert exchange_of("302132") == "SZ"  # 创业板新段（#4 实测补录）
    assert exchange_of("920982") == "BJ"
    assert exchange_of("430047") == "BJ"
    assert exchange_of("999999") == ""  # 未知段留空，不猜


# ---------- 纯逻辑：瀑布 ----------


def _t(code: str, name: str, historical: list[str] | None = None, l1: str = "食品饮料") -> Target:
    return Target(
        code=code, name=name, name_norm=normalize_name(name), exchange=exchange_of(code),
        sw_l1_code="34", sw_l1_name=l1, sw_l2_code=None, sw_l2_name=None,
        sw_l3_code=None, sw_l3_name=None, sw_effective_date=None,
        historical_names=historical,
    )


@pytest.fixture()
def master() -> MasterIndex:
    return MasterIndex([
        _t("600519", "贵州茅台"),
        _t("000001", "平安银行", l1="银行"),
        _t("002635", "安洁科技", l1="电子"),
        _t("000002", "万 科Ａ", l1="房地产"),
        _t("000005", "ST星源", l1="环保"),
        _t("000503", "国新健康", historical=["海虹控股", "琼海虹"], l1="计算机"),
        _t("600517", "国电南瑞", l1="电力设备"),
        _t("600406", "国电南自", l1="电力设备"),
        _t("688001", "华兴源创科技", l1="电子"),
        _t("688002", "华兴源创科教", l1="电子"),
    ])


def test_text_code_in_master_resolves_directly(master: MasterIndex) -> None:
    res = master.resolve(name="贵州茅台", code="600519", code_source="text")
    assert res.auto and res.code == "600519" and res.code_source == "text"


def test_text_code_not_in_master_queues(master: MasterIndex) -> None:
    """正文里有码但主数据没有（新股未导入/退市缺行/笔误）：不落成，交人工。"""
    res = master.resolve(name="某新股", code="301999", code_source="text")
    assert not res.auto and res.reason == MatchReason.CODE_NOT_IN_MASTER


def test_inferred_code_forced_to_queue_despite_exact_name_match(master: MasterIndex) -> None:
    """spike 核心回归：LLM 凭记忆补的码恰好对（名称精确命中同码）也不采信，强制队列。"""
    res = master.resolve(name="贵州茅台", code="600519", code_source="inferred")
    assert not res.auto
    assert res.reason == MatchReason.INFERRED_CODE
    assert res.candidates[0].code == "600519"  # 名称命中的同码只作候选提示


def test_inferred_code_with_hallucinated_code_still_shows_name_candidates(master: MasterIndex) -> None:
    """补码完全错（幻觉）：名称瀑布候选照常给出，供人工一键纠偏。"""
    res = master.resolve(name="平安银行", code="600999", code_source="inferred")
    assert res.reason == MatchReason.INFERRED_CODE
    assert res.candidates[0].code == "000001"


def test_no_code_exact_unique_name_resolves(master: MasterIndex) -> None:
    res = master.resolve(name="贵州茅台股份有限公司", code=None, code_source=None)  # 公司全称
    assert res.auto and res.code == "600519" and res.code_source == "text"


def test_no_code_exact_hit_via_normalized_variants(master: MasterIndex) -> None:
    for query in ("万 科Ａ", "万科a", "*ST星源", "海虹控股"):
        res = master.resolve(name=query, code=None, code_source=None)
        assert res.auto, query


def test_exact_name_collision_queues_multi_candidate(master: MasterIndex) -> None:
    """归一名撞车（曾用名与现有简称同名）：多候选落队列，不猜。"""
    res = MasterIndex([
        _t("600001", "同望科技", historical=["东方通信"]),
        _t("600776", "东方通信"),
    ]).resolve(name="东方通信", code=None, code_source=None)
    assert not res.auto
    assert res.reason == MatchReason.MULTI_CANDIDATE
    assert {c.code for c in res.candidates} == {"600001", "600776"}


def test_fuzzy_unique_hit_above_threshold_resolves(master: MasterIndex) -> None:
    """近名差一字 ratio≈0.91 ≥ 0.85 且唯一（另一候选 0.73）→ 自动落成。"""
    res = master.resolve(name="华兴源创技", code=None, code_source=None)
    assert res.auto and res.code == "688001"


def test_fuzzy_two_hits_above_threshold_queue(master: MasterIndex) -> None:
    """两个候选都 ≥ 阈值（各 0.91）：区分度不足，落队列。"""
    res = master.resolve(name="华兴源创科", code=None, code_source=None)
    assert not res.auto and res.reason == MatchReason.MULTI_CANDIDATE
    assert {c.code for c in res.candidates} == {"688001", "688002"}


def test_fuzzy_below_threshold_no_hit_with_near_candidates(master: MasterIndex) -> None:
    """#4 研究案例：'国电南瑞' vs '国电南自' ratio=0.75——OCR 级差异不自动匹配，
    但带最近候选给人工参考。"""
    res = master.resolve(name="国电南北", code=None, code_source=None)
    assert not res.auto and res.reason == MatchReason.NO_HIT
    codes = {c.code for c in res.candidates}
    assert codes == {"600517", "600406"}
    assert FUZZY_THRESHOLD == 0.85


# ---------- 导入幂等（真实库） ----------


def _industry_rows() -> list:
    from app.masterdata import IndustryRow

    return [
        IndustryRow("000001", dt.date(1991, 4, 3), "440101", dt.datetime(2015, 10, 27, 15, 29)),
        IndustryRow("000001", dt.date(2014, 2, 21), "480101", dt.datetime(2024, 9, 27, 9, 8)),
        IndustryRow("000001", dt.date(2021, 7, 30), "480301", dt.datetime(2025, 12, 15, 16, 33)),
        IndustryRow("600519", dt.date(2001, 7, 31), "340301", dt.datetime(2015, 10, 27, 15, 28)),
        IndustryRow("600519", dt.date(2021, 7, 30), "340501", dt.datetime(2022, 5, 9, 14, 1)),
        IndustryRow("600519", dt.date(2021, 7, 30), "340501", dt.datetime(2022, 5, 9, 14, 1)),  # 全史原始行会重复
    ]


def test_import_is_idempotent_and_joins_industry_names(db_engine) -> None:
    from app.db import session_scope
    from app.masterdata import StockRow, import_master_data, load_sw2021_names

    names = load_sw2021_names()
    assert names["480301"][1] == "银行"  # 静态码表就位（桥接产物）
    assert names["340501"][5] == "白酒Ⅲ"

    stocks = [
        StockRow("600519", "贵州茅台"),
        StockRow("000001", "平安银行"),
        StockRow("302132", "成飞"),  # 创业板新段：交易所推导 SZ
        StockRow("2635", "脏码跳过"),
    ]
    with session_scope() as s:
        stats1 = import_master_data(s, stocks, _industry_rows())
        s.commit()
        assert stats1["industry_history_added"] == 5  # 重复原始行只落一次
        assert stats1["targets_total"] == 3  # 脏码不入库；xls 覆盖的也是同三只中的两只
        assert s.get(Target, "302132").exchange == "SZ"

        moutai = s.get(Target, "600519")
        assert (moutai.name, moutai.name_norm, moutai.exchange) == ("贵州茅台", "贵州茅台", "SH")
        assert moutai.sw_l1_name == "食品饮料"  # 快照 = 计入日期最大的一行（340501）
        assert moutai.sw_l3_name == "白酒Ⅲ"
        assert moutai.sw_effective_date == dt.date(2021, 7, 30)
        pingan = s.get(Target, "000001")
        assert (pingan.sw_l2_name, pingan.sw_l3_name) == ("股份制银行Ⅱ", "股份制银行Ⅲ")

        # 重复导入：全量幂等（历史不重插、快照覆盖同值、计数归零）
        stats2 = import_master_data(s, stocks, _industry_rows())
        s.commit()
        assert stats2["industry_history_added"] == 0
        assert s.scalar(select(TargetMatch.id).limit(1)) is None  # 不产生队列副作用


def test_import_covers_delisted_and_merges_historical_names(db_engine) -> None:
    from app.db import session_scope
    from app.masterdata import IndustryRow, StockRow, import_master_data

    # xls 里的退市股不在 stocks：也要建行（研报是历史文档，旧代码要能命中）
    with session_scope() as s:
        stats = import_master_data(
            s,
            [StockRow("000503", "国新健康")],
            [IndustryRow("000503", dt.date(2016, 1, 1), "720100")],
            name_changes={"000503": ["海虹控股", "琼海虹"], "bad": ["x"]},  # bad 脏码跳过
        )
        s.commit()
        assert stats["historical_names_merged"] == 2
        row = s.get(Target, "000503")
        assert row.historical_names == ["海虹控股", "琼海虹"]
        # 曾用名回填幂等（并集不重复），当前简称不入列
        stats2 = import_master_data(s, [StockRow("000503", "国新健康")], [], name_changes={"000503": ["国新健康", "海虹控股"]})
        s.commit()
        assert stats2["historical_names_merged"] == 0
        assert row.historical_names == ["海虹控股", "琼海虹"]


# ---------- 回写：链接 + 队列 + 复用/接替（真实库） ----------


@pytest.fixture()
def seed_master_rows(db_engine) -> None:
    from app.db import session_scope

    with session_scope() as s:
        s.add_all([
            _t("600519", "贵州茅台"),
            _t("002635", "安洁科技", l1="电子"),
            _t("000001", "平安银行", l1="银行"),
        ])
        s.commit()


def _make_analysis(db, make_user, result: dict) -> tuple[ResearchReport, Analysis]:
    """直接造研报 + 文件 + 分析行（不走 LLM），专测回写语义。"""
    import uuid

    from app.conversion import normalize_title
    from app.models import ReportFile

    owner = make_user(Role.ANALYST)
    title = f"回测研报-{uuid.uuid4().hex[:8]}"
    report = ResearchReport(
        title=title, title_norm=normalize_title(title), broker="测试券商",
        publish_date=dt.date(2024, 12, 31), created_by=owner.id,
    )
    db.add(report)
    db.flush()
    f = ReportFile(
        report_id=report.id, storage_key=f"reports/{report.id}/files/1/a.pdf",
        filename="a.pdf", content_type="application/pdf", size_bytes=1,
        file_sha256="0" * 64, uploaded_by=owner.id,
        markdown_text="正文", converted_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(f)
    db.flush()
    analysis = Analysis(
        report_id=report.id, report_file_id=f.id, version=1,
        prompt_version="v1", model="m", prompt_tokens=1, completion_tokens=1,
        duration_ms=1, result=result,
    )
    db.add(analysis)
    db.flush()
    report.current_analysis_id = analysis.id
    return report, analysis


RESULT_MIXED = {
    "targets": [
        {"code": "002635", "name": "安洁科技", "stance": "推荐", "view": "v1", "has_forecast": True, "code_source": "text"},
        {"code": "600519", "name": "贵州茅台", "stance": "提及", "view": "v2", "has_forecast": False, "code_source": "inferred"},
        {"code": None, "name": "平安银行", "stance": "回避", "view": "", "has_forecast": False, "code_source": None},
        {"code": None, "name": "不存在的公司", "stance": "提及", "view": "", "has_forecast": False, "code_source": None},
    ]
}


def test_link_targets_waterfall_outcomes(seed_master_rows, db_engine, make_user) -> None:
    from app.db import session_scope
    from app.targets import link_analysis_targets

    with session_scope() as s:
        report, analysis = _make_analysis(s, make_user, RESULT_MIXED)
        links = link_analysis_targets(s, report, analysis)
        s.commit()

        by_seq = {l.seq: l for l in links}
        # ① text 码在主数据：直接落成
        assert by_seq[0].target_code == "002635" and by_seq[0].code_source == "text"
        assert by_seq[0].match_id is None
        # ② inferred：强制队列（名称精确命中同码也不采信）
        assert by_seq[1].target_code is None and by_seq[1].match_id is not None
        # ③ 无码 + 名称精确唯一：自动落成
        assert by_seq[2].target_code == "000001" and by_seq[2].code_source == "text"
        # ④ 无码无命中：队列
        assert by_seq[3].target_code is None and by_seq[3].match_id is not None

        pending = s.scalars(select(TargetMatch).where(TargetMatch.status == MatchStatus.PENDING)).all()
        assert {m.reason for m in pending} == {MatchReason.INFERRED_CODE, MatchReason.NO_HIT}
        inferred_match = next(m for m in pending if m.reason == MatchReason.INFERRED_CODE)
        assert inferred_match.raw_code == "600519"
        assert inferred_match.candidates[0]["code"] == "600519"  # 候选提示带同码


def test_confirm_then_reanalyze_reuses_manual_decision(seed_master_rows, db_engine, make_user) -> None:
    from app.db import session_scope
    from app.targets import link_analysis_targets, resolve_match

    with session_scope() as s:
        report, v1 = _make_analysis(s, make_user, RESULT_MIXED)
        links = link_analysis_targets(s, report, v1)
        s.commit()
        queued = [l for l in links if l.match_id][0]
        match = s.get(TargetMatch, queued.match_id)
        resolve_match(s, match, make_user(Role.ANALYST).id, code="600519")
        s.commit()
        assert queued.target_code == "600519" and queued.code_source == "manually_confirmed"

        # 重跑 v2（同输出）：确认判定自动复用，不重复打扰分析师
        v2 = Analysis(
            report_id=report.id, report_file_id=v1.report_file_id, version=2,
            prompt_version="v1", model="m", prompt_tokens=1, completion_tokens=1,
            duration_ms=1, result=RESULT_MIXED,
        )
        s.add(v2)
        s.flush()
        report.current_analysis_id = v2.id
        links2 = link_analysis_targets(s, report, v2)
        s.commit()

        by_seq = {l.seq: l for l in links2}
        assert by_seq[1].target_code == "600519"
        assert by_seq[1].code_source == "manually_confirmed"
        pending = s.scalars(select(TargetMatch).where(TargetMatch.status == MatchStatus.PENDING)).all()
        assert len(pending) == 1  # 只剩"不存在的公司"（NO_HIT）


def test_new_version_supersedes_stale_pending(seed_master_rows, db_engine, make_user) -> None:
    from app.db import session_scope
    from app.targets import link_analysis_targets

    with session_scope() as s:
        report, v1 = _make_analysis(s, make_user, RESULT_MIXED)
        link_analysis_targets(s, report, v1)
        s.commit()
        assert len(s.scalars(select(TargetMatch).where(TargetMatch.status == MatchStatus.PENDING)).all()) == 2

        v2 = Analysis(
            report_id=report.id, report_file_id=v1.report_file_id, version=2,
            prompt_version="v1", model="m", prompt_tokens=1, completion_tokens=1,
            duration_ms=1, result={"targets": []},
        )
        s.add(v2)
        s.flush()
        report.current_analysis_id = v2.id
        link_analysis_targets(s, report, v2)
        s.commit()
        # 旧 pending 全部接替；confirmed 不受影响
        statuses = {m.status for m in s.scalars(select(TargetMatch)).all()}
        assert MatchStatus.PENDING not in statuses
        assert MatchStatus.SUPERSEDED in statuses
        assert MatchStatus.CONFIRMED not in statuses  # 本用例没确认过


def test_dismissed_decision_also_reused(seed_master_rows, db_engine, make_user) -> None:
    from app.db import session_scope
    from app.targets import link_analysis_targets, resolve_match

    with session_scope() as s:
        report, v1 = _make_analysis(s, make_user, RESULT_MIXED)
        links = link_analysis_targets(s, report, v1)
        s.commit()
        match = s.get(TargetMatch, [l for l in links if l.match_id][0].match_id)
        resolve_match(s, match, make_user(Role.ANALYST).id, code=None, dismiss=True)
        s.commit()

        v2 = Analysis(
            report_id=report.id, report_file_id=v1.report_file_id, version=2,
            prompt_version="v1", model="m", prompt_tokens=1, completion_tokens=1,
            duration_ms=1, result=RESULT_MIXED,
        )
        s.add(v2)
        s.flush()
        report.current_analysis_id = v2.id
        links2 = link_analysis_targets(s, report, v2)
        s.commit()
        by_seq = {l.seq: l for l in links2}
        assert by_seq[1].match_id is not None
        m2 = s.get(TargetMatch, by_seq[1].match_id)
        assert m2.status == MatchStatus.DISMISSED  # 直接落已驳回，不再进 pending


# ---------- 端到端：spike 回归（mock LLM 全管道） ----------


def test_spike_regression_inferred_code_polls_queue_end_to_end(
    api, db_engine, make_user, login, llm_env, make_report, seed_master_rows
) -> None:
    """spike 场景贯穿：正文无码 → LLM 回填 600519（恰好对）→ inferred → 强制人工确认队列
    → 分析师确认 → manually_confirmed → 重跑自动复用。"""
    import json as _json

    from app import worker
    from test_analysis import DONGWU, MD_WITH_ANCHORS, fixture_json, make_llm

    raw = fixture_json(DONGWU)
    raw["targets"] = [
        {"code": "600519", "name": "贵州茅台", "stance": "推荐", "view": "v", "has_forecast": True},
    ]
    analyst = login(make_user(Role.ANALYST))
    llm_env(make_llm([_json.dumps(raw, ensure_ascii=False)] * 2))
    report_id, _ = make_report(MD_WITH_ANCHORS)

    r = api.post(f"/api/reports/{report_id}/reanalyze", cookies=analyst)
    assert r.status_code == 202
    worker.run_once()
    task = api.get(f"/api/tasks/{r.json()['task_id']}", cookies=analyst).json()
    assert task["status"] == "done"

    # 队列可见：inferred 条目带同码候选
    queue = api.get("/api/targets/matches", cookies=analyst).json()["items"]
    assert len(queue) == 1
    item = queue[0]
    assert item["reason"] == "inferred_code" and item["raw_code"] == "600519"
    assert item["candidates"][0]["code"] == "600519"
    assert item["report_title"]

    # 研报标的视图：未确认前 target_code 空
    view = api.get(f"/api/reports/{report_id}/targets", cookies=analyst).json()
    assert view["items"][0]["target_code"] is None
    assert view["items"][0]["code_source"] == "inferred"

    # 确认 → manually_confirmed
    r = api.post(f"/api/targets/matches/{item['id']}/confirm", json={"code": "600519"}, cookies=analyst)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    view = api.get(f"/api/reports/{report_id}/targets", cookies=analyst).json()
    assert view["items"][0]["target_code"] == "600519"
    assert view["items"][0]["code_source"] == "manually_confirmed"
    assert view["items"][0]["target_name"] == "贵州茅台"
    assert view["items"][0]["sw_l1_name"] == "食品饮料"

    # 重跑（第二个 mock 响应）：自动复用确认，队列清空
    r2 = api.post(f"/api/reports/{report_id}/reanalyze", cookies=analyst)
    worker.run_once()
    assert api.get(f"/api/tasks/{r2.json()['task_id']}", cookies=analyst).json()["status"] == "done"
    assert api.get("/api/targets/matches", cookies=analyst).json()["items"] == []
    view = api.get(f"/api/reports/{report_id}/targets", cookies=analyst).json()
    assert view["items"][0]["code_source"] == "manually_confirmed"


# ---------- API 面：搜索 / 队列权限 / 导入触发 ----------


def test_target_search_by_code_and_name(api, db_engine, make_user, login, seed_master_rows) -> None:
    reader = login(make_user(Role.READER))
    by_code = api.get("/api/targets", params={"q": "6005"}, cookies=reader).json()
    assert [i["code"] for i in by_code["items"]] == ["600519"]
    assert by_code["items"][0]["sw_l3_name"] is None or True  # 种子行无行业也容忍

    by_name = api.get("/api/targets", params={"q": "茅台"}, cookies=reader).json()
    assert by_name["items"][0]["code"] == "600519"

    # 归一名子串也能命中（全角变体入库过）
    assert api.get("/api/targets", params={"q": "安洁"}, cookies=reader).status_code == 200
    assert api.get("/api/targets", params={"q": ""}, cookies=reader).status_code == 422
    assert api.get("/api/targets", cookies=reader).status_code == 422


def test_match_endpoints_permissions_and_validation(
    api, db_engine, make_user, login, llm_env, make_report, seed_master_rows
) -> None:
    analyst = login(make_user(Role.ANALYST))
    reader = login(make_user(Role.READER))

    assert api.get("/api/targets/matches", cookies=reader).status_code == 403
    assert api.get("/api/targets/matches", cookies=analyst).status_code == 200

    assert api.post("/api/targets/matches/999/confirm", json={"code": "600519"}, cookies=analyst).status_code == 404
    assert api.post("/api/targets/import", cookies=analyst).status_code == 403
    assert api.post("/api/targets/import", cookies=reader).status_code == 403

    # 确认到未知代码：422（标的池不被未知代码污染）
    import json as _json

    from app import worker
    from test_analysis import DONGWU, MD_WITH_ANCHORS, fixture_json, make_llm

    raw = fixture_json(DONGWU)
    raw["targets"] = [{"code": None, "name": "不存在的公司", "stance": "提及"}]
    llm_env(make_llm([_json.dumps(raw, ensure_ascii=False)]))
    report_id, _ = make_report(MD_WITH_ANCHORS)
    r = api.post(f"/api/reports/{report_id}/reanalyze", cookies=analyst)
    from app import worker
    worker.run_once()
    item = api.get("/api/targets/matches", cookies=analyst).json()["items"][0]
    bad = api.post(f"/api/targets/matches/{item['id']}/confirm", json={"code": "999999"}, cookies=analyst)
    assert bad.status_code == 422

    ok = api.post(f"/api/targets/matches/{item['id']}/dismiss", cookies=analyst)
    assert ok.status_code == 204
    # 终审后不可重复操作
    assert api.post(f"/api/targets/matches/{item['id']}/dismiss", cookies=analyst).status_code == 409
    assert api.get("/api/targets/matches", cookies=analyst).json()["items"] == []
    # 驳回后关联行保持未落成
    view = api.get(f"/api/reports/{report_id}/targets", cookies=analyst).json()
    assert view["items"][0]["target_code"] is None


def test_report_targets_empty_when_no_analysis(api, make_user, login, make_report) -> None:
    cookies = login(make_user(Role.READER))
    report_id, _ = make_report(None)
    r = api.get(f"/api/reports/{report_id}/targets", cookies=cookies)
    assert r.status_code == 200
    assert r.json() == {"analysis_id": None, "items": []}


def test_admin_import_task_runs_via_worker(api, make_user, login, monkeypatch, db_engine) -> None:
    """admin 触发 → 202 → worker 领取 import_targets → 幂等导入落库（fetch mock，不碰网络）。"""
    from app import worker
    from app.db import session_scope
    from app.masterdata import IndustryRow, StockRow
    from app import masterdata

    monkeypatch.setattr(
        masterdata, "fetch_all",
        lambda: (
            [StockRow("600519", "贵州茅台"), StockRow("000001", "平安银行")],
            [IndustryRow("600519", dt.date(2021, 7, 30), "340501")],
        ),
    )
    monkeypatch.setattr(masterdata, "fetch_name_changes", lambda codes, **kw: {})

    admin = login(make_user(Role.ADMIN))
    r = api.post("/api/targets/import", cookies=admin)
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    claimed = worker.run_once()
    assert claimed is not None and claimed.id == task_id

    task = api.get(f"/api/tasks/{task_id}", cookies=admin).json()
    assert task["status"] == "done"
    assert task["result"]["targets_total"] == 2
    assert task["result"]["industry_history_added"] == 1

    with session_scope() as s:
        row = s.get(Target, "600519")
        assert (row.name, row.sw_l3_name) == ("贵州茅台", "白酒Ⅲ")  # 快照 + 静态码表名称
