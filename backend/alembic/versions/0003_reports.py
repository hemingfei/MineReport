"""reports / report_files 表：研报入库与转换管道（#13）

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, comment="展示标题（保留原文）"),
        sa.Column("title_norm", sa.String(length=512), nullable=False,
                  comment="归一标题（去空白/全半角/大小写），组合键成员"),
        sa.Column("broker", sa.String(length=128), nullable=False, comment="券商（组合键成员）"),
        sa.Column("publish_date", sa.Date(), nullable=False, comment="发布日期（组合键成员）"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间；NULL 即未删除"),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["deleted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_reports_identity_active",
        "reports",
        ["title_norm", "broker", "publish_date"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "report_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False, comment="存储抽象的逻辑路径"),
        sa.Column("filename", sa.String(length=255), nullable=False, comment="原始文件名"),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False, comment="仅作属性存储，不参与身份判定"),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.Column("markdown_text", sa.Text(), nullable=True, comment="清洗后的正文 markdown"),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True, comment="转换完成时间；NULL 即未完成"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_files_report_id", "report_files", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_report_files_report_id", table_name="report_files")
    op.drop_table("report_files")
    op.drop_index("uq_reports_identity_active", table_name="reports")
    op.drop_table("reports")
