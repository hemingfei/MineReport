"""SQLAlchemy 模型。Task 为 API 与 worker 共享的任务队列载体；User/Invitation/UserSession
支撑邀请制认证；ResearchReport/ReportFile 支撑研报入库（ADR-0001 组合键 + 软删除）；
PromptTemplate/Analysis/Tag/ReportTag 支撑分析管道（版本链 + 审计）与自由 tag（#15）；
Target/TargetIndustryHistory/TargetMatch/AnalysisTarget 支撑标的主数据、规范化瀑布与
人工确认队列（#16）；Theme/ReportTheme/ThemeMembership/AnalysisAuthor 支撑题材受控
词表治理、研报/标的关联与分析师覆盖查询（#17）；search_vector 支撑中文全文检索（#18）。"""

import datetime as dt
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Role:
    ADMIN = "admin"
    ANALYST = "analyst"
    READER = "reader"

    ALL = (ADMIN, ANALYST, READER)
    # 层级序（require_role 的比较基准）：矩阵各行的授权集合都是连续层级
    RANK: dict[str, int] = {READER: 0, ANALYST: 1, ADMIN: 2}


class TaskStatus:
    # spec 状态机字面：uploaded → converting → analyzing → done | failed。
    # uploaded 是一切任务的入队态（analyze 任务并非字面"上传"，指排队待领取）；
    # converting/analyzing 同时充当 worker 租约在途标记，超过租约可重领。
    UPLOADED = "uploaded"
    CONVERTING = "converting"
    ANALYZING = "analyzing"
    DONE = "done"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), comment="任务类型：convert/analyze/synthesize/...")
    status: Mapped[str] = mapped_column(String(32), comment="uploaded→converting→analyzing→done|failed")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次被领取的时间（租约；converting 超时视为遗弃可重新领取）",
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, comment="登录邮箱")
    password_hash: Mapped[str] = mapped_column(String(255), comment="scrypt 格式串，不含明文")
    display_name: Mapped[str] = mapped_column(String(64), comment="显示名")
    role: Mapped[str] = mapped_column(String(16), comment="admin | analyst | reader")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="禁用用户拒绝登录")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, comment="邀请码，创建时一次性生成")
    role: Mapped[str] = mapped_column(String(16), comment="被邀请者注册后获得的角色")
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="可选：限定被邀请者必须用此邮箱注册"
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), comment="邀请人（admin）")
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), comment="过期时间")
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, comment="凭此邀请注册的用户"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserSession(Base):
    """服务端会话：cookie 携带原始 token，库里只存 sha256 哈希。"""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True, comment="sha256 hex")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), comment="固定 TTL，不做滑动续期")


