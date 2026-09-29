"""题材受控词表（#17）：状态机治理、同义词合并、词表关联回写与冷启动种子。

状态机（spec）：待审 pending → 在册 active → 合并 merged / 停用 retired。
- LLM 分析提议的新题材落 pending（同词表 name_norm 去重，不碎片化）；管理员审核后在册。
- 停用不是终审：同名提议再现时条目复活重新进待审（_settle_theme），避免唯一索引
  撞库导致分析任务失败。
- 合并：源题材原名与同义词并入去向同义词组，成员与研报关联随迁——"AI算力"与
  "算力"不各立门户。
- 分析回填标的池：题材关联落库时，本分析瀑布已落成的标的作为
  ThemeMembership(source=analysis) 入池（spec 数据模型的三来源之一）。
- 种子：东财概念（内置快照，滤行情类噪音）+ 申万二级骨架一键导入（幂等），成分股
  直接作为 ThemeMembership(source=seed) 入库；重导入时移出的成分退池（is_active=False），
  分析/人工来源的成员不受种子同步影响；已合并/停用的种子不复活。

东财概念导入只读仓库内置快照（app/data/em_concept_snapshot.json，零网络——push2
对高频请求按 IP 断连，部署机在线抓取不可靠）；联网抓取降级为本地刷新脚本
scripts/refresh_em_concept_snapshot.py，产出提交进仓库随版本发布。纯导入函数
（import_theme_seeds）与网络函数分离：测试只喂行数据，不碰网络。
"""

from __future__ import annotations

import datetime as dt
import json
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .errors import MasterDataError
from .models import (
    Analysis,
    AnalysisTarget,
    ReportTheme,
    ResearchReport,
    Target,
    Theme,
    ThemeMembership,
)
from .targets import valid_code


class ThemeStatus:
    PENDING = "pending"
    ACTIVE = "active"
    MERGED = "merged"
    RETIRED = "retired"
    ALL = (PENDING, ACTIVE, MERGED, RETIRED)


class ThemeSource:
    ANALYSIS = "analysis"
    MANUAL = "manual"
    SEED_EM = "seed_em"
    SEED_SW = "seed_sw"


class MembershipSource:
    SEED = "seed"
    ANALYSIS = "analysis"
    MANUAL = "manual"


def normalize_theme_name(name: str) -> str:
    """NFKC + 去全部空白：全角/半角与空格差异不打散词表（"AI 算力"≡"AI算力"）。

    与展示名分开：name 保留原文，name_norm 只作身份键。
    """
    return "".join(unicodedata.normalize("NFKC", str(name or "")).split())


# ---------- 词表内存索引与关联回写 ----------


class ThemeVocab:
    """匹配词典：在册题材（名 + 同义词）→ 条目；待审题材（名）→ 条目。

    在册同义词参与匹配（合并的治理红利：被并入的原名继续命中去向）；
    待审只按名去重，避免同一 LM 提议词堆出多行。
    """

    def __init__(self, rows: list[Theme]) -> None:
        self.rows: dict[int, Theme] = {r.id: r for r in rows}
        self._active: dict[str, int] = {}
        self._pending: dict[str, int] = {}
        for r in sorted(rows, key=lambda x: x.id):
            if r.status == ThemeStatus.ACTIVE:
                keys = [normalize_theme_name(r.name)]
                keys.extend(normalize_theme_name(s) for s in (r.synonyms or []))
                for key in {k for k in keys if k}:
                    self._active.setdefault(key, r.id)
            elif r.status == ThemeStatus.PENDING:
                key = normalize_theme_name(r.name)
                if key:
                    self._pending.setdefault(key, r.id)

    @classmethod
    def load(cls, session: OrmSession) -> "ThemeVocab":
        return cls(list(session.scalars(select(Theme))))

    def register_pending(self, theme: Theme) -> None:
        self.rows[theme.id] = theme
        self._pending.setdefault(normalize_theme_name(theme.name), theme.id)

    def resolve(self, raw_name: str) -> Theme | None:
        key = normalize_theme_name(raw_name)
        if not key:
            return None
        theme_id = self._active.get(key) or self._pending.get(key)
        return self.rows.get(theme_id) if theme_id is not None else None

    def follow_merged(self, theme: Theme) -> Theme:
        """沿 merged_into_id 走到链尾（合并去向被再次合并时递归跟随；seen 防环兜底）。"""
        seen: set[int] = set()
        while theme.merged_into_id is not None and theme.id not in seen and theme.merged_into_id in self.rows:
            seen.add(theme.id)
            theme = self.rows[theme.merged_into_id]
        return theme


