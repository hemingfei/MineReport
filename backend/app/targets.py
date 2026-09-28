"""标的主数据与规范化瀑布（#16）。

瀑布（spec：主数据贴表 → 名称归一化 → 精确匹配 → 模糊匹配 → 人工确认队列）：
1. LLM 给了 6 位码：
   - code_source=text 且码在主数据 → 直接落成（贴表通过）；
   - code_source=text 但码不在主数据（新股/笔误/退市缺行）→ 队列（code_not_in_master）；
   - code_source=inferred → 无条件队列（inferred_code）。spike 证实 LLM 会凭世界知识
     回填代码——恰好对也不采信，名称瀑布结果只作候选提示（本票核心回归场景）。
2. LLM 没给码：名称瀑布。精确（归一后唯一命中）→ 落成；多名撞车 → 队列；
   模糊 difflib ratio≥0.85 且唯一候选码 → 落成；否则队列（multi_candidate / no_hit）。

自动落成的 code_source=text：代码字面在正文，或由正文名称经上述确定性瀑布命中
（可从文本证据机械复现，与"模型记忆"相区分）；人工确认后为 manually_confirmed。
金融场景宁缺毋滥：错配比缺配代价高，模糊命中必须唯一。

分析回写：link_analysis_targets 把 #15 暂存的 result.targets 原始串落成
analysis_targets 关联行（result JSON 仍是提取时审计快照，不动）。同报告已确认/
已驳回的条目在重跑新版本时自动复用（确认不因批量重跑而丢失）。
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .models import Analysis, AnalysisTarget, ResearchReport, Target, TargetMatch

FUZZY_THRESHOLD = 0.85  # spec 定案：ratio ≥ 0.85 且唯一命中才自动落成


class MatchReason:
    INFERRED_CODE = "inferred_code"
    CODE_NOT_IN_MASTER = "code_not_in_master"
    MULTI_CANDIDATE = "multi_candidate"
    NO_HIT = "no_hit"


class MatchStatus:
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


# ---------- 名称归一化（实测坑清单：ST 前缀 204 只 / 空格 36 只 / 全角字母 21 只 / 公司后缀） ----------

_ST_PREFIX = re.compile(r"^(?:S?\*?ST)+")
_SUFFIXES = ("股份有限公司", "股份公司", "有限责任公司", "有限公司", "集团公司", "集团", "控股", "公司")


def normalize_name(name: str) -> str:
    """NFKC（全角→半角）→ 去全部空白 → 大写拉丁 → 剥 ST/*ST 前缀 → 循环剥公司后缀。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = "".join(s.split()).upper()
    stripped = _ST_PREFIX.sub("", s)
    if not stripped:
        return s  # 整名就是 ST 标记：保留原文，避免归一成空串撞键
    s = stripped
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if len(s) > len(suffix) and s.endswith(suffix):
                s = s[: -len(suffix)]
                changed = True
    return s


# 交易所由代码前缀确定性推导（#4 研究：实测三所全量分布校准）
_EXCHANGE_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("600", "601", "603", "605", "688", "689"), "SH"),
    (("000", "001", "002", "003", "300", "301", "302"), "SZ"),
    (("43", "83", "87", "88", "92"), "BJ"),
)


def exchange_of(code: str) -> str:
    for prefixes, exchange in _EXCHANGE_PREFIXES:
        if code.startswith(prefixes):
            return exchange
    return ""  # 未知段（未来新代码段）：留空，不猜


_CODE_RE = re.compile(r"\d{6}")


def valid_code(code: str | None) -> bool:
    return bool(code) and _CODE_RE.fullmatch(str(code)) is not None


# ---------- 主数据内存索引与瀑布 ----------


@dataclass(frozen=True)
class Candidate:
    code: str
    name: str
    exchange: str
    sw_l1_name: str | None
    score: float


@dataclass
class Resolution:
    """单个标的的瀑布结论：code 非空即自动落成，否则带 reason 入队。"""

    code: str | None = None
    code_source: str | None = None
    reason: str | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def auto(self) -> bool:
        return self.code is not None


class MasterIndex:
    """匹配词典：当前简称 + 曾用名（全部归一后入典），key → 代码集合。

    全量 ~5.5k 行加载进内存一次（SELECT 毫秒级），单篇分析的瀑布查询全走内存。
    """

    def __init__(self, rows: list[Target]) -> None:
        self.rows: dict[str, Target] = {r.code: r for r in rows}
        self.exact: dict[str, set[str]] = {}
        for r in rows:
            keys = [r.name_norm]
            keys.extend(normalize_name(n) for n in (r.historical_names or []))
            for key in {k for k in keys if k}:
                self.exact.setdefault(key, set()).add(r.code)

    @classmethod
    def load(cls, session: OrmSession) -> "MasterIndex":
        return cls(list(session.scalars(select(Target))))

    def _candidate(self, code: str, score: float) -> Candidate:
        row = self.rows[code]
        return Candidate(
            code=code, name=row.name, exchange=row.exchange,
            sw_l1_name=row.sw_l1_name, score=round(score, 3),
        )

    def _by_name(self, name: str) -> Resolution:
        key = normalize_name(name)
        if not key:
            return Resolution(reason=MatchReason.NO_HIT)
        codes = self.exact.get(key)
        if codes:
            if len(codes) == 1:
                code = next(iter(codes))
                return Resolution(code=code, code_source="text")
            return Resolution(
                reason=MatchReason.MULTI_CANDIDATE,
                candidates=[self._candidate(c, 1.0) for c in sorted(codes)],
            )

        # 模糊：每个代码取其全部名称变体中的最高分，≥阈值且唯一代码才落成
        best: dict[str, float] = {}
        for dict_key, code_set in self.exact.items():
            score = SequenceMatcher(None, key, dict_key).ratio()
            for code in code_set:
                if score > best.get(code, 0.0):
                    best[code] = score
        hits = {c: s for c, s in best.items() if s >= FUZZY_THRESHOLD}
        if len(hits) == 1:
            code, score = next(iter(hits.items()))
            return Resolution(code=code, code_source="text")
        if len(hits) > 1:
            return Resolution(
                reason=MatchReason.MULTI_CANDIDATE,
                candidates=[self._candidate(c, s) for c, s in sorted(hits.items())],
            )
        # 未达阈值也带最接近的 3 个候选给人工参考
        near = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:3]
        return Resolution(
            reason=MatchReason.NO_HIT,
            candidates=[self._candidate(c, s) for c, s in near if s > 0.5],
        )

    def resolve(self, *, name: str, code: str | None, code_source: str | None) -> Resolution:
        """瀑布入口。code_source 是 #15 提取时的标记（text|inferred|None）。"""
        if code is not None:
            if code_source == "text" and code in self.rows:
                return Resolution(code=code, code_source="text")  # 贴表通过（text 码）
            name_res = self._by_name(name)
            # inferred（LLM 记忆）或违例输入（schema 约束 code_source=None ⟺ code=None，
            # 有码无标记视为不可信）：一律强制队列，名称瀑布只作候选
            if code_source in ("inferred", None):
                if name_res.auto:
                    candidates = [self._candidate(name_res.code, 1.0)]
                else:
                    candidates = name_res.candidates
                if code in self.rows and all(c.code != code for c in candidates):
                    candidates = [self._candidate(code, 1.0), *candidates]
                return Resolution(reason=MatchReason.INFERRED_CODE, candidates=candidates[:5])
            return Resolution(reason=MatchReason.CODE_NOT_IN_MASTER, candidates=name_res.candidates)
        return self._by_name(name)


# ---------- 分析回写（result.targets → analysis_targets + 队列） ----------


def _prior_decisions(session: OrmSession, report_id: int) -> dict[tuple[str, str | None], TargetMatch]:
    """同报告已终审（confirmed/dismissed）的队列条目，按（归一名, 原码）索引，供新版本复用。"""
    rows = session.scalars(
        select(TargetMatch).where(
            TargetMatch.report_id == report_id,
            TargetMatch.status.in_((MatchStatus.CONFIRMED, MatchStatus.DISMISSED)),
        )
    )
    return {(normalize_name(m.raw_name), m.raw_code): m for m in rows}


def _supersede_pending(session: OrmSession, report_id: int) -> None:
    for m in session.scalars(
        select(TargetMatch).where(
            TargetMatch.report_id == report_id, TargetMatch.status == MatchStatus.PENDING
        )
    ):
        m.status = MatchStatus.SUPERSEDED


def link_analysis_targets(
    session: OrmSession, report: ResearchReport, analysis: Analysis
) -> list[AnalysisTarget]:
    """把一版分析的 result.targets 经瀑布落成关联行；未落成的进人工确认队列。

    在 run_analysis 落库后同事务调用（不 commit）。旧版本遗留的 pending 条目置
    superseded（新版本会重建等效条目），已确认/已驳回的判定按（归一名, 原码）复用，
    分析师不因重跑被重复打扰。
    """
    result: dict = analysis.result or {}
    master = MasterIndex.load(session)
    prior = _prior_decisions(session, report.id)
    _supersede_pending(session, report.id)

    links: list[AnalysisTarget] = []
    for seq, t in enumerate(result.get("targets") or []):
        raw_code = t.get("code")
        res = master.resolve(name=t.get("name") or "", code=raw_code, code_source=t.get("code_source"))

        reuse = prior.get((normalize_name(t.get("name") or ""), raw_code))
        match_id: int | None = None
        if res.auto:
            target_code, code_source = res.code, "text"
        elif reuse is not None and reuse.status == MatchStatus.CONFIRMED:
            target_code, code_source, match_id = reuse.resolved_code, "manually_confirmed", reuse.id
        else:
            match = TargetMatch(
                report_id=report.id,
                analysis_id=analysis.id,
                raw_name=t.get("name") or "",
                raw_code=raw_code,
                reason=res.reason or MatchReason.NO_HIT,
                candidates=[asdict(c) for c in res.candidates],
                status=MatchStatus.DISMISSED if reuse is not None else MatchStatus.PENDING,
                resolved_by=reuse.resolved_by if reuse is not None else None,
                resolved_at=reuse.resolved_at if reuse is not None else None,
            )
            session.add(match)
            session.flush()
            target_code, code_source, match_id = None, t.get("code_source"), match.id

        link = AnalysisTarget(
            analysis_id=analysis.id,
            report_id=report.id,
            seq=seq,
            target_code=target_code,
            match_id=match_id,
            raw_name=t.get("name") or "",
            raw_code=raw_code,
            stance=t.get("stance") or "提及",
            view=t.get("view") or "",
            has_forecast=t.get("has_forecast") is True,
            code_source=code_source,
        )
        session.add(link)
        links.append(link)

    session.flush()
    return links


def pending_matches(session: OrmSession) -> list[tuple[TargetMatch, ResearchReport]]:
    """待确认队列（含研报标题上下文），创建时间倒序。"""
    rows = session.execute(
        select(TargetMatch, ResearchReport)
        .join(ResearchReport, ResearchReport.id == TargetMatch.report_id)
        .where(TargetMatch.status == MatchStatus.PENDING)
        .order_by(TargetMatch.created_at.desc(), TargetMatch.id.desc())
    ).all()
    return [(m, r) for m, r in rows]


def resolve_match(
    session: OrmSession, match: TargetMatch, user_id: int, *, code: str | None, dismiss: bool = False
) -> AnalysisTarget | None:
    """队列终审：dismiss=True 驳回（认定非标的）；否则确认到指定代码（须在主数据中）。

    同步更新该队列条目对应的 analysis_targets 关联行（target_code + manually_confirmed）。
    返回更新的关联行（驳回时也可能有）。
    """
    now = dt.datetime.now(dt.timezone.utc)
    match.status = MatchStatus.DISMISSED if dismiss else MatchStatus.CONFIRMED
    match.resolved_code = None if dismiss else code
    match.resolved_by = user_id
    match.resolved_at = now

    link = session.scalar(
        select(AnalysisTarget).where(AnalysisTarget.match_id == match.id)
    )
    if link is not None:
        if dismiss:
            link.target_code = None
        else:
            link.target_code = code
            link.code_source = "manually_confirmed"
    session.flush()
    return link
