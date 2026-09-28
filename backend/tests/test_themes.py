"""题材词表测试（#17 验收清单）。

纯逻辑（归一/噪音过滤/查询词展开）不碰库；状态机与治理动作走 API 缝合口
（真实 Postgres）；词表关联经 mock LLM 端到端验证（复用 #15 管道缝合口）；
种子导入只喂行数据不碰网络（与 masterdata 同模式）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import (
    Analysis,
    AnalysisAuthor,
    ReportFile,
    ReportTheme,
    ResearchReport,
    Role,
    Target,
    Theme,
    ThemeMembership,
)
from app.themes import (
    ConceptSeed,
    MembershipSource,
    ThemeSource,
    ThemeStatus,
    expand_query_terms,
    import_theme_seeds,
    is_noise_concept,
    normalize_theme_name,
)

from test_analysis import make_llm

# ---------- 纯逻辑 ----------


def test_normalize_theme_name_collapses_width_and_spaces() -> None:
    assert normalize_theme_name("AI 算力") == normalize_theme_name("ＡＩ算力") == "AI算力"
    assert normalize_theme_name("  创新\t药 ") == "创新药"


def test_noise_concept_filter_keeps_real_themes() -> None:
    for name in ("AI算力", "创新药", "消费电子", "机器人概念", "国企改革"):
        assert not is_noise_concept(name), name


def test_noise_concept_filter_drops_market_noise() -> None:
    for name in (
        "昨日涨停", "昨日连板_含一字", "昨日触板", "百元股", "低价股", "高价股", "破净股",
        "微盘股", "低价微盘股", "大盘股", "中盘股", "小盘股", "ST股", "次新股", "注册制次新股",
        "科创板做市股", "融资融券", "转融券标的", "富时罗素概念", "MSCI中国", "标普道琼斯A股",
        "沪股通", "深股通", "机构重仓", "基金重仓", "QFII重仓", "社保重仓", "证金持股", "汇金持股",
        "央视50_", "上证50_", "上证180_", "中证500", "沪深300_", "创业板综", "含可转债", "AH股",
    ):
        assert is_noise_concept(name), name


def test_expand_query_terms_for_subscription() -> None:
    """订阅查询词展开（#19 消费）：题材名 + 同义词，归一去重、保序。"""
    theme = Theme(
        name="算力", name_norm="算力", status=ThemeStatus.ACTIVE,
        synonyms=["AI算力", "AI 算力", "算力"],
    )
    assert expand_query_terms(theme) == ["算力", "AI算力"]


# ---------- 库内造数 helpers ----------


@pytest.fixture()
def db_session(db_engine):
    from app.db import session_scope

    # 退出时 commit 是安全网：造数路径均已显式提交，无 teardown 丢弃语义
    with session_scope() as s:
        yield s


def _mk_target(s, code: str, name: str) -> Target:
    from app.targets import exchange_of, normalize_name

    t = Target(code=code, name=name, name_norm=normalize_name(name), exchange=exchange_of(code))
    s.add(t)
    s.flush()
    return t


def _mk_theme(s, name: str, status: str = ThemeStatus.ACTIVE, **kw) -> Theme:
    kw.setdefault("source", ThemeSource.MANUAL)
    theme = Theme(name=name, name_norm=normalize_theme_name(name), status=status, **kw)
    s.add(theme)
    s.flush()
    return theme


def _mk_analyzed_report(s, owner_id: int, *, title: str, theme: Theme, raw_names: list[str]) -> ResearchReport:
    """造一篇带当前分析版本的研报，raw_names 逐条挂到 theme（当前版关联）。"""
    from app.conversion import normalize_title

    report = ResearchReport(
        title=title, title_norm=normalize_title(title), broker="测试券商",
        publish_date=dt.date(2024, 12, 31), created_by=owner_id,
    )
    s.add(report)
    s.flush()
    f = ReportFile(
        report_id=report.id, storage_key=f"reports/{report.id}/files/1/a.pdf",
        filename="a.pdf", content_type="application/pdf", size_bytes=1,
        file_sha256="0" * 64, uploaded_by=owner_id, markdown_text="x",
        converted_at=dt.datetime.now(dt.timezone.utc),
    )
    s.add(f)
    s.flush()
    a = Analysis(
        report_id=report.id, report_file_id=f.id, version=1, prompt_version="v1",
        model="m", prompt_tokens=1, completion_tokens=1, duration_ms=1, result={},
    )
    s.add(a)
    s.flush()
    report.current_analysis_id = a.id
    for seq, raw in enumerate(raw_names):
        s.add(ReportTheme(analysis_id=a.id, report_id=report.id, seq=seq, theme_id=theme.id, raw_name=raw))
    return report