def _settle_theme(session: OrmSession, vocab: ThemeVocab, raw: str, *, source: str) -> Theme:
    """resolve 未命中时的落地：新建待审，或复活占用同 name_norm 的死条目。

    停用/合并后的行仍占 name_norm 唯一索引，直接插入会撞库（分析任务整体失败）。
    治理回环语义：同名提议再现 → 该条目重新进待审交管理员复审（停用不是终审）；
    合并链尾已死 → 复活链尾（被并入的原名语义上属于它）。
    """
    key = normalize_theme_name(raw)
    existing = session.scalar(select(Theme).where(Theme.name_norm == key))
    if existing is None:
        theme = Theme(name=raw, name_norm=key, status=ThemeStatus.PENDING, source=source)
        session.add(theme)
        session.flush()
        vocab.register_pending(theme)
        return theme

    if existing.status == ThemeStatus.MERGED:
        root = vocab.follow_merged(existing)
        if root.status in (ThemeStatus.ACTIVE, ThemeStatus.PENDING):
            return root
        existing = root  # 链尾也已死：复活链尾

    assert existing.status == ThemeStatus.RETIRED
    existing.status = ThemeStatus.PENDING
    existing.reviewed_by = None
    existing.reviewed_at = None
    vocab.register_pending(existing)
    return existing


def link_analysis_themes(
    session: OrmSession, report: ResearchReport, analysis: Analysis
) -> list[ReportTheme]:
    """把一版分析的 result.themes 关联到词表：在册（名/同义词）直连，未知进待审。

    在 run_analysis 落库后同事务调用（不 commit）。result JSON 仍是提取时审计
    快照，不动；本表是可查询投影（与 analysis_targets 同构）。
    同时把本分析已落成的标的回填题材标的池（ThemeMembership source=analysis，
    spec 数据模型）：已入池的仅确保活跃，不重复建行。
    """
    result: dict = analysis.result or {}
    vocab = ThemeVocab.load(session)

    links: list[ReportTheme] = []
    linked_themes: dict[int, Theme] = {}
    seq = 0  # 跳过空名后紧凑编号（唯一约束 analysis_id+seq）
    for t in result.get("themes") or []:
        if not isinstance(t, dict):
            continue
        raw = str(t.get("name") or "").strip()
        if not raw:
            continue
        theme = vocab.resolve(raw)
        if theme is None:
            theme = _settle_theme(session, vocab, raw, source=ThemeSource.ANALYSIS)
        linked_themes[theme.id] = theme
        link = ReportTheme(
            analysis_id=analysis.id,
            report_id=report.id,
            seq=seq,
            theme_id=theme.id,
            raw_name=raw,
            reason=str(t.get("reason") or ""),
        )
        session.add(link)
        links.append(link)
        seq += 1

    # 标的池回填（瀑布已落成的 code 才入池——人工确认后同样经此路径补齐）
    if linked_themes:
        codes = list(
            session.scalars(
                select(AnalysisTarget.target_code).where(
                    AnalysisTarget.analysis_id == analysis.id,
                    AnalysisTarget.target_code.is_not(None),
                )
            )
        )
        if codes:
            existing = {
                (m.theme_id, m.target_code): m
                for m in session.scalars(
                    select(ThemeMembership).where(
                        ThemeMembership.theme_id.in_(linked_themes.keys()),
                        ThemeMembership.target_code.in_(codes),
                    )
                )
            }
            today = dt.datetime.now(dt.timezone.utc).date()
            for theme_id in linked_themes:
                for code in codes:
                    row = existing.get((theme_id, code))
                    if row is None:
                        row = ThemeMembership(
                            theme_id=theme_id, target_code=code,
                            source=MembershipSource.ANALYSIS,
                            joined_at=today, is_active=True,
                        )
                        session.add(row)
                    else:
                        row.is_active = True

    session.flush()
    return links


