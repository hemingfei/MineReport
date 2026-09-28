"""analyses / prompt_templates / tags / report_tags 表：分析管道（#15）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False, comment="如 v1"),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("user_template", sa.Text(), nullable=False, comment="{markdown} 占位"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )

    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("report_file_id", sa.Integer(), nullable=False,
                  comment="本版分析的输入文件（多来源文件后到优先）"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号，从 1 递增"),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, comment="LLM 调用总耗时（含分块兜底的多趟）"),
        sa.Column("result", sa.JSON(), nullable=False, comment="spec schema 分析结果（归一后）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["report_file_id"], ["report_files.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analyses_report_id", "analyses", ["report_id"])
    op.create_index("uq_analyses_report_version", "analyses", ["report_id", "version"], unique=True)

    op.add_column(
        "reports",
        sa.Column("current_analysis_id", sa.Integer(), nullable=True,
                  comment="当前生效的分析版本（版本链头指针）"),
    )
    op.create_foreign_key(
        "fk_reports_current_analysis", "reports", "analyses", ["current_analysis_id"], ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False, comment="归一后的展示名"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "report_tags",
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("report_id", "tag_id"),
    )


def downgrade() -> None:
    op.drop_table("report_tags")
    op.drop_table("tags")
    op.drop_constraint("fk_reports_current_analysis", "reports", type_="foreignkey")
    op.drop_column("reports", "current_analysis_id")
    op.drop_index("uq_analyses_report_version", table_name="analyses")
    op.drop_index("ix_analyses_report_id", table_name="analyses")
    op.drop_table("analyses")
    op.drop_table("prompt_templates")
