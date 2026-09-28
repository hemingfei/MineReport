"""一次性工具：重建申万 2021 分类标准码（官网 xls 的 6 位，如 480301）→ 名称 的静态码表。

背景（2026-09-28 实测定案）：
- 官网 xls（StockClassifyUse_stock.xls）= 个股→分类标准码 的权威映射（含全史），但只有码没有名称。
- 申万指数接口（legulegu / 官网 current API）有完整 L1/L2/L3 名称，但用指数码（801xxx/850xxx.SI）。
- 两套编码无公开映射表，唯一可靠的桥 = 个股归属：每个行业指数的成分股集合 ⊢ 对应的 xls
  分类码前缀（L1 取前 2 位、L2 前 4 位、L3 全 6 位，逐指数多数投票 ≥60%）。

产出同目录 sw2021_l3.csv（2026-09-28 跑出 335 行 = 有发布指数的三级行业数；标准共 346 个，
其余 11 个无指数发布，xls 中出现时名称留空、一二级名称仍可由码表前缀推断）。
运行时主数据导入只拉 xls，名称经本 fixture join，不再访问指数接口。
申万修订行业分类（约年更）后重跑本脚本刷新码表。

TLS：www.swsresearch.com 服务端不下发中间证书，普通 verify 会失败；
用同目录 swsresearch_ca.pem（DigiCert 中间+根，取自官方 CA 仓库）补全链后照常校验。

用法：uv run python app/data/sw2021_bridge.py（依赖 akshare 生态：pandas/bs4/requests）
"""

import csv
import io
import time
import warnings
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
CURRENT_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/current/"
CONS_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/"
XLS_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
LEGU_URL = "https://legulegu.com/stockdata/sw-industry-overview"
CA_BUNDLE = HERE / "swsresearch_ca.pem"
OUT = HERE / "sw2021_l3.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/114.0.0.0"}

# 出站白名单：本工具只访问申万官网与乐咕乐股（协议限 https、域名点对点）
ALLOWED_HOSTS = {"www.swsresearch.com", "legulegu.com"}


def safe_get(url: str, **kw) -> requests.Response:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"blocked outbound url: {url}")
    return requests.get(url, **kw)


def fetch_levels() -> dict[str, list[tuple[str, str]]]:
    """指数清单两源合并：官网 current API（L1/L2）+ legulegu 总览页（L1/L2/L3，唯一有 L3 的源）。"""
    out: dict[str, dict[str, str]] = {"L1": {}, "L2": {}, "L3": {}}
    for t, key in (("一级行业", "L1"), ("二级行业", "L2")):
        r = safe_get(
            CURRENT_URL,
            params={"page": 1, "page_size": 400, "indextype": t},
            headers=UA, verify=CA_BUNDLE, timeout=30,
        )
        for x in r.json()["data"]["results"]:
            out[key][x["swindexcode"].split(".")[0]] = x["swindexname"]

    r = safe_get(LEGU_URL, headers=UA, timeout=60)
    soup = BeautifulSoup(r.text, "lxml")
    for key, div_id in (("L1", "level1Items"), ("L2", "level2Items"), ("L3", "level3Items")):
        box = soup.find("div", attrs={"id": div_id})
        codes = box.find_all("div", attrs={"class": "lg-industries-item-chinese-title"})
        names = box.find_all("div", attrs={"class": "lg-industries-item-number"})
        for c, n in zip(codes, names):
            code, name = c.get_text().split(".")[0], n.get_text().split("(")[0]
            out[key].setdefault(code, name)  # 官网结果优先，legulegu 只补缺
    return {k: sorted(v.items()) for k, v in out.items()}


def fetch_current_clf() -> dict[str, str]:
    r = safe_get(XLS_URL, headers=UA, verify=CA_BUNDLE, timeout=60)
    df = pd.read_excel(io.BytesIO(r.content), dtype={"股票代码": "str", "行业代码": "str"})
    df.rename(columns={"股票代码": "symbol", "计入日期": "start_date", "行业代码": "clf", "更新日期": "upd"}, inplace=True)
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["upd"] = pd.to_datetime(df["upd"], errors="coerce")
    df = df.sort_values(["symbol", "start_date", "upd"])
    cur = df.groupby("symbol").tail(1)
    return dict(zip(cur["symbol"], cur["clf"]))


def bridge(level: list[tuple[str, str]], clf: dict[str, str], prefix_len: int) -> tuple[dict[str, str], list]:
    out: dict[str, str] = {}
    fails: list = []
    for code, name in level:
        try:
            r = safe_get(
                CONS_URL,
                params={"swindexcode": code, "page": 1, "page_size": 10000},
                headers=UA, verify=CA_BUNDLE, timeout=30,
            )
            votes: dict[str, int] = {}
            for s in r.json()["data"]["results"]:
                c = clf.get(str(s["stockcode"]))
                if c:
                    votes[c[:prefix_len]] = votes.get(c[:prefix_len], 0) + 1
            if not votes:
                fails.append((code, name, "no_overlap"))
            else:
                top, n = max(votes.items(), key=lambda kv: kv[1])
                if n / sum(votes.values()) >= 0.6:
                    out[top] = name
                else:
                    fails.append((code, name, f"ambiguous:{votes}"))
        except Exception as e:  # noqa: BLE001
            fails.append((code, name, f"error:{type(e).__name__}"))
        time.sleep(0.12)
    return out, fails


def main() -> None:
    levels = fetch_levels()
    print({k: len(v) for k, v in levels.items()})
    clf = fetch_current_clf()
    print("xls stocks:", len(clf))
    l1, f1 = bridge(levels["L1"], clf, 2)
    l2, f2 = bridge(levels["L2"], clf, 4)
    l3, f3 = bridge(levels["L3"], clf, 6)
    print("bridged:", len(l1), len(l2), len(l3), "fails:", len(f1) + len(f2) + len(f3))
    for f in (f1 + f2 + f3)[:15]:
        print("  FAIL", f)

    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name"])
        for c in sorted(l3):
            writer.writerow([c[:2], l1.get(c[:2], ""), c[:4], l2.get(c[:4], ""), c, l3[c]])
    print("fixture rows:", len(l3), "->", OUT)


if __name__ == "__main__":
    main()
