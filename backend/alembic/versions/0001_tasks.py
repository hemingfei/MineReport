"""tasks table: API 与 worker 共享的任务队列载体

Revision ID: 0001
Revises:
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False, comment="任务类型：convert/analyze/synthesize/..."),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="uploaded→converting→analyzing→done|failed（失败可分阶段重试）",
        ),
        sa.Column("payload", sa.JSON(), nullable=True, comment="任务输入参数"),
        sa.Column("result", sa.JSON(), nullable=True, comment="任务输出/错误信息"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次被领取的时间（租约；converting 超时视为遗弃可重新领取）",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_status_kind", "tasks", ["status", "kind"])


def downgrade() -> None:
    op.drop_index("ix_tasks_status_kind", table_name="tasks")
    op.drop_table("tasks")