def expand_query_terms(theme: Theme) -> list[str]:
    """订阅查询词展开（spec：题材名+同义词，#19 订阅票直接消费）。"""
    seen: dict[str, str] = {}
    for term in [theme.name, *(theme.synonyms or [])]:
        term = str(term or "").strip()
        if term:
            seen.setdefault(normalize_theme_name(term), term)
    return list(seen.values())


# ---------- 治理动作（审核 / 合并 / 停用；由 admin API 调用） ----------


def propose_theme(
    session: OrmSession,
    *,
    name: str,
    definition: str = "",
    synonyms: list[str] | None = None,
    proposed_by: int,
) -> tuple[Theme | None, str | None]:
    """人工提议：落 pending。返回 (theme, None) 或 (None, 冲突原因)——
    与在册题材的名/同义词重合、或与待审题材重名都不另立条目；
    与停用/已合并条目同名则复活该条目重新进待审（治理回环，见 _settle_theme）。"""
    name = name.strip()
    key = normalize_theme_name(name)
    if not key:
        return None, "empty_name"
    vocab = ThemeVocab.load(session)
    hit = vocab.resolve(name)
    if hit is not None:
        return None, (
            f"与{'在册' if hit.status == ThemeStatus.ACTIVE else '待审'}题材「{hit.name}」重名"
        )
    theme = _settle_theme(session, vocab, name, source=ThemeSource.MANUAL)
    theme.definition = definition
    theme.synonyms = [s.strip() for s in synonyms or [] if s and s.strip()]
    theme.proposed_by = proposed_by
    return theme, None


def approve_theme(
    session: OrmSession,
    theme: Theme,
    admin_id: int,
    *,
    definition: str | None = None,
    synonyms: list[str] | None = None,
) -> None:
    """审核在册：pending → active；可同时补定义与同义词组。"""
    if theme.status != ThemeStatus.PENDING:
        raise ValueError(f"题材「{theme.name}」当前状态 {theme.status}，仅待审可审核在册")
    theme.status = ThemeStatus.ACTIVE
    theme.reviewed_by = admin_id
    theme.reviewed_at = dt.datetime.now(dt.timezone.utc)
    if definition is not None:
        theme.definition = definition
    if synonyms is not None:
        theme.synonyms = _merge_synonyms(theme, synonyms)


def retire_theme(session: OrmSession, theme: Theme, admin_id: int) -> None:
    """停用：pending/active → retired（词表出列，历史关联保留）。"""
    if theme.status not in (ThemeStatus.PENDING, ThemeStatus.ACTIVE):
        raise ValueError(f"题材「{theme.name}」当前状态 {theme.status}，不可停用")
    theme.status = ThemeStatus.RETIRED
    theme.reviewed_by = admin_id
    theme.reviewed_at = dt.datetime.now(dt.timezone.utc)