class ResearchReport(Base):
    """研报业务实体：身份 = (标题归一, 券商, 发布日期) 纯组合键（ADR-0001）。

    软删除：deleted_at 置位即隐藏，30 天内可恢复（恢复窗口见 config.report_soft_delete_days）；
    唯一索引只约束未删除行，故"软删后重传同键"会新建条目，恢复旧条目时若撞键返回 409。
    """

    __tablename__ = "reports"
    __table_args__ = (
        Index(
            "uq_reports_identity_active",
            "title_norm",
            "broker",
            "publish_date",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), comment="展示标题（保留原文）")
    title_norm: Mapped[str] = mapped_column(String(512), comment="归一标题（去空白/全半角/大小写），组合键成员")
    broker: Mapped[str] = mapped_column(String(128), comment="券商（组合键成员）")
    publish_date: Mapped[dt.date] = mapped_column(Date, comment="发布日期（组合键成员）")
    current_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("analyses.id", use_alter=True, name="fk_reports_current_analysis", ondelete="SET NULL"),
        nullable=True,
        comment="当前生效的分析版本（版本链头指针；use_alter 破解与 analyses 表的循环依赖）",
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), comment="入库人")
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="软删除时间；NULL 即未删除"
    )
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, comment="删除操作人")
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        nullable=True,
        deferred=True,  # 向量只写不读，常规查询不携带
        comment="标题A/正文B/总结C 的 zhcfg 全文向量（app/search.py 维护）",
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReportFile(Base):
    """研报原始文件与转换产物：多来源文件挂同一研报下；sha256 仅属性不做唯一约束（ADR-0001）。"""

    __tablename__ = "report_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(512), comment="存储抽象的逻辑路径")
    filename: Mapped[str] = mapped_column(String(255), comment="原始文件名")
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64), comment="仅作属性存储，不参与身份判定")
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    markdown_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="清洗后的正文 markdown")
    converted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="转换完成时间；NULL 即未完成"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PromptTemplate(Base):
    """prompt 模板版本化入库（spec 分析管道决策）：重跑时 Analysis 记录所用版本。

    user_template 中 `{markdown}` 为占位符（渲染用 str.replace，模板可含 JSON 花括号）。
    当前模板 = id 最大者；新增版本插行即生效。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(32), unique=True, comment="如 v1")
    system_prompt: Mapped[str] = mapped_column(Text)
    user_template: Mapped[str] = mapped_column(Text, comment="{markdown} 占位")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Analysis(Base):
    """研报结构化分析：版本链（version++），reports.current_analysis_id 指向当前版。

    每版记录完整审计：prompt_version、model、prompt/completion tokens、耗时
    （spec：分析质量的人工迭代轨迹）。result 为归一后的 spec schema JSON
    （含后处理产物 publish_date_source 与 targets[].code_source）。
    题材/标的以原始字符串暂存（受控词表与主数据规范化由 #16/#17 接入）。
    """

    __tablename__ = "analyses"
    __table_args__ = (Index("uq_analyses_report_version", "report_id", "version", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True)
    report_file_id: Mapped[int] = mapped_column(
        ForeignKey("report_files.id"), comment="本版分析的输入文件（多来源文件后到优先）"
    )
    version: Mapped[int] = mapped_column(Integer, comment="版本号，从 1 递增")
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, comment="LLM 调用总耗时（含分块兜底的多趟）")
    result: Mapped[dict] = mapped_column(JSON, comment="spec schema 分析结果（归一后）")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Tag(Base):
    """自由 tag（与受控题材词表是两层体系）：轻量、无状态机、按名复用。"""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, comment="归一后的展示名")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReportTag(Base):
    """研报↔tag 多对多关联（spec 数据模型）。"""

    __tablename__ = "report_tags"

    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Target(Base):
    """标的主数据（#16）：akshare 全量代码+名称 + 申万 2021 行业快照（含生效日期）+ 曾用名。

    身份 = 6 位代码（交易所由代码前缀确定性推导，不单独入库来源）。
    行业字段是"当前分类"快照：来自官网 xls 全史表的每股最新一行，名称经静态码表
    sw2021_l3.csv join（分类标准码→名称的桥，见 research/masterdata/sw2021_bridge.py）。
    匹配词典 = 当前简称 + 曾用名（historical_names），都经 normalize_name 归一。
    """

    __tablename__ = "targets"
    __table_args__ = (Index("ix_targets_name_norm", "name_norm"),)

    code: Mapped[str] = mapped_column(String(6), primary_key=True, comment="6 位股票代码")
    name: Mapped[str] = mapped_column(String(64), comment="当前简称")
    name_norm: Mapped[str] = mapped_column(String(64), comment="归一简称（剥 ST/空格/全角/后缀）")
    exchange: Mapped[str] = mapped_column(String(8), comment="SH | SZ | BJ（代码前缀推导）")
    sw_l1_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    sw_l1_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sw_l2_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    sw_l2_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sw_l3_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    sw_l3_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sw_effective_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, comment="当前行业分类的计入日期（快照生效日期）"
    )
    historical_names: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="曾用名列表（akshare 新浪曾用名回填）"
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TargetIndustryHistory(Base):
    """申万行业分类全史（官网 xls 原样，含退市股）：研报是历史文档，回溯需知"当时"分类。"""

    __tablename__ = "target_industry_history"
    __table_args__ = (
        Index("uq_industry_history", "code", "effective_date", "industry_code", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), comment="6 位股票代码（不外键：xls 含未上市股之外的存量行）")
    effective_date: Mapped[dt.date] = mapped_column(Date, comment="计入日期")
    industry_code: Mapped[str] = mapped_column(String(6), comment="申万 2021 分类标准码")
    source_updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="官网 xls 的更新日期"
    )


class TargetMatch(Base):
    """人工确认队列（规范化瀑布终点，#16）。

    一条 = 某版分析里的一个未能自动落成的标的。candidates 是入队时的瀑布建议快照
    （[{code,name,exchange,sw_l1_name,score}]），主数据重导不会刷新它——队列语义是
    "当时的待办"，确认动作才写终值。确认/驳回后 code_source 见关联行。
    """

    __tablename__ = "target_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    raw_name: Mapped[str] = mapped_column(String(128), comment="LLM 提取的原始名称串")
    raw_code: Mapped[str | None] = mapped_column(String(6), nullable=True, comment="LLM 给的 6 位码（可能凭记忆）")
    reason: Mapped[str] = mapped_column(
        String(32),
        comment="入队原因：inferred_code | code_not_in_master | multi_candidate | no_hit",
    )
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="瀑布建议候选")
    status: Mapped[str] = mapped_column(String(16), comment="pending | confirmed | dismissed | superseded")
    resolved_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnalysisTarget(Base):
    """分析↔标的关联（#16 回写）：result JSON 是提取时审计快照，本表是可关联可查询的投影。

    code_source 在此表的语义：text=代码在正文或由正文名称经确定性瀑布命中（可从文本
    证据机械复现，非模型记忆）；inferred=LLM 凭记忆补；manually_confirmed=人工确认。
    与 result JSON 里的提取时 code_source（仅区分 text/inferred）是两个时点。
    """

    __tablename__ = "analysis_targets"
    __table_args__ = (Index("uq_analysis_targets_seq", "analysis_id", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True, comment="冗余：按标的过滤研报免 join")
    seq: Mapped[int] = mapped_column(Integer, comment="在 result.targets 中的序位")
    target_code: Mapped[str | None] = mapped_column(
        ForeignKey("targets.code"), nullable=True, comment="落成的规范代码；NULL=仍在队列"
    )
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("target_matches.id"), nullable=True, comment="对应队列条目（自动落成时为 NULL）"
    )
    raw_name: Mapped[str] = mapped_column(String(128))
    raw_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    stance: Mapped[str] = mapped_column(String(8), comment="推荐|提及|回避")
    view: Mapped[str] = mapped_column(Text, default="")
    has_forecast: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    code_source: Mapped[str | None] = mapped_column(String(24), nullable=True)


class Theme(Base):
    """题材受控词表（#17）：状态机 待审 → 在册 → 合并/停用；带定义与同义词组。

    身份 = name_norm（NFKC + 去空白，"AI算力"与"AI 算力"同一条目）；seed_code 是种子
    来源的稳定锚点（东财 BKxxxx / 申万 l2_code），人工与 LLM 提议为 NULL。合并后
    merged_into_id 指向去向题材，原名与同义词并入去向的同义词组（"AI算力"与"算力"
    不各立门户）。
    """

    __tablename__ = "themes"
    __table_args__ = (Index("uq_themes_name_norm", "name_norm", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), comment="展示名")
    name_norm: Mapped[str] = mapped_column(String(64), comment="归一名（NFKC+去空白），唯一")
    status: Mapped[str] = mapped_column(String(16), comment="pending | active | merged | retired")
    definition: Mapped[str] = mapped_column(Text, default="", comment="定义（审核时可补）")
    synonyms: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="同义词组（含合并并入的原名）")
    source: Mapped[str] = mapped_column(
        String(16), comment="analysis | manual | seed_em | seed_sw"
    )
    seed_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="种子锚点：东财 BKxxxx / 申万 l2_code；提议类为 NULL"
    )
    proposed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id"), nullable=True, comment="合并去向（status=merged 时非空）"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReportTheme(Base):
    """分析↔题材关联（#17 回写）：result JSON 是提取时审计快照，本表是可查询投影。

    theme_id 非空即已关联词表（在册直连 / 待审提议均落行）；题材浏览与覆盖查询
    按 reports.current_analysis_id 只看当前版。合并题材时 theme_id 随迁。
    """

    __tablename__ = "report_themes"
    __table_args__ = (Index("uq_report_themes_seq", "analysis_id", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True, comment="冗余：按题材过滤研报免 join")
    seq: Mapped[int] = mapped_column(Integer, comment="在 result.themes 中的序位（跳过空名后紧凑编号）")
    theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id"), nullable=True, comment="关联的词表条目；NULL=未能关联（理论上不出现）"
    )
    raw_name: Mapped[str] = mapped_column(String(128), comment="LLM 提取的原始题材词")
    reason: Mapped[str] = mapped_column(Text, default="", comment="一句话归属依据（提取时快照）")


class ThemeMembership(Base):
    """题材成员（spec 数据模型）：题材↔标的中间实体，source ∈ {seed, analysis, manual}。

    joined_at 是首次入库日期（重导入不覆盖）；is_active=False 即退池（成分股移出、
    行业改分类）。seed 同步只动 source=seed 的行，分析/人工成员不受种子重导影响。
    """

    __tablename__ = "theme_memberships"
    __table_args__ = (Index("uq_theme_memberships", "theme_id", "target_code", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("themes.id"), index=True)
    target_code: Mapped[str] = mapped_column(ForeignKey("targets.code"))
    source: Mapped[str] = mapped_column(String(16), comment="seed | analysis | manual")
    joined_at: Mapped[dt.date] = mapped_column(Date, comment="首次入库日期")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="False=已退池")


class AnalysisAuthor(Base):
    """分析↔分析师投影（#17）：result.authors 原始串落行，覆盖查询（观点迁移追踪）用。

    分析师主数据实体（执业证书号唯一键）由后续票按需收敛；本表按（name, cert）原串
    聚合，覆盖查询支持按券商过滤以区分同名。
    """

    __tablename__ = "analysis_authors"
    __table_args__ = (Index("uq_analysis_authors_seq", "analysis_id", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"), index=True, comment="冗余：按分析师过滤研报免 join")
    seq: Mapped[int] = mapped_column(Integer, comment="在 result.authors 中的序位")
    name: Mapped[str] = mapped_column(String(128), comment="LLM 提取的署名原文")
    cert: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="执业证书号（可缺）")
