"""users / invitations / sessions 表：邀请制认证与三角色（#12）

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False, comment="登录邮箱"),
        sa.Column("password_hash", sa.String(length=255), nullable=False, comment="scrypt 格式串，不含明文"),
        sa.Column("display_name", sa.String(length=64), nullable=False, comment="显示名"),
        sa.Column("role", sa.String(length=16), nullable=False, comment="admin | analyst | reader"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true"),
                  comment="禁用用户拒绝登录"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False, comment="邀请码，创建时一次性生成"),
        sa.Column("role", sa.String(length=16), nullable=False, comment="被邀请者注册后获得的角色"),
        sa.Column("email", sa.String(length=255), nullable=True,
                  comment="可选：限定被邀请者必须用此邮箱注册"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_invitations_token"),
    )

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False, comment="sha256 hex"),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="固定 TTL，不做滑动续期"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("invitations")
    op.drop_table("users")