def _merge_synonyms(theme: Theme, extra: list[str]) -> list[str]:
    """同义词组并集（归一去重、剔除与正名重合者），保序。"""
    out: list[str] = []
    seen = {normalize_theme_name(theme.name)}
    for s in [*(theme.synonyms or []), *extra]:
        s = str(s or "").strip()
        key = normalize_theme_name(s)
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def merge_theme(session: OrmSession, src: Theme, dst: Theme, admin_id: int) -> None:
    """合并：src 的同义词组与原名并入 dst 同义词组，成员与研报关联随迁。

    src 置 merged 并记去向；dst 必须在册（合并是治理动作，去向须是可用词表条目）。
    """
    if dst.id == src.id:
        raise ValueError("不能合并到自身")
    if dst.status != ThemeStatus.ACTIVE:
        raise ValueError(f"合并去向「{dst.name}」必须在册（当前 {dst.status}）")
    if src.status not in (ThemeStatus.PENDING, ThemeStatus.ACTIVE):
        raise ValueError(f"题材「{src.name}」当前状态 {src.status}，不可合并")

    dst.synonyms = _merge_synonyms(dst, [src.name, *(src.synonyms or [])])

    # 成员随迁：去向已有该标的则并（活跃 OR、加入日期取更早），否则整行改挂
    for row in list(
        session.scalars(select(ThemeMembership).where(ThemeMembership.theme_id == src.id))
    ):
        kept = session.scalar(
            select(ThemeMembership).where(
                ThemeMembership.theme_id == dst.id,
                ThemeMembership.target_code == row.target_code,
            )
        )
        if kept is None:
            row.theme_id = dst.id
        else:
            kept.is_active = kept.is_active or row.is_active
            kept.joined_at = min(kept.joined_at, row.joined_at)
            session.delete(row)

    # 研报关联随迁（同 (analysis_id, seq) 唯一，theme_id 改挂无约束冲突）
    for link in session.scalars(select(ReportTheme).where(ReportTheme.theme_id == src.id)):
        link.theme_id = dst.id

    src.status = ThemeStatus.MERGED
    src.merged_into_id = dst.id
    src.reviewed_by = admin_id
    src.reviewed_at = dt.datetime.now(dt.timezone.utc)
    session.flush()


# ---------- 冷启动种子（纯导入幂等；网络抓取见下节） ----------


@dataclass(frozen=True)
class ConceptSeed:
    """一个种子题材及其当前成分股（代码必须在 targets 主数据中）。"""

    source: str  # ThemeSource.SEED_EM | ThemeSource.SEED_SW
    seed_code: str  # 东财 BKxxxx / 申万 l2_code
    name: str
    member_codes: list[str] = field(default_factory=list)


def import_theme_seeds(
    session: OrmSession, seeds: list[ConceptSeed], today: dt.date | None = None
) -> dict:
    """种子骨架幂等导入：题材按 (source, seed_code) upsert，成分按 (theme, code) upsert。

    重导入时成分集变化体现为退池/回池（仅 source=seed 的行；分析与人工成员不动），
    joined_at 保留首见日期。返回计数（落 task.result）。
    """
    today = today or dt.date.today()
    rows: dict[tuple[str, str], Theme] = {
        (t.source, t.seed_code): t
        for t in session.scalars(select(Theme).where(Theme.seed_code.is_not(None)))
    }
    name_norm_taken = {t.name_norm: t for t in session.scalars(select(Theme))}
    known_codes = set(session.scalars(select(Target.code)))  # FK 锚点：成分必须是主数据

    themes_created = 0
    for s in seeds:
        theme = rows.get((s.source, s.seed_code))
        if theme is not None and theme.status != ThemeStatus.ACTIVE:
            # 已合并/停用的种子不复活：治理裁决优先于自动导入（合并时成员已随迁）
            continue
        if theme is None:
            # 与既有题材撞名（如东财概念与人工提议同名）：跳过该种子，人工合并取舍
            holder = name_norm_taken.get(normalize_theme_name(s.name))
            if holder is not None:
                continue
            theme = Theme(
                name=s.name,
                name_norm=normalize_theme_name(s.name),
                status=ThemeStatus.ACTIVE,
                source=s.source,
                seed_code=s.seed_code,
            )
            session.add(theme)
            session.flush()
            rows[(s.source, s.seed_code)] = theme
            name_norm_taken[theme.name_norm] = theme
            themes_created += 1
        elif theme.name != s.name:
            # 种子题材更名（东财板块对象随官方改名）且新名未被占用 → 跟随改名
            new_norm = normalize_theme_name(s.name)
            holder = name_norm_taken.get(new_norm)
            if holder is None or holder is theme:
                del name_norm_taken[theme.name_norm]
                theme.name = s.name
                theme.name_norm = new_norm
                name_norm_taken[new_norm] = theme

        members = {c for c in s.member_codes if valid_code(c) and c in known_codes}
        existing = {
            m.target_code: m
            for m in session.scalars(
                select(ThemeMembership).where(ThemeMembership.theme_id == theme.id)
            )
        }
        for code, m in existing.items():
            if m.source == MembershipSource.SEED:
                m.is_active = code in members  # 成分集同步（退池/回池）
        for code in sorted(members):
            if code not in existing:
                session.add(ThemeMembership(
                    theme_id=theme.id,
                    target_code=code,
                    source=MembershipSource.SEED,
                    joined_at=today,
                    is_active=True,
                ))

    session.flush()
    return {
        "seeds_total": len(seeds),
        "themes_created": themes_created,
        "themes_known": len(rows),
    }


