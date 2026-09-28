"""标的主数据导入（#16）：akshare 全量代码+名称 + 申万官网 xls 行业全史 + 可选曾用名回填。

数据源（#4 研究定案，均 2026-09-28 实测）：
- akshare（MIT）`stock_info_a_code_name()`：沪深京全量 ~5.5k 行（code + name），
  内部连三所官网，偶发超时需重试；交易所由代码前缀推导（targets.exchange_of）。
- 申万官网 StockClassifyUse_stock.xls：12925 行个股行业分类全史（含退市股共 5930 只），
  字段 股票代码/计入日期/行业代码(6位标准码)/更新日期。名称经静态码表
  app/data/sw2021_l3.csv join（桥接方法见 app/data/sw2021_bridge.py 与同目录 README）。
  TLS：官网不下发中间证书，用 app/data/swsresearch_ca.pem（DigiCert 中间+根）补全链后照常校验。
- 新浪曾用名 `stock_info_change_name(symbol)`：逐股调用，仅显式开启时回填（约 0.2s/股）。

导入幂等：targets 按主键 upsert（名称/交易所/行业快照覆盖更新，曾用名并集合并）、
行业全史按唯一键补插不重不删。可重复执行（每日/每周全量刷新成本 10s 级）。

网络函数（fetch_*）与纯导入函数（import_master_data）分离：测试只喂行数据，不碰网络。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .errors import MasterDataError
from .models import Target, TargetIndustryHistory
from .targets import exchange_of, normalize_name, valid_code
from .urlguard import assert_safe_http_url

DATA_DIR = Path(__file__).resolve().parent / "data"
SW_CA_BUNDLE = DATA_DIR / "swsresearch_ca.pem"
SW_NAMES_CSV = DATA_DIR / "sw2021_l3.csv"

XLS_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/114.0.0.0"}


@dataclass(frozen=True)
class StockRow:
    code: str
    name: str


@dataclass(frozen=True)
class IndustryRow:
    code: str
    effective_date: dt.date
    industry_code: str
    source_updated_at: dt.datetime | None = None


# ---------- 静态码表（申万 2021 分类标准码 → L1/L2/L3 名称） ----------


def load_sw2021_names() -> dict[str, tuple[str, str, str, str, str, str]]:
    """l3_code(6位) -> (l1_code, l1_name, l2_code, l2_name, l3_code, l3_name)。"""
    out: dict[str, tuple[str, str, str, str, str, str]] = {}
    with SW_NAMES_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["l3_code"]] = (
                row["l1_code"], row["l1_name"],
                row["l2_code"], row["l2_name"],
                row["l3_code"], row["l3_name"],
            )
    return out


# ---------- 纯导入（幂等 upsert，测试注入行数据） ----------


def import_master_data(
    session: OrmSession,
    stocks: list[StockRow],
    industry_rows: list[IndustryRow],
    name_changes: dict[str, list[str]] | None = None,
) -> dict:
    """全量幂等导入。返回计数（落 task.result）。

    xls 覆盖的退市股（不在 stocks 里）也会建行：研报是历史文档，旧代码要能命中；
    名称缺省空串（曾用名回填或后续快照可能补上）。
    """
    names = load_sw2021_names()
    rows: dict[str, Target] = {t.code: t for t in session.scalars(select(Target))}

    def _row(code: str) -> Target:
        row = rows.get(code)
        if row is None:
            row = Target(code=code, name="", name_norm="", exchange="")
            rows[code] = row
            session.add(row)
        return row

    for s in stocks:
        if not valid_code(s.code):
            continue  # 脏行跳过（宁缺毋滥）
        row = _row(s.code)
        row.name = s.name
        row.name_norm = normalize_name(s.name)
        row.exchange = exchange_of(s.code)

    # 行业快照 = 每股 (计入日期, 更新日期) 最大的一行；名称经静态码表 join
    latest: dict[str, IndustryRow] = {}
    for r in sorted(
        (r for r in industry_rows if valid_code(r.code)),
        key=lambda r: (r.effective_date, r.source_updated_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
    ):
        latest[r.code] = r
    for code, r in latest.items():
        row = _row(code)
        row.sw_l1_code, row.sw_l1_name, row.sw_l2_code, row.sw_l2_name, row.sw_l3_code, row.sw_l3_name = names.get(
            r.industry_code, (r.industry_code[:2], None, r.industry_code[:4], None, r.industry_code, None)
        )
        row.sw_effective_date = r.effective_date

    # 全史补插（唯一键 code+effective_date+industry_code，存量跳过）
    existing_hist = {
        (h.code, h.effective_date, h.industry_code)
        for h in session.scalars(select(TargetIndustryHistory))
    }
    hist_added = 0
    seen: set[tuple[str, dt.date, str]] = set()
    for r in industry_rows:
        if not valid_code(r.code):
            continue
        key = (r.code, r.effective_date, r.industry_code)
        if key in existing_hist or key in seen:
            continue
        seen.add(key)
        session.add(TargetIndustryHistory(
            code=r.code,
            effective_date=r.effective_date,
            industry_code=r.industry_code,
            source_updated_at=r.source_updated_at,
        ))
        hist_added += 1

    # 曾用名：并集合并，当前简称不入列
    merged_names = 0
    for code, history in (name_changes or {}).items():
        if not valid_code(code):
            continue
        row = _row(code)
        current = {row.name, *(row.historical_names or [])}
        added = [n for n in history if n and n not in current]
        if added:
            row.historical_names = [*(row.historical_names or []), *added]
            merged_names += len(added)

    session.flush()
    return {
        "targets_total": len(rows),
        "stocks_upserted": len(stocks),
        "industry_snapshot": len(latest),
        "industry_history_added": hist_added,
        "historical_names_merged": merged_names,
    }


# ---------- 网络抓取（生产路径；测试不调用） ----------


def _retry_fetch(desc: str, fn, retries: int = 3, wait: float = 2.0):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise MasterDataError("fetch_failed", f"{desc} 抓取失败（重试 {retries} 次）：{e}") from e
            time.sleep(wait)


def fetch_stocks() -> list[StockRow]:
    """akshare 全量代码+名称（#4 实测 ~5.5k 行，偶发超时需重试）。"""
    import akshare as ak

    def _fetch() -> list[StockRow]:
        df = ak.stock_info_a_code_name()
        return [StockRow(code=str(c), name=str(n)) for c, n in zip(df["code"], df["name"])]

    return _retry_fetch("akshare stock_info_a_code_name（三所官网）", _fetch)


