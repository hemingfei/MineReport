"""SQLAlchemy 模型。Task 为 API 与 worker 共享的任务队列载体；User/Invitation/UserSession
支撑邀请制认证；ResearchReport/ReportFile 支撑研报入库（ADR-0001 组合键 + 软删除）；
PromptTemplate/Analysis/Tag/ReportTag 支撑分析管道（版本链 + 审计）与自由 tag（#15）。"""

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text
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
