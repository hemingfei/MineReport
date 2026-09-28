"""syntheses 表：题材跨报告综合分析（#20）

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "syntheses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("theme_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号，从 1 递增（手动刷新即 ++）"),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False,
                  comment="输入研报 id 集合的 sha256（缓存命中判据）"),
        sa.Column("report_ids", sa.JSON(), nullable=False,
                  comment="输入研报 id 集合（证据边界，序位 = 引用编号）"),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False, comment="共性结论/共识标的/分歧点 + 输入规模元信息"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_syntheses_theme_id", "syntheses", ["theme_id"])
    op.create_index("uq_syntheses_theme_version", "syntheses", ["theme_id", "version"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_syntheses_theme_version", table_name="syntheses")
    op.drop_index("ix_syntheses_theme_id", table_name="syntheses")
    op.drop_table("syntheses")
