"""themes / report_themes / theme_memberships / analysis_authors 表：题材词表（#17）

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "themes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False, comment="展示名"),
        sa.Column("name_norm", sa.String(length=64), nullable=False, comment="归一名（NFKC+去空白），唯一"),
        sa.Column("status", sa.String(length=16), nullable=False, comment="pending | active | merged | retired"),
        sa.Column("definition", sa.Text(), nullable=False, comment="定义（审核时可补）"),
        sa.Column("synonyms", sa.JSON(), nullable=True, comment="同义词组（含合并并入的原名）"),
        sa.Column("source", sa.String(length=16), nullable=False, comment="analysis | manual | seed_em | seed_sw"),
        sa.Column(
            "seed_code", sa.String(length=32), nullable=True,
            comment="种子锚点：东财 BKxxxx / 申万 l2_code；提议类为 NULL",
        ),
        sa.Column("proposed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_id", sa.Integer(), nullable=True, comment="合并去向（status=merged 时非空）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["proposed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["merged_into_id"], ["themes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_themes_name_norm", "themes", ["name_norm"], unique=True)

    op.create_table(
        "report_themes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False, comment="冗余：按题材过滤研报免 join"),
        sa.Column("seq", sa.Integer(), nullable=False, comment="在 result.themes 中的序位（跳过空名后紧凑编号）"),
        sa.Column("theme_id", sa.Integer(), nullable=True, comment="关联的词表条目；NULL=未能关联（理论上不出现）"),
        sa.Column("raw_name", sa.String(length=128), nullable=False, comment="LLM 提取的原始题材词"),
        sa.Column("reason", sa.Text(), nullable=False, comment="一句话归属依据（提取时快照）"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"]),
    )
    op.create_index("ix_report_themes_analysis_id", "report_themes", ["analysis_id"])
    op.create_index("ix_report_themes_report_id", "report_themes", ["report_id"])
    op.create_index("uq_report_themes_seq", "report_themes", ["analysis_id", "seq"], unique=True)

    op.create_table(
        "theme_memberships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("theme_id", sa.Integer(), nullable=False),
        sa.Column("target_code", sa.String(length=6), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, comment="seed | analysis | manual"),
        sa.Column("joined_at", sa.Date(), nullable=False, comment="首次入库日期"),
        sa.Column("is_active", sa.Boolean(), nullable=False, comment="False=已退池"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"]),
        sa.ForeignKeyConstraint(["target_code"], ["targets.code"]),
    )
    op.create_index("ix_theme_memberships_theme_id", "theme_memberships", ["theme_id"])
    op.create_index("uq_theme_memberships", "theme_memberships", ["theme_id", "target_code"], unique=True)

    op.create_table(
        "analysis_authors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False, comment="冗余：按分析师过滤研报免 join"),
        sa.Column("seq", sa.Integer(), nullable=False, comment="在 result.authors 中的序位"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="LLM 提取的署名原文"),
        sa.Column("cert", sa.String(length=64), nullable=True, comment="执业证书号（可缺）"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
    )
    op.create_index("ix_analysis_authors_analysis_id", "analysis_authors", ["analysis_id"])
    op.create_index("ix_analysis_authors_report_id", "analysis_authors", ["report_id"])
    op.create_index("uq_analysis_authors_seq", "analysis_authors", ["analysis_id", "seq"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_analysis_authors_seq", table_name="analysis_authors")
    op.drop_index("ix_analysis_authors_report_id", table_name="analysis_authors")
    op.drop_index("ix_analysis_authors_analysis_id", table_name="analysis_authors")
    op.drop_table("analysis_authors")
    op.drop_index("uq_theme_memberships", table_name="theme_memberships")
    op.drop_index("ix_theme_memberships_theme_id", table_name="theme_memberships")
    op.drop_table("theme_memberships")
    op.drop_index("uq_report_themes_seq", table_name="report_themes")
    op.drop_index("ix_report_themes_report_id", table_name="report_themes")
    op.drop_index("ix_report_themes_analysis_id", table_name="report_themes")
    op.drop_table("report_themes")
    op.drop_index("uq_themes_name_norm", table_name="themes")
    op.drop_table("themes")