# ---------- 东财概念噪音过滤（行情/风格/资金面类，非题材） ----------

_NOISE_SUBSTRINGS = (
    "昨日", "涨停", "连板", "触板", "重仓", "持股", "股通", "含可转债", "含H股", "含GDR",
    "百元股", "低价股", "高价股", "破净", "微盘", "大盘股", "中盘股", "小盘股", "ST股",
    "次新股", "科创板做市", "融资融券", "转融券", "富时罗素", "MSCI", "标普道琼斯",
    "央视50", "上证50", "上证180", "上证380", "中证500", "中证1000", "中证2000",
    "沪深300", "深证100", "创业板综", "科创板综", "AB股", "AH股", "B股", "H股", "GDR",
)
_NOISE_EXACT = {"低价微盘股", "ST板块", "股权激励", "基金", "期货概念"}


def is_noise_concept(name: str) -> bool:
    """滤行情类噪音（spec 冷启动决策）：涨停/指数成分/风格/资金持仓类非题材。"""
    n = str(name or "").strip()
    return n in _NOISE_EXACT or any(p in n for p in _NOISE_SUBSTRINGS)


# ---------- 东财概念快照（仓库内置；导入零网络，刷新走 scripts/） ----------

EM_SNAPSHOT_JSON = Path(__file__).resolve().parent / "data" / "em_concept_snapshot.json"