def fetch_industry_history() -> list[IndustryRow]:
    """申万官网 xls 全史（12925 行，含退市股）。URL 字面量 + 出站闸门，便于静态审计。"""
    import pandas as pd
    import requests

    def _fetch() -> list[IndustryRow]:
        # 闸门查 XLS_URL 常量，请求内联同一字面量（静态审计要求；改动须两处同步）
        assert_safe_http_url(XLS_URL)
        r = requests.get(
            "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls",
            headers=_UA, verify=SW_CA_BUNDLE, timeout=60,
        )
        r.raise_for_status()
        df = pd.read_excel(
            io.BytesIO(r.content), dtype={"股票代码": "str", "行业代码": "str"}
        )
        out: list[IndustryRow] = []
        for symbol, start, code, upd in zip(
            df["股票代码"], df["计入日期"], df["行业代码"], df["更新日期"]
        ):
            try:
                eff = start.date() if isinstance(start, dt.datetime) else start
                updated = (
                    upd.to_pydatetime() if isinstance(upd, pd.Timestamp) else
                    upd if isinstance(upd, dt.datetime) else None
                )
            except (AttributeError, ValueError):
                continue
            if not isinstance(eff, dt.date) or not valid_code(str(code)):
                continue
            out.append(IndustryRow(
                code=str(symbol), effective_date=eff,
                industry_code=str(code), source_updated_at=updated,
            ))
        return out

    return _retry_fetch("申万官网 xls", _fetch)


def fetch_name_changes(codes: list[str], limit: int | None = None, sleep: float = 0.2) -> dict[str, list[str]]:
    """新浪曾用名逐股回填（慢路径，显式开启；limit 供小批量验证）。"""
    import akshare as ak

    out: dict[str, list[str]] = {}
    for i, code in enumerate(codes):
        if limit is not None and i >= limit:
            break
        try:
            df = ak.stock_info_change_name(symbol=code)
            names = [str(n) for n in df["name"].tolist()] if not df.empty else []
            if names:
                out[code] = names
        except Exception:  # noqa: BLE001 —— 单股失败不拖垮整批
            continue
        time.sleep(sleep)
    return out


def fetch_all() -> tuple[list[StockRow], list[IndustryRow]]:
    return fetch_stocks(), fetch_industry_history()
