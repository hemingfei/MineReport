"""subscriptions / external_refs / connector_runs 表：连接器订阅调度（#19）

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("theme_id", sa.Integer(), nullable=False),
        sa.Column("connector_id", sa.String(length=32), nullable=False, comment="连接器注册表键，如 fxbaogao"),
        sa.Column("created_by", sa.Integer(), nullable=False, comment="订阅人（拉回研报的入库人）"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="订阅开关"),
        sa.Column("interval_hours", sa.Integer(), nullable=False, comment="调度间隔（小时）"),
        sa.Column("auto_download", sa.Boolean(), nullable=False, comment="命中是否自动下载 PDF（扣下载额度）"),
        sa.Column("keywords", sa.JSON(), nullable=True, comment="题材名+同义词之外的额外查询词"),
        sa.Column("orgs", sa.JSON(), nullable=True, comment="机构过滤（可选）"),
        sa.Column("cursor_pubtime", sa.DateTime(timezone=True), nullable=True, comment="pubTime 增量游标（下轮 discover 的 since）"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, comment="本轮已退避重试次数（0-3）"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, comment="连续死信轮数；达 5 触发告警"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_theme_id", "subscriptions", ["theme_id"])
    op.create_index("ix_subscriptions_next_run_at", "subscriptions", ["next_run_at"])

    op.create_table(
        "external_refs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connector_id", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=True, comment="关联的本库研报（duplicate/ingested 非空）"),
        sa.Column("status", sa.String(length=16), nullable=False, comment="seen | ingested | duplicate | fetch_failed"),
        sa.Column("title", sa.String(length=512), nullable=False, comment="剥离 <em> 高亮后的标题"),
        sa.Column("broker", sa.String(length=128), nullable=True, comment="来源侧机构名（orgName）"),
        sa.Column("publish_date", sa.Date(), nullable=False, comment="pubTime 秒级时间戳换算的日期"),
        sa.Column("industry", sa.String(length=64), nullable=True, comment="来源侧行业（与申万口径不一致，仅来源标签）"),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True, comment="命中段落摘录（<em> 已剥）"),
        sa.Column("subscription_id", sa.Integer(), nullable=True, comment="首个发现它的订阅（审计）；订阅删除后置空"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True, comment="最近一次下载失败原因"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_external_refs", "external_refs", ["connector_id", "external_id"], unique=True)
    op.create_index("ix_external_refs_report_id", "external_refs", ["report_id"])

    op.create_table(
        "connector_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connector_id", sa.String(length=32), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True, comment="手动下载等无订阅上下文；订阅删除后置空"),
        sa.Column("event", sa.String(length=16), nullable=False, comment="run | retry | dead_letter | alert | manual_download"),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True, comment="轮级计数：queries/found/new/downloaded/ingested/duplicates 等"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_runs_subscription_id", "connector_runs", ["subscription_id"])


def downgrade() -> None:
    op.drop_index("ix_connector_runs_subscription_id", table_name="connector_runs")
    op.drop_table("connector_runs")
    op.drop_index("ix_external_refs_report_id", table_name="external_refs")
    op.drop_index("uq_external_refs", table_name="external_refs")
    op.drop_table("external_refs")
    op.drop_index("ix_subscriptions_next_run_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_theme_id", table_name="subscriptions")
    op.drop_table("subscriptions")