def load_em_concept_seeds(path: Path | None = None) -> list[ConceptSeed]:
    """东财概念种子 = 仓库内置快照（scripts/refresh_em_concept_snapshot.py 产出）。

    部署机不直连东财：push2 对高频请求按 IP 断连（实测连 curl 都被掐，冷却分钟级
    以上），在线抓取在数据中心 IP 上不可靠。噪音板块在加载时滤（is_noise_concept
    演化无需重抓快照），成分代码只收 6 位合规码。
    """
    p = path or EM_SNAPSHOT_JSON
    if not p.exists():
        raise MasterDataError(
            "snapshot_missing",
            f"东财概念快照缺失（{p.name}）：backend/ 下跑 scripts/refresh_em_concept_snapshot.py 生成后提交",
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MasterDataError("snapshot_corrupt", f"东财概念快照解析失败：{e}") from e
    seeds: list[ConceptSeed] = []
    for row in raw.get("concepts", []):
        name = str(row.get("name", "")).strip()
        if not name or is_noise_concept(name):
            continue
        members = [str(c) for c in row.get("members", []) if valid_code(str(c))]
        seeds.append(ConceptSeed(
            source=ThemeSource.SEED_EM,
            seed_code=str(row.get("code", "")),
            name=name,
            member_codes=members,
        ))
    return seeds


def merge_em_snapshot(
    existing: dict | None,
    boards: list[tuple[str, str]],
    fetched: list[ConceptSeed],
    failed: list[str],
    today: dt.date | None = None,
) -> dict:
    """刷新脚本用纯函数：本次抓取与既有快照合并（部分失败不丢已有数据）。

    板块集以本次列表为准（官方撤销的板块随之移除）；本次成功的板块用新成分，
    失败的板块沿用既有成分并标 stale（重跑可补齐）；新板块本次失败则缺席。
    """
    by_code = {s.seed_code: s for s in fetched}
    prev = {c.get("code"): c for c in (existing or {}).get("concepts", [])}
    concepts: list[dict] = []
    for name, code in boards:
        s = by_code.get(code)
        if s is not None:
            concepts.append({"code": code, "name": name, "members": s.member_codes})
        elif code in prev:
            concepts.append({**prev[code], "name": name, "stale": True})
    return {
        "generated_at": (today or dt.date.today()).isoformat(),
        "source": "eastmoney push2 clist m:90 t:3（经 akshare，快照说明见 data/README.md）",
        "failed_boards": sorted(failed),
        "concepts": concepts,
    }


# ---------- 网络抓取（仅刷新脚本调用；测试不调用） ----------


def _retry_fetch(desc: str, fn, retries: int = 3, wait: float = 2.0):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise MasterDataError("fetch_failed", f"{desc} 抓取失败（重试 {retries} 次）：{e}") from e
            time.sleep(wait)


def fetch_em_concept_seeds(
    sleep: float = 1.0,
) -> tuple[list[tuple[str, str]], list[ConceptSeed], list[str]]:
    """东财概念板块列表 + 逐板块成分股（akshare）→ (板块全集, 成功种子, 失败板块名)。

    板块全集不过滤（噪音在 load_em_concept_seeds 加载时滤，规则演化不用重抓）；
    单板块失败重试后跳过不拖垮整批（与 merge_em_snapshot 配合，缺口重跑补齐）。
    push2 按请求频率掐连接（按 IP，冷却分钟级以上），sleep 默认 1s（~400 板块
    全程约 10 分钟），仍大面积失败就等冷却后重跑。
    """
    import akshare as ak

    def _boards() -> list[tuple[str, str]]:
        df = ak.stock_board_concept_name_em()
        return [
            (str(name), str(code))
            for name, code in zip(df["板块名称"], df["板块代码"])
            if str(name).strip()
        ]

    boards = _retry_fetch("akshare stock_board_concept_name_em（东财概念列表）", _boards, wait=5.0)
    seeds: list[ConceptSeed] = []
    failed: list[str] = []
    for i, (name, code) in enumerate(boards):
        def _cons(name=name) -> list[str]:
            df = ak.stock_board_concept_cons_em(symbol=name)
            return [str(c) for c in df["代码"] if valid_code(str(c))]

        try:
            members = _retry_fetch(f"东财概念「{name}」成分股", _cons, wait=5.0)
        except MasterDataError:
            failed.append(name)  # 单板块失败不拖垮整批（冷却后重跑补齐）
        else:
            seeds.append(ConceptSeed(source=ThemeSource.SEED_EM, seed_code=code, name=name, member_codes=members))
        if i < len(boards) - 1:
            time.sleep(sleep)
    return boards, seeds, failed


def fetch_sw_l2_seeds(session: OrmSession) -> list[ConceptSeed]:
    """申万二级骨架：静态码表 130 个 l2 + 主数据快照的行业归属作成分（无网络）。"""
    from .masterdata import load_sw2021_names

    l2 = {v[2]: v[3] for v in load_sw2021_names().values()}

    members: dict[str, list[str]] = {}
    for code, sw_l2 in session.execute(
        select(Target.code, Target.sw_l2_code).where(Target.sw_l2_code.is_not(None))
    ):
        members.setdefault(sw_l2, []).append(code)
    return [
        ConceptSeed(source=ThemeSource.SEED_SW, seed_code=code, name=name, member_codes=members.get(code, []))
        for code, name in sorted(l2.items())
    ]
