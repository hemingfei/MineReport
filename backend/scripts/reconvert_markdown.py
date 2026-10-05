"""引擎换代回填（ADR-0002）：用 pymupdf4llm 重转存量研报正文（一次性运维脚本）。

用法（backend/ 目录）：
    DATABASE_URL=postgresql+psycopg://postgres:...@localhost:5432/minereport \
    STORAGE_ROOT=./data/files \
        uv run python scripts/reconvert_markdown.py

- 仅处理 PDF（docx 引擎未换代，原产物不变）；软删除研报跳过。
- 写回 file.markdown_text + raw 缓存（.pymupdf.raw.md，与 worker 同 key）+ 重算 search_vector。
- 不重跑 LLM 分析（需要时对单份报告调 POST /api/reports/{id}/reanalyze）。
- 幂等：重跑即重转。输出低于 markdown_min_chars 闸门的文件记 skip 不中断。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.conversion import clean_markdown, convert_to_markdown  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import ReportFile, ResearchReport  # noqa: E402
from app.search import refresh_search_vector  # noqa: E402
from app.storage import get_storage  # noqa: E402


def main() -> None:
    storage = get_storage()
    s = get_settings()
    converted = skipped = failed = 0
    with session_scope() as session:
        rows = session.execute(
            select(ReportFile, ResearchReport)
            .join(ResearchReport, ReportFile.report_id == ResearchReport.id)
            .where(ResearchReport.deleted_at.is_(None))
            .order_by(ReportFile.id)
        ).all()
        for file, _report in rows:
            if not file.filename.lower().endswith(".pdf"):
                skipped += 1
                continue
            try:
                decrypted_key = f"{file.storage_key}.decrypted.pdf"
                src_key = decrypted_key if storage.exists(decrypted_key) else file.storage_key
                raw = convert_to_markdown(storage.get(src_key), file.filename)
                if len(raw.strip()) < s.markdown_min_chars:
                    print(f"skip file #{file.id} {file.filename}: 输出仅 {len(raw.strip())} 字符")
                    skipped += 1
                    continue
                storage.put(f"{file.storage_key}.pymupdf.raw.md", raw.encode("utf-8"))
                file.markdown_text = clean_markdown(raw)
                refresh_search_vector(session, file.report_id)
                session.commit()  # 逐文件提交：单文件失败不丢已转成果
                converted += 1
                print(f"reconverted file #{file.id} {file.filename}: {len(raw)} -> {len(file.markdown_text)} chars")
            except Exception as e:  # noqa: BLE001 单文件失败不中断回填
                failed += 1
                print(f"FAIL file #{file.id} {file.filename}: {type(e).__name__}: {e}")
    print(f"done: converted={converted} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
