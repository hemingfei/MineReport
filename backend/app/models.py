"""SQLAlchemy 模型。Task 为 API 与 worker 共享的任务队列载体；User/Invitation/UserSession
支撑邀请制认证；ResearchReport/ReportFile 支撑研报入库（ADR-0001 组合键 + 软删除）。"""

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