# ---------- 状态机与治理（API 缝合口） ----------


def test_propose_lands_pending_and_admin_approves(api, login, make_user) -> None:
    analyst, admin, reader = make_user(Role.ANALYST), make_user(Role.ADMIN), make_user(Role.READER)

    r = api.post("/api/themes", json={"name": "低空经济"}, cookies=login(analyst))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["source"] == "manual"
    theme_id = body["id"]

    assert api.post("/api/themes", json={"name": "x"}, cookies=login(reader)).status_code == 403
    assert api.patch(
        f"/api/themes/{theme_id}", json={"action": "approve"}, cookies=login(analyst)
    ).status_code == 403

    r = api.patch(
        f"/api/themes/{theme_id}",
        json={"action": "approve", "definition": "eVTOL 与低空基础设施",
              "synonyms": ["eVTOL", "飞行汽车"]},
        cookies=login(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["definition"] == "eVTOL 与低空基础设施"
    assert body["synonyms"] == ["eVTOL", "飞行汽车"]

    # 状态机违例：已审不可再审
    assert api.patch(
        f"/api/themes/{theme_id}", json={"action": "approve"}, cookies=login(admin)
    ).status_code == 409


def test_propose_duplicate_conflicts_with_active_and_pending(api, login, make_user, db_session) -> None:
    analyst = make_user(Role.ANALYST)
    _mk_theme(db_session, "AI算力", synonyms=["算力基础设施"])
    db_session.commit()

    for name in ("AI算力", "ＡＩ算力 ", "算力基础设施"):  # 撞在册名 / 全角变体 / 在册同义词
        r = api.post("/api/themes", json={"name": name}, cookies=login(analyst))
        assert r.status_code == 409, name
    assert api.post("/api/themes", json={"name": "固态电池"}, cookies=login(analyst)).status_code == 201
    assert api.post("/api/themes", json={"name": "固态电池"}, cookies=login(analyst)).status_code == 409


def test_retire_pending_and_active(api, login, make_user, db_session) -> None:
    admin = make_user(Role.ADMIN)
    theme = _mk_theme(db_session, "mrna")
    pending = _mk_theme(db_session, "脑机接口", ThemeStatus.PENDING)
    db_session.commit()

    assert api.patch(
        f"/api/themes/{pending.id}", json={"action": "retire"}, cookies=login(admin)
    ).json()["status"] == "retired"
    assert api.patch(
        f"/api/themes/{theme.id}", json={"action": "retire"}, cookies=login(admin)
    ).status_code == 200
    # 已停用不可再停用（状态机终点无出边）
    assert api.patch(
        f"/api/themes/{theme.id}", json={"action": "retire"}, cookies=login(admin)
    ).status_code == 409


def test_merge_migrates_synonyms_members_and_links(api, login, make_user, db_session) -> None:
    """合并题材：同义词自动关联（"AI算力"并入"算力"），成员与研报关联随迁。"""
    admin, analyst = make_user(Role.ADMIN), make_user(Role.ANALYST)
    t1 = _mk_target(db_session, "600519", "贵州茅台")
    t2 = _mk_target(db_session, "002635", "安洁科技")

    keep = _mk_theme(db_session, "算力", synonyms=["数据中心"])
    gone = _mk_theme(db_session, "AI算力", synonyms=["智算中心"])
    db_session.add(ThemeMembership(theme_id=gone.id, target_code=t1.code,
                                   source=MembershipSource.SEED, joined_at=dt.date(2024, 1, 1)))
    db_session.add(ThemeMembership(theme_id=keep.id, target_code=t2.code,
                                   source=MembershipSource.SEED, joined_at=dt.date(2024, 2, 1)))
    report = _mk_analyzed_report(db_session, analyst.id, title="算力报告", theme=gone,
                                 raw_names=["AI算力"])
    db_session.commit()

    r = api.patch(
        f"/api/themes/{gone.id}", json={"action": "merge", "merge_into_id": keep.id},
        cookies=login(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "merged" and body["merged_into_id"] == keep.id

    db_session.expire_all()
    dst = db_session.get(Theme, keep.id)
    assert set(dst.synonyms) == {"数据中心", "AI算力", "智算中心"}  # 原名+同义词并入
    assert set(expand_query_terms(dst)) == {"算力", "数据中心", "AI算力", "智算中心"}
    member_codes = {
        m.target_code
        for m in db_session.scalars(select(ThemeMembership).where(ThemeMembership.theme_id == keep.id))
    }
    assert member_codes == {t1.code, t2.code}  # 成员随迁
    # 研报关联随迁：keep 下能查到这篇研报
    reports = api.get(f"/api/themes/{keep.id}/reports", cookies=login(analyst)).json()
    assert [i["id"] for i in reports["items"]] == [report.id]


def test_merge_rejects_bad_destination(api, login, make_user, db_session) -> None:
    admin = make_user(Role.ADMIN)
    keep = _mk_theme(db_session, "算力")
    gone = _mk_theme(db_session, "AI算力", ThemeStatus.PENDING)
    db_session.commit()

    assert api.patch(  # 缺 merge_into_id
        f"/api/themes/{gone.id}", json={"action": "merge"}, cookies=login(admin)
    ).status_code == 422
    assert api.patch(  # 合并到自身
        f"/api/themes/{keep.id}",
        json={"action": "merge", "merge_into_id": keep.id}, cookies=login(admin),
    ).status_code == 409
    assert api.patch(  # 去向必须 active（pending 不可作去向）
        f"/api/themes/{keep.id}",
        json={"action": "merge", "merge_into_id": gone.id}, cookies=login(admin),
    ).status_code == 409


# ---------- 分析关联回写（mock LLM 端到端） ----------


_MD = "证券研究报告\n2024 年 12月 31日\n" + "AI 算力产业链需求高增，安洁科技（002635）受益。" * 100


def _raw(themes: list[dict], authors: list[dict]) -> str:
    return json.dumps({
        "broker": "东吴证券", "authors": authors, "publish_date": None,
        "report_type": "深度", "title": "算力深度", "summary": "s",
        "themes": themes,
        "targets": [{"code": "002635", "name": "安洁科技", "stance": "推荐",
                     "view": "受益算力需求", "has_forecast": True}],
        "rating": {"action": "买入", "maintained": True}, "risk_notes": "",
    }, ensure_ascii=False)


def test_analysis_links_known_theme_and_queues_unknown(make_report, llm_env) -> None:
    """在册题材（含同义词）直连；未知题材进待审；重跑不重复堆待审。

    已落成的标的同步回填题材标的池（ThemeMembership source=analysis，spec 三来源）。
    """
    from app import analysis
    from app.db import session_scope

    with session_scope() as s:
        _mk_target(s, "002635", "安洁科技")  # 瀑布贴表通过 → 会员回填有料
        keep = _mk_theme(s, "算力", synonyms=["AI算力"])
        s.commit()
    report_id, _ = make_report(_MD)
    payload = _raw(
        [{"name": "AI算力", "reason": "全文主线"}, {"name": "液冷服务器", "reason": "章节"}],
        [{"name": "张三", "cert": "S1050521010001"}],
    )
    llm_env(make_llm([payload, payload]))  # 两版重跑

    for _ in range(2):  # v1 与 v2（reanalyze 语义）
        with session_scope() as s:
            report = s.get(ResearchReport, report_id)
            file = analysis.latest_converted_file(s, report_id)
            analysis.run_analysis(s, report, file)
            s.commit()

    with session_scope() as s:
        pending = s.scalars(select(Theme).where(Theme.status == ThemeStatus.PENDING)).all()
        assert [t.name for t in pending] == ["液冷服务器"]  # 两版重跑只堆一条待审
        assert pending[0].source == ThemeSource.ANALYSIS

        links = s.scalars(
            select(ReportTheme).order_by(ReportTheme.analysis_id, ReportTheme.seq)
        ).all()
        assert len(links) == 4  # 2 版 × 2 题材
        assert {l.theme_id for l in links} == {keep.id, pending[0].id}
        assert all(l.theme_id is not None for l in links)

        # 标的池回填：两个题材 × 002635，source=analysis，重跑不重复
        memberships = s.scalars(select(ThemeMembership)).all()
        assert {(m.theme_id, m.target_code, m.source, m.is_active) for m in memberships} == {
            (keep.id, "002635", MembershipSource.ANALYSIS, True),
            (pending[0].id, "002635", MembershipSource.ANALYSIS, True),
        }

        authors = s.scalars(select(AnalysisAuthor)).all()
        assert [(a.name, a.cert) for a in authors] == [("张三", "S1050521010001")] * 2


def test_analysis_links_via_merged_synonym(make_report) -> None:
    """合并的治理红利：被并入的原名"AI算力"继续命中去向"算力"（含全角变体）。"""
    from app import analysis
    from app.db import session_scope

    with session_scope() as s:
        _mk_theme(s, "算力", synonyms=["AI算力"])
        s.commit()
    report_id, _ = make_report(_MD)
    with session_scope() as s:
        report = s.get(ResearchReport, report_id)
        file = analysis.latest_converted_file(s, report_id)
        analysis.run_analysis(s, report, file, llm=make_llm([_raw(
            [{"name": "ＡＩ算力", "reason": "r"}], [{"name": "李四"}],
        )]))
        s.commit()
    with session_scope() as s:
        assert s.scalars(select(Theme).where(Theme.status == ThemeStatus.PENDING)).first() is None
        link = s.scalar(select(ReportTheme))
        keep = s.scalars(select(Theme).where(Theme.status == ThemeStatus.ACTIVE)).one()
        assert link.theme_id == keep.id


# ---------- 治理回环：停用不是终审（评审修复的回归） ----------


def test_retired_theme_name_requeues_instead_of_unique_violation(make_report) -> None:
    """停用题材仍占 name_norm 唯一索引：同名再提取必须复活条目而非撞库炸分析。"""
    from app import analysis
    from app.db import session_scope

    with session_scope() as s:
        dead = _mk_theme(s, "元宇宙", ThemeStatus.RETIRED)
        s.commit()
        dead_id = dead.id
    report_id, _ = make_report(_MD)
    with session_scope() as s:
        report = s.get(ResearchReport, report_id)
        file = analysis.latest_converted_file(s, report_id)
        analysis.run_analysis(s, report, file, llm=make_llm([_raw(
            [{"name": "元宇宙", "reason": "r"}], [{"name": "x"}],
        )]))
        s.commit()
    with session_scope() as s:
        theme = s.get(Theme, dead_id)
        assert theme.status == ThemeStatus.PENDING  # 复活重新进待审
        assert s.scalar(select(ReportTheme)).theme_id == dead_id


def test_merged_then_retired_chain_resurrects_root(make_report, make_user) -> None:
    """A 并入 B、B 又被停用：再提 A 的名字 → 复活链尾 B（A 的语义归宿）。"""
    from app import analysis
    from app.db import session_scope
    from app.themes import merge_theme, retire_theme

    admin_id = make_user(Role.ADMIN).id
    with session_scope() as s:
        a = _mk_theme(s, "AI算力")
        b = _mk_theme(s, "算力")
        merge_theme(s, a, b, admin_id)
        retire_theme(s, b, admin_id)
        s.commit()
        b_id = b.id
    report_id, _ = make_report(_MD)
    with session_scope() as s:
        report = s.get(ResearchReport, report_id)
        file = analysis.latest_converted_file(s, report_id)
        analysis.run_analysis(s, report, file, llm=make_llm([_raw(
            [{"name": "AI算力", "reason": "r"}], [{"name": "x"}],
        )]))
        s.commit()
    with session_scope() as s:
        assert s.get(Theme, b_id).status == ThemeStatus.PENDING


def test_propose_retired_name_via_api_requeues(api, login, make_user, db_session) -> None:
    analyst = make_user(Role.ANALYST)
    theme = _mk_theme(db_session, "元宇宙", ThemeStatus.RETIRED)
    db_session.commit()

    r = api.post("/api/themes", json={"name": "元宇宙"}, cookies=login(analyst))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["id"] == theme.id  # 复用原条目，不另立


def test_merged_seed_theme_not_resurrected_by_reimport(db_session, make_user) -> None:
    """已合并的种子题材：重导入不复活、不挂回成员（治理裁决优先于自动导入）。"""
    from app.themes import merge_theme

    admin_id = make_user(Role.ADMIN).id
    t1 = _mk_target(db_session, "600519", "贵州茅台")
    keeper = _mk_theme(db_session, "算力")
    seed_theme = _mk_theme(db_session, "AI算力", status=ThemeStatus.ACTIVE,
                           source=ThemeSource.SEED_EM, seed_code="BK0800")
    db_session.add(ThemeMembership(theme_id=seed_theme.id, target_code=t1.code,
                                   source=MembershipSource.SEED, joined_at=dt.date(2026, 1, 1)))
    db_session.flush()
    merge_theme(db_session, seed_theme, keeper, admin_id)  # 成员随迁到 keeper
    db_session.commit()

    import_theme_seeds(db_session, [_seed(ThemeSource.SEED_EM, "BK0800", "AI算力", [t1.code])])
    db_session.refresh(seed_theme)
    assert seed_theme.status == ThemeStatus.MERGED
    assert db_session.scalars(
        select(ThemeMembership).where(ThemeMembership.theme_id == seed_theme.id)
    ).all() == []
    assert len(db_session.scalars(
        select(ThemeMembership).where(ThemeMembership.theme_id == keeper.id)
    ).all()) == 1





# ---------- 种子导入（纯导入幂等；测试注入行数据） ----------


def _seed(theme_source: str, code: str, name: str, members: list[str]) -> ConceptSeed:
    return ConceptSeed(source=theme_source, seed_code=code, name=name, member_codes=members)


def test_seed_import_idempotent_with_membership_sync(db_session) -> None:
    t1 = _mk_target(db_session, "600519", "贵州茅台")
    t2 = _mk_target(db_session, "002635", "安洁科技")
    t3 = _mk_target(db_session, "000001", "平安银行")
    day1 = dt.date(2026, 9, 1)

    stats1 = import_theme_seeds(db_session, [
        _seed(ThemeSource.SEED_EM, "BK0800", "AI算力", [t1.code, t2.code, "999999"]),  # 脏码跳过
        _seed(ThemeSource.SEED_SW, "1101", "种植业", [t3.code]),
    ], today=day1)
    assert stats1["themes_created"] == 2

    em = db_session.scalar(select(Theme).where(Theme.seed_code == "BK0800"))
    assert em.status == ThemeStatus.ACTIVE and em.source == ThemeSource.SEED_EM

    def _members(theme_id: int) -> dict[str, ThemeMembership]:
        return {
            m.target_code: m
            for m in db_session.scalars(
                select(ThemeMembership).where(ThemeMembership.theme_id == theme_id)
            )
        }

    m1 = _members(em.id)
    assert set(m1) == {t1.code, t2.code}
    assert m1[t1.code].joined_at == day1 and m1[t1.code].is_active

    # 人工加的成员：种子同步不许动
    db_session.add(ThemeMembership(theme_id=em.id, target_code=t3.code,
                                   source=MembershipSource.MANUAL, joined_at=dt.date(2025, 1, 1)))
    db_session.flush()

    # 重导：成分变化（t2 退出）；题材不重复建
    stats2 = import_theme_seeds(db_session, [
        _seed(ThemeSource.SEED_EM, "BK0800", "AI算力", [t1.code]),
        _seed(ThemeSource.SEED_SW, "1101", "种植业", [t3.code]),
    ], today=dt.date(2026, 9, 28))
    assert stats2["themes_created"] == 0

    m2 = _members(em.id)
    assert m2[t2.code].is_active is False  # 退池
    assert m2[t1.code].is_active and m2[t1.code].joined_at == day1  # 加入日期保留首见
    assert m2[t3.code].source == MembershipSource.MANUAL and m2[t3.code].is_active

    # 回池：成分又回来了 → is_active 复位，joined_at 仍首见
    import_theme_seeds(
        db_session,
        [_seed(ThemeSource.SEED_EM, "BK0800", "AI算力", [t1.code, t2.code])],
        today=dt.date(2026, 10, 1),
    )
    m3 = _members(em.id)
    assert m3[t2.code].is_active and m3[t2.code].joined_at == day1


def test_seed_skips_name_collision_and_follows_rename(db_session) -> None:
    """种子撞人工题材名 → 跳过；板块更名且新名空闲 → 跟随改名。"""
    _mk_target(db_session, "600519", "贵州茅台")
    _mk_theme(db_session, "机器人", ThemeStatus.PENDING)  # 人工提议占名

    import_theme_seeds(db_session, [_seed(ThemeSource.SEED_EM, "BK0655", "机器人", ["600519"])])
    assert len(db_session.scalars(select(Theme)).all()) == 1  # 种子被跳过

    keep = _mk_theme(db_session, "旧名", status=ThemeStatus.ACTIVE,
                     source=ThemeSource.SEED_EM, seed_code="BK0001")
    import_theme_seeds(db_session, [_seed(ThemeSource.SEED_EM, "BK0001", "新名", [])])
    db_session.refresh(keep)
    assert keep.name == "新名" and keep.name_norm == "新名"

    _mk_theme(db_session, "占位")
    import_theme_seeds(db_session, [_seed(ThemeSource.SEED_EM, "BK0001", "占位", [])])
    db_session.refresh(keep)
    assert keep.name == "新名"  # 新名被占用 → 保留旧名


# ---------- 浏览 API：列表/研报/成员/研报列表题材过滤 ----------


def test_theme_list_counts_filter_and_search(api, login, make_user, db_session) -> None:
    reader, analyst = make_user(Role.READER), make_user(Role.ANALYST)
    _mk_target(db_session, "600519", "贵州茅台")
    hot = _mk_theme(db_session, "算力", synonyms=["AI算力"])
    _mk_theme(db_session, "种植业")
    _mk_theme(db_session, "待审词", ThemeStatus.PENDING)
    _mk_analyzed_report(db_session, analyst.id, title="算力报告", theme=hot,
                        raw_names=["算力", "AI算力"])  # 同题材多词
    db_session.add(ThemeMembership(theme_id=hot.id, target_code="600519",
                                   source=MembershipSource.SEED, joined_at=dt.date(2026, 1, 1)))
    db_session.commit()

    body = api.get("/api/themes", cookies=login(reader)).json()
    assert body["total"] == 2  # 默认只看在册
    assert [t["name"] for t in body["items"]] == ["算力", "种植业"]  # 研报数降序
    assert body["items"][0]["report_count"] == 1 and body["items"][0]["member_count"] == 1
    assert body["items"][1]["report_count"] == 0 and body["items"][1]["member_count"] == 0

    body = api.get("/api/themes", params={"status": "pending"}, cookies=login(reader)).json()
    assert [t["name"] for t in body["items"]] == ["待审词"]

    body = api.get("/api/themes", params={"status": "all", "q": "算力"}, cookies=login(reader)).json()
    assert [t["name"] for t in body["items"]] == ["算力"]  # q 命中同义词
    assert api.get("/api/themes", params={"status": "bogus"}, cookies=login(reader)).status_code == 422


def test_theme_reports_current_version_only_distinct_and_softdeleted(api, login, make_user, db_session) -> None:
    reader, analyst = make_user(Role.READER), make_user(Role.ANALYST)
    theme = _mk_theme(db_session, "算力")

    # 命中：当前版关联（同题材两词 → 去重后 1 篇）
    hit = _mk_analyzed_report(db_session, analyst.id, title="命中", theme=theme,
                              raw_names=["算力", "AI算力"])
    # 不命中：关联只在旧版本上，当前版（v2）无关联
    stale = _mk_analyzed_report(db_session, analyst.id, title="过期", theme=theme, raw_names=["算力"])
    old_analysis_id = stale.current_analysis_id
    f2 = ReportFile(report_id=stale.id, storage_key="k2", filename="b.pdf",
                    content_type="application/pdf", size_bytes=1, file_sha256="0" * 64,
                    uploaded_by=analyst.id, markdown_text="x",
                    converted_at=dt.datetime.now(dt.timezone.utc))
    db_session.add(f2)
    db_session.flush()
    v2 = Analysis(report_id=stale.id, report_file_id=f2.id, version=2, prompt_version="v1",
                  model="m", prompt_tokens=1, completion_tokens=1, duration_ms=1, result={})
    db_session.add(v2)
    db_session.flush()
    stale.current_analysis_id = v2.id
    assert old_analysis_id != v2.id
    # 不命中：软删除（当前版有关联）
    deleted = _mk_analyzed_report(db_session, analyst.id, title="已删", theme=theme, raw_names=["算力"])
    deleted.deleted_at = dt.datetime.now(dt.timezone.utc)
    db_session.commit()

    body = api.get(f"/api/themes/{theme.id}/reports", cookies=login(reader)).json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [hit.id]

    # 研报列表题材过滤同语义
    body = api.get("/api/reports", params={"theme_id": theme.id}, cookies=login(reader)).json()
    assert {i["id"] for i in body["items"]} == {hit.id}
    assert api.get("/api/themes/999999/reports", cookies=login(reader)).status_code == 404


def test_theme_members_endpoint(api, login, make_user, db_session) -> None:
    reader = make_user(Role.READER)
    t1 = _mk_target(db_session, "600519", "贵州茅台")
    t2 = _mk_target(db_session, "002635", "安洁科技")
    theme = _mk_theme(db_session, "白酒")
    db_session.add(ThemeMembership(theme_id=theme.id, target_code=t1.code,
                                   source=MembershipSource.SEED, joined_at=dt.date(2026, 1, 1)))
    db_session.add(ThemeMembership(theme_id=theme.id, target_code=t2.code,
                                   source=MembershipSource.ANALYSIS,
                                   joined_at=dt.date(2026, 2, 1), is_active=False))
    db_session.commit()

    body = api.get(f"/api/themes/{theme.id}/members", cookies=login(reader)).json()
    assert [(m["code"], m["is_active"]) for m in body["items"]] == [
        ("600519", True), ("002635", False)  # 活跃在前
    ]
    assert body["items"][0]["name"] == "贵州茅台"
    assert body["items"][1]["source"] == "analysis"

    body = api.get(f"/api/themes/{theme.id}/members",
                   params={"active_only": True}, cookies=login(reader)).json()
    assert [m["code"] for m in body["items"]] == ["600519"]


# ---------- 覆盖查询（观点迁移追踪） ----------


_MD2 = "证券研究报告\n2024 年 12月 31日\n" + "白酒板块复苏，贵州茅台（600519）龙头地位稳固。" * 100


def _raw_coverage(authors: list[dict]) -> str:
    return json.dumps({
        "broker": "东吴证券", "authors": authors, "publish_date": None,
        "report_type": "点评", "title": "t", "summary": "s",
        "themes": [{"name": "白酒", "reason": "r"}],
        "targets": [{"code": "600519", "name": "贵州茅台", "stance": "推荐", "view": "v",
                     "has_forecast": True}],
        "rating": None, "risk_notes": "",
    }, ensure_ascii=False)


def test_author_search_and_coverage(api, login, make_user, llm_env, make_report) -> None:
    reader = make_user(Role.READER)
    from app import analysis
    from app.db import session_scope

    with session_scope() as s:
        _mk_target(s, "600519", "贵州茅台")
        theme = _mk_theme(s, "白酒")
        s.commit()
    r1, _ = make_report(_MD2, title="覆盖一", broker="东吴证券")
    r2, _ = make_report(_MD2, title="覆盖二", broker="中信证券")
    llm_env(make_llm([
        _raw_coverage([{"name": "王明星", "cert": "S001"}]),
        _raw_coverage([{"name": "王明星", "cert": "S001"}, {"name": "同事甲"}]),
    ]))
    for rid in (r1, r2):
        with session_scope() as s:
            report = s.get(ResearchReport, rid)
            file = analysis.latest_converted_file(s, rid)
            analysis.run_analysis(s, report, file)
            s.commit()

    body = api.get("/api/authors", params={"q": "明星"}, cookies=login(reader)).json()
    # 署名搜索按 (name, cert, broker) 分列：同名跨券商可辨
    assert [(i["name"], i["cert"], i["broker"], i["report_count"]) for i in body["items"]] == [
        ("王明星", "S001", "东吴证券", 1),
        ("王明星", "S001", "中信证券", 1),
    ]

    body = api.get("/api/authors/coverage", params={"name": "王明星"},
                   cookies=login(reader)).json()
    assert body["reports_total"] == 2
    assert [(t["theme_id"], t["name"], t["status"]) for t in body["themes"]] == [
        (theme.id, "白酒", "active")
    ]
    assert [(t["code"], len(t["report_ids"])) for t in body["targets"]] == [("600519", 2)]

    # 券商过滤：同名分析师跨券商可分
    body = api.get("/api/authors/coverage",
                   params={"name": "王明星", "broker": "中信证券"}, cookies=login(reader)).json()
    assert body["reports_total"] == 1

    body = api.get("/api/authors/coverage", params={"name": "同事甲"},
                   cookies=login(reader)).json()
    assert body["reports_total"] == 1


def test_author_search_includes_reports_without_theme_links(api, login, make_user, llm_env, make_report) -> None:
    """零题材研报的作者不消失（笛卡尔积缺陷回归）。

    署名搜索的"当前版"谓词曾误用 ReportTheme 专用常量配 AnalysisAuthor 查询，
    SQLAlchemy 隐式 FROM 把 report_themes 笛卡尔积进查询——当前分析没提取出
    题材的研报（行业/宏观报告常见），其作者从搜索与覆盖查询中整个消失。
    """
    reader = make_user(Role.READER)
    from app import analysis
    from app.db import session_scope

    raw_no_theme = json.dumps({
        "broker": "东吴证券", "authors": [{"name": "冷门侠"}], "publish_date": None,
        "report_type": "策略", "title": "t", "summary": "s",
        "themes": [], "targets": [],
        "rating": None, "risk_notes": "",
    }, ensure_ascii=False)

    rid, _ = make_report(_MD2, title="无题材覆盖", broker="东吴证券")
    llm_env(make_llm([raw_no_theme]))
    with session_scope() as s:
        analysis.run_analysis(s, s.get(ResearchReport, rid), analysis.latest_converted_file(s, rid))
        s.commit()

    body = api.get("/api/authors", params={"q": "冷门"}, cookies=login(reader)).json()
    assert [(i["name"], i["report_count"]) for i in body["items"]] == [("冷门侠", 1)]

    body = api.get("/api/authors/coverage", params={"name": "冷门侠"},
                   cookies=login(reader)).json()
    assert body["reports_total"] == 1
    assert body["themes"] == [] and body["targets"] == []


# ---------- worker：import_themes 任务 ----------


def test_worker_import_themes_task(api, login, make_user, db_session, monkeypatch) -> None:
    admin = make_user(Role.ADMIN)
    t = _mk_target(db_session, "600519", "贵州茅台")
    db_session.commit()

    r = api.post("/api/themes/import", cookies=login(admin))
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert api.post("/api/themes/import", cookies=login(make_user(Role.ANALYST))).status_code == 403

    from app import themes as themes_mod
    from app import worker

    monkeypatch.setattr(
        themes_mod, "fetch_em_concept_seeds",
        lambda sleep=0.35: [_seed(ThemeSource.SEED_EM, "BK0800", "AI算力", [t.code])],
    )
    monkeypatch.setattr(
        themes_mod, "fetch_sw_l2_seeds",
        lambda session: [_seed(ThemeSource.SEED_SW, "1101", "种植业", [t.code])],
    )
    claimed = worker.run_once()
    assert claimed is not None and claimed.id == task_id
    body = api.get(f"/api/tasks/{task_id}", cookies=login(admin)).json()
    assert body["status"] == "done", body
    assert body["result"]["themes_created"] == 2

    db_session.expire_all()
    names = {row.name for row in db_session.scalars(select(Theme))}
    assert names == {"AI算力", "种植业"}
