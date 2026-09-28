"""综合分析管道（#20）：题材下研报集合 → LLM 二次分析 → 版本链落库。

流程与缓存语义（spec）：
- 输入集合 = 题材当前分析版本关联的未删研报（#17 投影），按发布日期取最近
  synthesis_max_reports 篇（token 闸门）；输入指纹 = 研报 id 集合的 sha256。
- POST 时比对最新版指纹：未变命中缓存不重算（不重复烧 token）；手动刷新
  强制重算并 version++（题材词表变更不自动失效缓存，刷新按钮兜底）。
- 任务 payload 携带 POST 时点的快照 id 集合（选择即请求），worker 运行时校验
  快照成员仍然在库且有当前分析（被软删/换版失联的剔除），实际使用的集合才是
  记录在案的证据边界——报告序位即引用编号 R1..Rn。
- 结论引用纪律：LLM 输出的 report_refs 经钳位（越界丢弃）归一，引用全空的
  结论整条丢弃（无引用即不可溯源）；共识标的代码必须在输入材料出现过
  （白名单校验，防 LLM 凭记忆补码——#6 spike 同款教训）。

prompt 不入 prompt_templates 表：该表"当前版 = id 最大者"的选择语义会把综合
prompt 误供给单篇分析（两个管道共用表会互相污染），综合 prompt 版本以代码常量
登记（PROMPT_VERSION），行上留审计字段即可。
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from . import analysis
from .config import Settings, get_settings
from .errors import AnalysisError
from .llm import LLMClient, parse_llm_json
from .models import (
    Analysis,
    AnalysisTarget,
    ReportTheme,
    ResearchReport,
    Synthesis,
    Task,
    TaskStatus,
    Theme,
)
from .themes import ThemeStatus

# ---------- prompt synth-v1 ----------

SCHEMA_HINT = """{
  "common_conclusions": [{"text": "多份研报的共性结论(中文)", "report_refs": [1, 2]}],
  "consensus_targets": [{"name": "公司简称", "code": "6位代码或null", "view": "共识观点一句话",
                          "report_refs": [1, 3]}],
  "divergences": [{"text": "分歧点(写明各方立场与依据)", "report_refs": [1, 2]}]
}"""

SYSTEM_PROMPT = (
    "你是A股研报综合分析师。输入是同一题材下多份研报的结构化摘要（编号 R1..Rn）。"
    "只依据给定材料综合，禁止引入材料之外的信息或编造观点。"
    "每条结论必须给出支撑它的材料编号（report_refs，1 起始）；分歧点要写明哪些材料持何种立场。"
    "共识标的的 code 只能来自材料中出现过的代码，材料没有就填 null。"
    "只输出一个JSON对象，不要输出其他任何文字。"
)

USER_TEMPLATE = (
    "题材：{theme_name}（{theme_definition}）\n\n"
    "研报材料摘要：\n{digest}\n\n"
    "综合为JSON：\n" + SCHEMA_HINT
)

PROMPT_VERSION = "synth-v1"


# ---------- 产出结构 ----------

class CommonConclusion(BaseModel):
    text: str
    report_refs: list[int] = []  # 输入材料序位（1 起始，已钳位去重排序）


class ConsensusTarget(BaseModel):
    name: str
    code: str | None = None  # 白名单校验后的 6 位码；None = 材料未给出可信代码
    view: str = ""
    report_refs: list[int] = []


class Divergence(BaseModel):
    text: str
    report_refs: list[int] = []


class SynthesisResult(BaseModel):
    common_conclusions: list[CommonConclusion] = []
    consensus_targets: list[ConsensusTarget] = []
    divergences: list[Divergence] = []
    input_total: int = 0  # 题材下符合条件研报总数（截断前口径）
    truncated: bool = False  # 输入超 synthesis_max_reports 被截断


# ---------- 输入选集与指纹 ----------

def select_input_reports(
    session: OrmSession, theme: Theme, settings: Settings | None = None
) -> tuple[list[int], int]:
    """题材的输入研报选集：当前分析版本关联（#17 投影）、未删，按发布日期降序取最近 N 篇。

    返回 (选集 id 列表, 符合条件总数)；截断时总数 > 选集长度（result.truncated）。
    """
    s = settings or get_settings()
    conds = (
        ReportTheme.theme_id == theme.id,
        *analysis.live_current_conds(ReportTheme),
    )
    total = int(
        session.scalar(
            select(func.count(func.distinct(ResearchReport.id))).join(
                ReportTheme, ReportTheme.report_id == ResearchReport.id
            ).where(*conds)
        )
        or 0
    )
    # distinct 经子查询（一份研报同版多条题材关联行只取一次；DISTINCT 直挂
    # ORDER BY 会撞 Postgres "ORDER BY 表达式须在 select list" 限制）
    linked = (
        select(ReportTheme.report_id.label("rid"))
        .join(ResearchReport, ResearchReport.id == ReportTheme.report_id)
        .where(*conds)
        .distinct()
        .subquery()
    )
    ids = list(
        session.scalars(
            select(ResearchReport.id)
            .join(linked, linked.c.rid == ResearchReport.id)
            .order_by(ResearchReport.publish_date.desc(), ResearchReport.id.desc())
            .limit(s.synthesis_max_reports)
        )
    )
    return ids, total


def input_fingerprint(report_ids: list[int]) -> str:
    """输入集合哈希指纹：排序后规范 JSON 的 sha256（与集合序无关，缓存比对稳定）。"""
    canonical = json.dumps(sorted(report_ids), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest_for_theme(session: OrmSession, theme_id: int) -> Synthesis | None:
    """题材最新综合分析版本（缓存比对/详情入口共用）。"""
    return session.scalar(
        select(Synthesis)
        .where(Synthesis.theme_id == theme_id)
        .order_by(Synthesis.version.desc())
        .limit(1)
    )


def theme_unavailable_reason(theme: Theme) -> str | None:
    """题材不可综合的原因（None = 可综合）：合并/停用是死条目（关联已随迁/出列），
    交人工处理。API 与 worker 共用本判据（错误面各自包装）。"""
    if theme.status == ThemeStatus.MERGED:
        return f"题材「{theme.name}」已合并，不可综合"
    if theme.status == ThemeStatus.RETIRED:
        return f"题材「{theme.name}」已停用，不可综合"
    return None


def inflight_task(session: OrmSession, theme_id: int, report_ids: list[int]) -> Task | None:
    """同题材且同输入快照的在途 synthesize 任务（防重复触发重复烧 token）。

    仅快照一致才复用：输入已变化的请求仍入新任务（携带新选集）。
    """
    tasks = session.scalars(
        select(Task)
        .where(
            Task.kind == "synthesize",
            Task.status.in_((TaskStatus.UPLOADED, TaskStatus.ANALYZING)),
        )
        .order_by(Task.id)
    )
    wanted = sorted(report_ids)
    for t in tasks:
        payload = t.payload or {}
        if payload.get("theme_id") == theme_id and sorted(payload.get("report_ids") or []) == wanted:
            return t
    return None


def latest_version(session: OrmSession, theme_id: int) -> int:
    return session.scalar(select(func.max(Synthesis.version)).where(Synthesis.theme_id == theme_id)) or 0


# ---------- 材料摘要（LLM 输入） ----------

def _code6(raw: object, known: set[str]) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) == 6 and digits in known else None


def build_digest(
    session: OrmSession, reports: list[ResearchReport]
) -> tuple[str, set[str]]:
    """把各研报当前分析的结构化要点编成编号材料（R1..Rn）。

    标的取 #16 投影（analysis_targets）：code 只呈现瀑布落成的规范码（未落成
    的只给名称）——这正是共识标的代码白名单的口径。返回 (材料文本, 已知代码集)。
    """
    analysis_ids = [r.current_analysis_id for r in reports if r.current_analysis_id]
    analyses = {
        a.id: a
        for a in session.scalars(select(Analysis).where(Analysis.id.in_(analysis_ids)))
    }
    links: dict[int, list[AnalysisTarget]] = {}
    for l in session.scalars(
        select(AnalysisTarget)
        .where(AnalysisTarget.analysis_id.in_(analysis_ids))
        .order_by(AnalysisTarget.seq)
    ):
        links.setdefault(l.analysis_id, []).append(l)

    known_codes: set[str] = set()
    blocks: list[str] = []
    for i, r in enumerate(reports, start=1):
        a = analyses.get(r.current_analysis_id)
        result: dict = (a.result or {}) if a is not None else {}
        lines = [f"[R{i}] 券商={r.broker} 日期={r.publish_date.isoformat()} 标题={r.title}"]
        summary = str(result.get("summary") or "").strip()
        if summary:
            lines.append(f"总结：{summary}")
        targets = links.get(r.current_analysis_id, [])
        if targets:
            parts = []
            for t in targets:
                if t.target_code:
                    known_codes.add(t.target_code)
                    parts.append(f"{t.raw_name}({t.target_code},{t.stance})")
                else:
                    parts.append(f"{t.raw_name}({t.stance})")
            lines.append("标的：" + "；".join(parts))
        rating = result.get("rating")
        action = rating.get("action") if isinstance(rating, dict) else None
        if action:
            lines.append(f"评级：{action}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), known_codes


def _messages(theme: Theme, digest: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.replace("{theme_name}", theme.name)
            .replace("{theme_definition}", (theme.definition or "").strip() or "无定义")
            .replace("{digest}", digest),
        },
    ]


# ---------- 归一（引用钳位 + 代码白名单 + 不可溯源结论丢弃） ----------

def _clean_refs(raw: object, n: int) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: set[int] = set()
    for r in raw:
        try:
            v = int(r)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= n:
            out.add(v)
    return sorted(out)


def normalize_synthesis(raw: dict, n_reports: int, known_codes: set[str]) -> SynthesisResult:
    """LLM 原始输出 → spec 产出结构。引用越界丢弃；引用全空的结论整条丢弃
    （spec 用户故事 18：每条判断可溯源）；共识标的代码不在输入材料中置 null。"""
    try:
        conclusions: list[CommonConclusion] = []
        for c in raw.get("common_conclusions") or []:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()
            refs = _clean_refs(c.get("report_refs"), n_reports)
            if text and refs:
                conclusions.append(CommonConclusion(text=text, report_refs=refs))

        targets: list[ConsensusTarget] = []
        for t in raw.get("consensus_targets") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            refs = _clean_refs(t.get("report_refs"), n_reports)
            if name and refs:
                targets.append(
                    ConsensusTarget(
                        name=name,
                        code=_code6(t.get("code"), known_codes),
                        view=str(t.get("view") or ""),
                        report_refs=refs,
                    )
                )

        divergences: list[Divergence] = []
        for d in raw.get("divergences") or []:
            if not isinstance(d, dict):
                continue
            text = str(d.get("text") or "").strip()
            refs = _clean_refs(d.get("report_refs"), n_reports)
            if text and refs:
                divergences.append(Divergence(text=text, report_refs=refs))

        return SynthesisResult(
            common_conclusions=conclusions,
            consensus_targets=targets,
            divergences=divergences,
        )
    except AnalysisError:
        raise
    except Exception as e:
        raise AnalysisError("schema_invalid", f"综合分析结果归一失败：{e}") from e


# ---------- 主路径 ----------

def load_input_reports(session: OrmSession, report_ids: list[int]) -> list[ResearchReport]:
    """快照校验：仍处于未删且有当前分析的成员才可用（保序）。

    POST 到 worker 运行之间研报可能被软删或重跑换版失联——剔除后以实际
    使用集合作证据边界（指纹同口径重算），集合为空即任务失败。
    """
    rows = {
        r.id: r
        for r in session.scalars(
            select(ResearchReport).where(
                ResearchReport.id.in_(report_ids),
                ResearchReport.deleted_at.is_(None),
                ResearchReport.current_analysis_id.is_not(None),
            )
        )
    }
    return [rows[rid] for rid in report_ids if rid in rows]


def run_synthesis(
    session: OrmSession,
    theme: Theme,
    report_ids: list[int],
    *,
    created_by: int,
    input_total: int,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> Synthesis:
    """对快照研报集合跑一次综合分析并落版本链（不 commit，由调用方统一提交）。

    输入快照由 POST 时点决定（选集 + 缓存判据）；本函数只做可用性校验与实际
    集合记录。失败不留半版本：Synthesis 行仅在提取归一全部成功后插入。
    """
    s = settings or get_settings()
    reports = load_input_reports(session, report_ids)
    if not reports:
        raise AnalysisError("no_input_reports", "输入研报集合为空（快照成员已不可用）")

    if llm is None:
        if not analysis.llm_ready(s):
            raise AnalysisError("llm_not_configured", "LLM 未配置（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）")
        client = analysis.build_llm(s)  # 经 analysis 模块属性调用：测试 monkeypatch 同一生效
    else:
        client = llm

    digest, known_codes = build_digest(session, reports)
    used_ids = [r.id for r in reports]
    t0 = time.perf_counter()
    chat = client.chat(_messages(theme, digest))
    duration_ms = int((time.perf_counter() - t0) * 1000)

    result = normalize_synthesis(parse_llm_json(chat.content), len(reports), known_codes)
    result.input_total = input_total
    # 截断口径 = POST 时点选集是否触到闸门；运行期剔除软删成员不属于截断
    result.truncated = input_total > len(report_ids)

    synthesis = Synthesis(
        theme_id=theme.id,
        version=latest_version(session, theme.id) + 1,
        input_fingerprint=input_fingerprint(used_ids),
        report_ids=used_ids,
        prompt_version=PROMPT_VERSION,
        model=chat.model,
        prompt_tokens=chat.prompt_tokens,
        completion_tokens=chat.completion_tokens,
        duration_ms=duration_ms,
        result=result.model_dump(mode="json"),
        created_by=created_by,
    )
    session.add(synthesis)
    session.flush()
    return synthesis
