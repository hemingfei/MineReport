"""分析管道（#15）：清洗后 markdown 整篇单次 LLM 提取 → 归一 → 版本链落库。

策略（spec 分析管道决策，#6 spike 18 组实测结论）：
- 整篇单次调用为主（成本/速度/质量全面胜出）；>analysis_max_input_chars 才分块兜底。
- publish_date 正则锚定首页优先，LLM 兜底但标记 publish_date_source=llm 不直接采信
  （spike 证实 LLM 会凭世界知识回填日期）。锚定出的日期也只落在分析产物里，
  不回写 reports.publish_date（那是组合键成员，入库时已由上传者/连接器确定）。
- 标的代码瀑布：LLM 给的 6 位代码若在正文中出现 → code_source=text；
  否则 code_source=inferred（后续票接人工确认队列；spike 证实 LLM 补码不可信）。
- 枚举漂移兜底：LLM 偶尔回吐 stance=增持 这类训练词，按同义表归一到 spec 枚举。
- prompt 模板版本化入库（prompt_templates），重跑记录 prompt_version。

题材/标的原始串仍完整暂存于 result（提取时审计快照），规范化投影由 #16（标的瀑布）
与 #17（题材词表关联）在落库后同事务回写。
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from . import search
from .config import Settings, get_settings
from .errors import AnalysisError
from .llm import LLMClient, parse_llm_json
from .models import (
    Analysis,
    AnalysisAuthor,
    AnalysisTarget,
    PromptTemplate,
    ReportFile,
    ReportTheme,
    ResearchReport,
)
from .targets import link_analysis_targets
from .themes import link_analysis_themes

# ---------- 当前版投影谓词（题材浏览/研报过滤/署名搜索/覆盖查询共用） ----------

_Projection = type[ReportTheme] | type[AnalysisTarget] | type[AnalysisAuthor]


def current_link(proj: _Projection, report: ResearchReport | None = None):
    """投影行的"当前版"链接条件：投影 analysis_id == 研报当前分析指针。

    report=None 为查询形态（与 ResearchReport.current_analysis_id 列比较，查询需
    join 投影表与 ResearchReport）；传入具体研报对象为值形态（与其已加载的指针值
    比较，单报告视图免 join）。
    """
    cur = report.current_analysis_id if report is not None else ResearchReport.current_analysis_id
    return proj.analysis_id == cur


def live_current_conds(proj: _Projection) -> tuple:
    """当前版投影查询条件组：链接条件 + 研报未删。

    换版策略（指针语义）与软删口径的唯一出处，三张投影表同款适用；调用方自行
    负责 join 投影表与 ResearchReport——各端点的 join 方向 / count / distinct /
    exists 形状是合法差异。
    """
    return (current_link(proj), ResearchReport.deleted_at.is_(None))


# ---------- prompt v1（spike fulltext 胜出策略，枚举对齐 spec 终版） ----------

SCHEMA_HINT = """{
  "broker": "券商名称",
  "authors": [{"name": "分析师姓名", "cert": "执业证书号(可选)"}],
  "publish_date": "YYYY-MM-DD",
  "report_type": "点评|深度|策略|其他",
  "title": "报告标题(去掉书名号)",
  "summary": "200字以内中文总结: 核心观点与投资逻辑",
  "themes": [{"name": "题材词", "reason": "一句话依据"}],
  "targets": [{"code": "6位股票代码或null", "name": "公司简称", "stance": "推荐|提及|回避",
               "view": "一句话观点", "has_forecast": true}],
  "rating": {"action": "买入|增持|中性|减持|卖出|null(行业报告则null)", "maintained": true},
  "risk_notes": "风险提示原文要点(没有则空串)"
}"""

SYSTEM_PROMPT_V1 = (
    "你是A股券商研报结构化提取器。输入是研报PDF转成的markdown：无标题层级、表格可能碎片化、"
    "图已丢失但图注残留。只依据文本证据提取，禁止编造；提取不到的字段用null。"
    "股票代码必须来自正文文本，禁止凭记忆补码。"
    "题材词是市场通用叫法(如:AI算力、消费电子、创新药)。"
    "stance表达标的层面券商是否重点推荐，只能取 推荐|提及|回避；评级语义在rating字段，勿混入stance。"
    "只输出一个JSON对象，不要输出其他任何文字。"
)

USER_TEMPLATE_V1 = (
    "研报全文：\n```\n{markdown}\n```\n\n提取为JSON：\n" + SCHEMA_HINT
)


@dataclass(frozen=True)
class PromptSpec:
    version: str
    system_prompt: str
    user_template: str


# 版本登记表：新增版本在此追加并同步建迁移不必要（数据非结构变更），seed 幂等补插
PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(version="v1", system_prompt=SYSTEM_PROMPT_V1, user_template=USER_TEMPLATE_V1),
)


def ensure_prompt_seeded(session: OrmSession) -> None:
    """把代码里登记的 prompt 版本幂等补插入库（worker 首次分析前调用）。"""
    existing = set(session.scalars(select(PromptTemplate.version)))
    for spec in PROMPTS:
        if spec.version not in existing:
            session.add(
                PromptTemplate(
                    version=spec.version,
                    system_prompt=spec.system_prompt,
                    user_template=spec.user_template,
                )
            )
    session.flush()


def current_prompt(session: OrmSession) -> PromptTemplate:
    ensure_prompt_seeded(session)
    template = session.scalar(select(PromptTemplate).order_by(PromptTemplate.id.desc()).limit(1))
    assert template is not None  # ensure_prompt_seeded 之后必然存在
    return template


# ---------- 存储结构（spec schema + 两个后处理字段） ----------

class AuthorItem(BaseModel):
    name: str
    cert: str | None = None


class ThemeItem(BaseModel):
    name: str
    reason: str = ""


class TargetItem(BaseModel):
    code: str | None = Field(default=None, description="6 位代码；None = 未提取到")
    name: str
    stance: str = Field(description="推荐|提及|回避")
    view: str = ""
    has_forecast: bool = False
    code_source: str | None = Field(
        default=None, description="text|inferred|manually_confirmed；code 为 None 时本字段为 None"
    )


class RatingItem(BaseModel):
    action: str | None = None  # 买入|增持|中性|减持|卖出
    maintained: bool | None = None


class AnalysisResult(BaseModel):
    broker: str = ""
    authors: list[AuthorItem] = []
    publish_date: dt.date | None = None
    report_type: str = "其他"  # 点评|深度|策略|其他
    title: str = ""
    summary: str = ""
    themes: list[ThemeItem] = []
    targets: list[TargetItem] = []
    rating: RatingItem = Field(default_factory=RatingItem)
    risk_notes: str = ""
    publish_date_source: str | None = None  # regex_head | llm | None


# ---------- 归一（枚举漂移兜底 + 正则锚定 + 代码瀑布） ----------

# spike 实测 LLM 会回吐训练语料里的评级词：映射到 spec 终版枚举
_STANCE_MAP = {
    "买入": "推荐", "增持": "推荐", "推荐": "推荐", "强推": "推荐", "强烈推荐": "推荐",
    "提及": "提及", "中性": "提及", "持有": "提及", "关注": "提及",
    "回避": "回避", "减持": "回避", "卖出": "回避", "规避": "回避",
}
_ACTION_MAP = {
    "买入": "买入", "增持": "增持", "中性": "中性", "减持": "减持", "卖出": "卖出",
    "持有": "中性", "观望": "中性", "强烈推荐": "买入", "强推": "买入", "推荐": "买入",
}
_REPORT_TYPE_KEYS = (
    ("深度", "深度"),
    ("点评", "点评"), ("跟踪", "点评"), ("动态", "点评"),
    ("策略", "策略"), ("行业", "策略"), ("宏观", "策略"),
)

_DATE_CJK = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 前后不能再紧跟数字/日期分隔符（防截断"2024/12/31"或粘连"2024-12-31-1"）
_DATE_NUM = re.compile(r"(?<![\d/.-])(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d/.-])")


def anchor_publish_date(markdown: str, head_chars: int) -> dt.date | None:
    """正则锚定"首页"（文档头窗口）里的发布日期。

    两层策略（真实样本校准）：
    1. 年月日形态（容 PDF 空格拆字"2024 年 12月 31日"）优先——标题页发布日期的标准
       写法，首个命中即锚；
    2. 纯数字日期仅当该行只出现一个时采信——股价走势图的坐标轴刻度行
       （如"2024/1/2 2024/5/2 2024/8/31"）一行含多个日期，据此排除。
    """
    head = markdown[:head_chars]

    m = _DATE_CJK.search(head)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass  # 形似日期但非法（如 13 月），落到数字策略

    for line in head.splitlines():
        found = _DATE_NUM.findall(line)
        if len(found) == 1:
            y, mo, d = (int(g) for g in found[0])
            try:
                return dt.date(y, mo, d)
            except ValueError:
                continue
    return None


def _normalize_code(raw: object) -> str | None:
    code = re.sub(r"\D", "", str(raw or ""))
    return code if len(code) == 6 else None


def _map_enum(raw: object, table: dict[str, str], default: str | None) -> str | None:
    return table.get(str(raw or "").strip(), default)


def _map_report_type(raw: object) -> str:
    """子串匹配（LLM 常回吐'公司深度报告'这类短语）；深度键在最前，'深度跟踪'判深度。"""
    txt = str(raw or "")
    for key, value in _REPORT_TYPE_KEYS:
        if key in txt:
            return value
    return "其他"


def normalize_result(
    raw: dict, markdown: str, settings: Settings
) -> AnalysisResult:
    """LLM 原始输出 → spec schema：枚举归一、代码瀑布、publish_date 锚定。
    字段级容错（LLM 输出形状漂移不致命），结构级失败抛 schema_invalid。"""
    try:
        anchored = anchor_publish_date(markdown, settings.analysis_head_chars)

        targets: list[TargetItem] = []
        for t in raw.get("targets") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            code = _normalize_code(t.get("code"))
            code_source = None
            if code is not None:
                code_source = "text" if code in markdown else "inferred"
            targets.append(
                TargetItem(
                    code=code,
                    name=name,
                    stance=_map_enum(t.get("stance"), _STANCE_MAP, "提及") or "提及",
                    view=str(t.get("view") or ""),
                    has_forecast=t.get("has_forecast") is True,
                    code_source=code_source,
                )
            )

        rating_raw = raw.get("rating") or {}
        if not isinstance(rating_raw, dict):
            rating_raw = {}

        llm_date: dt.date | None = None
        raw_date = str(raw.get("publish_date") or "").strip()
        if raw_date:
            m = re.fullmatch(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw_date)
            if m:
                try:
                    llm_date = dt.date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    llm_date = None

        if anchored is not None:
            publish_date, source = anchored, "regex_head"
        elif llm_date is not None:
            publish_date, source = llm_date, "llm"
        else:
            publish_date, source = None, None

        return AnalysisResult(
            broker=str(raw.get("broker") or ""),
            authors=[
                AuthorItem(name=str(a.get("name") or "").strip(), cert=(str(a["cert"]).strip() or None) if a.get("cert") else None)
                for a in raw.get("authors") or []
                if isinstance(a, dict) and str(a.get("name") or "").strip()
            ],
            publish_date=publish_date,
            report_type=_map_report_type(raw.get("report_type")),
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            themes=[
                ThemeItem(name=str(t.get("name") or "").strip(), reason=str(t.get("reason") or ""))
                for t in raw.get("themes") or []
                if isinstance(t, dict) and str(t.get("name") or "").strip()
            ],
            targets=targets,
            rating=RatingItem(
                action=_map_enum(rating_raw.get("action"), _ACTION_MAP, None),
                maintained=rating_raw.get("maintained") if isinstance(rating_raw.get("maintained"), bool) else None,
            ),
            risk_notes=str(raw.get("risk_notes") or ""),
            publish_date_source=source,
        )
    except AnalysisError:
        raise
    except Exception as e:
        raise AnalysisError("schema_invalid", f"分析结果归一失败：{e}") from e


# ---------- LLM 装配（测试经 monkeypatch build_llm 注入 mock） ----------

def llm_ready(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.llm_base_url and s.llm_api_key and s.llm_model)


def build_llm(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    return LLMClient(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        timeout=s.llm_timeout_seconds,
    )


# ---------- 提取主路径 ----------

def _fulltext_messages(template: PromptTemplate, markdown: str) -> list[dict]:
    return [
        {"role": "system", "content": template.system_prompt},
        {"role": "user", "content": template.user_template.replace("{markdown}", markdown)},
    ]


def _chunk_text(md: str, size: int, overlap: int) -> list[str]:
    step = size - overlap
    return [md[i : i + size] for i in range(0, len(md), step) if md[i : i + size].strip()]


def _extract(
    llm: LLMClient, template: PromptTemplate, markdown: str, settings: Settings
) -> tuple[dict, str, int, int, int]:
    """整篇单次为主；超长降级为分块提取 + 一次合并（spike chunked 策略的兜底版）。

    返回 (原始 dict, model, prompt_tokens, completion_tokens, 调用次数)。
    """
    if len(markdown) <= settings.analysis_max_input_chars:
        chat = llm.chat(_fulltext_messages(template, markdown))
        return parse_llm_json(chat.content), chat.model, chat.prompt_tokens, chat.completion_tokens, 1

    chunks = _chunk_text(
        markdown, settings.analysis_chunk_chars, settings.analysis_chunk_overlap
    )
    partials: list[dict] = []
    pt = ct = 0
    model = llm.model
    for i, chunk in enumerate(chunks):
        chat = llm.chat(
            [
                {"role": "system", "content": template.system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"这是研报第{i + 1}/{len(chunks)}块，只提取本块可见字段，看不到的为null：\n"
                        f"```\n{chunk}\n```\n\n提取为JSON：\n{SCHEMA_HINT}"
                    ),
                },
            ]
        )
        partials.append(parse_llm_json(chat.content))
        pt += chat.prompt_tokens
        ct += chat.completion_tokens
        model = chat.model

    import json as _json

    merge_prompt = (
        f"以下是同一篇研报分块提取出的局部JSON（{len(partials)}块），合并为一份完整JSON。"
        "规则： 标题/券商/作者/日期/评级取最早出现的非null； targets/themes按code与name去重合并，"
        "冲突时以观点更具体的为准; summary综合各块要点重写为200字内。\n"
        + "\n".join(f"块{i + 1}: {_json.dumps(p, ensure_ascii=False)}" for i, p in enumerate(partials))
        + f"\n\n输出合并后的完整JSON：\n{SCHEMA_HINT}"
    )
    chat = llm.chat(
        [
            {"role": "system", "content": template.system_prompt},
            {"role": "user", "content": merge_prompt},
        ]
    )
    pt += chat.prompt_tokens
    ct += chat.completion_tokens
    return parse_llm_json(chat.content), chat.model, pt, ct, len(partials) + 1


def latest_converted_file(session: OrmSession, report_id: int) -> ReportFile | None:
    """分析输入文件的选择语义：最近转换完成者（多来源文件后到优先，与 /markdown 一致）。
    search._latest_body 同语义（正文入索引口径）——改"最新"规则须两处同步。"""
    return session.scalar(
        select(ReportFile)
        .where(ReportFile.report_id == report_id, ReportFile.converted_at.is_not(None))
        .order_by(ReportFile.converted_at.desc(), ReportFile.id.desc())
        .limit(1)
    )


def run_analysis(
    session: OrmSession,
    report: ResearchReport,
    file: ReportFile,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> Analysis:
    """对单个已转换文件跑一次分析并挂版本链（不 commit，由调用方统一提交）。"""
    s = settings or get_settings()
    if not file.markdown_text:
        raise AnalysisError("markdown_missing", f"report_file {file.id} 尚无清洗后正文")
    # 显式注入 client（测试 mock）时不查配置；生产路径由调用方（worker/API）先过 llm_ready
    if llm is None:
        if not llm_ready(s):
            raise AnalysisError("llm_not_configured", "LLM 未配置（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）")
        client = build_llm(s)
    else:
        client = llm

    template = current_prompt(session)
    t0 = time.perf_counter()
    raw, model, pt, ct, _calls = _extract(client, template, file.markdown_text, s)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    result = normalize_result(raw, file.markdown_text, s)

    prev_version = session.scalar(
        select(func.max(Analysis.version)).where(Analysis.report_id == report.id)
    )
    analysis = Analysis(
        report_id=report.id,
        report_file_id=file.id,
        version=(prev_version or 0) + 1,
        prompt_version=template.version,
        model=model,
        prompt_tokens=pt,
        completion_tokens=ct,
        duration_ms=duration_ms,
        result=result.model_dump(mode="json"),
    )
    session.add(analysis)
    session.flush()
    report.current_analysis_id = analysis.id
    search.refresh_search_vector(session, report.id)  # #18：总结随版本链头指针移动入索引
    # #16 回写：标的原始串经规范化瀑布落成 analysis_targets 关联（未落成的进人工确认队列）
    link_analysis_targets(session, report, analysis)
    # #17 回写：题材关联受控词表（在册直连/未知进待审），分析师署名落 analysis_authors 投影
    link_analysis_themes(session, report, analysis)
    _link_analysis_authors(session, report, analysis)
    return analysis


def _link_analysis_authors(
    session: OrmSession, report: ResearchReport, analysis: Analysis
) -> list[AnalysisAuthor]:
    """result.authors 原始串落投影行（覆盖查询用，#17）。cert 缺失存 NULL。"""
    links: list[AnalysisAuthor] = []
    seq = 0
    for a in (analysis.result or {}).get("authors") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        cert = a.get("cert")
        links.append(
            AnalysisAuthor(
                analysis_id=analysis.id,
                report_id=report.id,
                seq=seq,
                name=name,
                cert=str(cert).strip() or None if cert else None,
            )
        )
        seq += 1
    session.add_all(links)
    session.flush()
    return links
