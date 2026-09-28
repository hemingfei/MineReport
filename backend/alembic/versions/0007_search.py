"""reports.search_vector：中文全文检索列 + GIN 索引（#18，zhparser/zhcfg）

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28

向量由应用层维护（app/search.py，全 SQLAlchemy constructs）：三源分权重——标题 A /
正文 B（最近转换完成文件，与 /markdown 同语义）/ 总结 C（当前分析版本 result.summary）。
刷新挂点：POST /api/reports 建档、worker 转换 persist、run_analysis 指针移动；
存量行回填用 scripts/backfill_search.py（向量表达式归 app/search.py 单一出处，不在
迁移里复制一份）。

前置依赖（实例级，不在本迁移内）：postgres 镜像 abcfy2/zhparser + 卷首启
init/001_zhparser.sql 建好的 zhcfg 配置（本仓库部署形态即如此）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("search_vector", TSVECTOR(), nullable=True, comment="标题A/正文B/总结C，应用层维护"),
    )
    op.create_index(
        "ix_reports_search_vector", "reports", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_reports_search_vector", table_name="reports")
    op.drop_column("reports", "search_vector")
