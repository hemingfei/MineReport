"""#18 存量回填：为已有研报重算 search_vector（一次性运维脚本）。

用法（backend/ 目录）：
    DATABASE_URL=postgresql+psycopg://postgres:...@localhost:5432/minereport \
        uv run python scripts/backfill_search.py

迁移 0007 只加列不回填（向量表达式归 app/search.py 单一出处）；全新部署无需跑——
建档/转换/分析挂点从第一天就写向量。幂等：重跑只是重算。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import ResearchReport  # noqa: E402
from app.search import refresh_search_vector  # noqa: E402


def main() -> None:
    with SessionLocal() as session:
        ids = session.scalars(select(ResearchReport.id).order_by(ResearchReport.id)).all()
        for rid in ids:
            refresh_search_vector(session, rid)
        session.commit()
    print(f"backfilled search_vector for {len(ids)} reports")


if __name__ == "__main__":
    main()
