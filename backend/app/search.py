"""#18 中文全文搜索：search_vector 的组装与刷新（应用层维护）。

三源分权重：标题 A / 正文 B（最近转换完成文件，与 /markdown 同语义）/ 总结 C（当前
分析版本 result.summary）。zhparser 扩展与 zhcfg 配置是实例级的，由 postgres/init/
001_zhparser.sql 卷首启安装；本模块只组装表达式，全部经 SQLAlchemy constructs 下发。

刷新挂点（漏挂 = 索引陈旧，新增研报写入路径时须同步补挂）：
- POST /api/reports 建档后（标题）
- worker 转换 persist 后（正文）
- run_analysis 版本链头指针移动后（总结）
存量回填：scripts/backfill_search.py。

查询侧（routers/reports.py）用 plainto_tsquery('zhcfg', q)：索引与查询同一分词器，
"算力"必命中"AI 算力"（两侧切出相同词元序列）。分词器名与权重字面必须内联渲染
（literal_column）——regconfig/"char" 无 text 参数的重载，绑定参数会解析失败。
"""

from __future__ import annotations

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.orm import Session as OrmSession

from .models import Analysis, ReportFile, ResearchReport

_REGCONFIG = literal_column("'zhcfg'")
_EMPTY = literal_column("''")
_WEIGHTS = {"A": literal_column("'A'"), "B": literal_column("'B'"), "C": literal_column("'C'")}

# tsvector 序列化上限 1MB，研报正文全量入词必然超限；截断仅影响极端长文的尾部召回
_BODY_MAX_CHARS = 200_000


def _tsv(segment, weight: str):
    return func.setweight(
        func.to_tsvector(_REGCONFIG, func.coalesce(segment, _EMPTY)),
        _WEIGHTS[weight],
    )


def _latest_body(report_id: int):
    """正文取最近转换完成文件（converted_at 优先、id 破并列，与 latest_converted_file
    同语义——改"最新"规则须两处同步）。"""
    return (
        select(ReportFile.markdown_text)
        .where(ReportFile.report_id == report_id, ReportFile.converted_at.is_not(None))
        .order_by(ReportFile.converted_at.desc(), ReportFile.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def refresh_search_vector(session: OrmSession, report_id: int) -> None:
    """重算单篇研报的 search_vector（幂等；读库内已 flush 的三源，与调用方同事务）。"""
    summary = (
        select(Analysis.result["summary"].as_string())
        .where(
            Analysis.id
            == select(ResearchReport.current_analysis_id)
            .where(ResearchReport.id == report_id)
            .scalar_subquery()
        )
        .scalar_subquery()
    )
    session.execute(
        update(ResearchReport)
        .where(ResearchReport.id == report_id)
        .values(
            search_vector=_tsv(ResearchReport.title, "A")
            .op("||")(_tsv(func.left(_latest_body(report_id), _BODY_MAX_CHARS), "B"))
            .op("||")(_tsv(summary, "C"))
        )
    )


def matches(q: str):
    """查询侧谓词：search_vector @@ plainto_tsquery('zhcfg', q)（多词按分词交集）。
    'zhcfg' 字面必须与写入侧同源内联——本模块是分词器名的单一出处。"""
    return ResearchReport.search_vector.op("@@")(
        func.plainto_tsquery(_REGCONFIG, q)
    )
