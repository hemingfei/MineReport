"""targets / target_industry_history / target_matches / analysis_targets 表：标的主数据（#16）

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "targets",
        sa.Column("code", sa.String(length=6), nullable=False, comment="6 位股票代码"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="当前简称"),
        sa.Column("name_norm", sa.String(length=64), nullable=False, comment="归一简称（剥 ST/空格/全角/后缀）"),
        sa.Column("exchange", sa.String(length=8), nullable=False, comment="SH | SZ | BJ（代码前缀推导）"),
        sa.Column("sw_l1_code", sa.String(length=2), nullable=True),
        sa.Column("sw_l1_name", sa.String(length=64), nullable=True),
        sa.Column("sw_l2_code", sa.String(length=4), nullable=True),
        sa.Column("sw_l2_name", sa.String(length=64), nullable=True),
        sa.Column("sw_l3_code", sa.String(length=6), nullable=True),
        sa.Column("sw_l3_name", sa.String(length=64), nullable=True),
        sa.Column("sw_effective_date", sa.Date(), nullable=True, comment="当前行业分类的计入日期（快照生效日期）"),
        sa.Column("historical_names", sa.JSON(), nullable=True, comment="曾用名列表（akshare 新浪曾用名回填）"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index("ix_targets_name_norm", "targets", ["name_norm"])

    op.create_table(
        "target_industry_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=6), nullable=False, comment="6 位股票代码"),
        sa.Column("effective_date", sa.Date(), nullable=False, comment="计入日期"),
        sa.Column("industry_code", sa.String(length=6), nullable=False, comment="申万 2021 分类标准码"),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True, comment="官网 xls 的更新日期"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_industry_history", "target_industry_history",
        ["code", "effective_date", "industry_code"], unique=True,
    )

    op.create_table(
        "target_matches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("raw_name", sa.String(length=128), nullable=False, comment="LLM 提取的原始名称串"),
        sa.Column("raw_code", sa.String(length=6), nullable=True, comment="LLM 给的 6 位码（可能凭记忆）"),
        sa.Column(
            "reason", sa.String(length=32), nullable=False,
            comment="入队原因：inferred_code | code_not_in_master | multi_candidate | no_hit",
        ),
        sa.Column("candidates", sa.JSON(), nullable=True, comment="瀑布建议候选"),
        sa.Column("status", sa.String(length=16), nullable=False, comment="pending | confirmed | dismissed | superseded"),
        sa.Column("resolved_code", sa.String(length=6), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_target_matches_report_id", "target_matches", ["report_id"])
    op.create_index("ix_target_matches_analysis_id", "target_matches", ["analysis_id"])

    op.create_table(
        "analysis_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False, comment="冗余：按标的过滤研报免 join"),
        sa.Column("seq", sa.Integer(), nullable=False, comment="在 result.targets 中的序位"),
        sa.Column("target_code", sa.String(length=6), nullable=True, comment="落成的规范代码；NULL=仍在队列"),
        sa.Column("match_id", sa.Integer(), nullable=True, comment="对应队列条目（自动落成时为 NULL）"),
        sa.Column("raw_name", sa.String(length=128), nullable=False),
        sa.Column("raw_code", sa.String(length=6), nullable=True),
        sa.Column("stance", sa.String(length=8), nullable=False, comment="推荐|提及|回避"),
        sa.Column("view", sa.Text(), nullable=False),
        sa.Column("has_forecast", sa.Boolean(), nullable=False),
        sa.Column("code_source", sa.String(length=24), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["target_code"], ["targets.code"]),
        sa.ForeignKeyConstraint(["match_id"], ["target_matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_targets_analysis_id", "analysis_targets", ["analysis_id"])
    op.create_index("ix_analysis_targets_report_id", "analysis_targets", ["report_id"])
    op.create_index("uq_analysis_targets_seq", "analysis_targets", ["analysis_id", "seq"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_analysis_targets_seq", table_name="analysis_targets")
    op.drop_index("ix_analysis_targets_report_id", table_name="analysis_targets")
    op.drop_index("ix_analysis_targets_analysis_id", table_name="analysis_targets")
    op.drop_table("analysis_targets")
    op.drop_index("ix_target_matches_analysis_id", table_name="target_matches")
    op.drop_index("ix_target_matches_report_id", table_name="target_matches")
    op.drop_table("target_matches")
    op.drop_index("uq_industry_history", table_name="target_industry_history")
    op.drop_table("target_industry_history")
    op.drop_index("ix_targets_name_norm", table_name="targets")
    op.drop_table("targets")
