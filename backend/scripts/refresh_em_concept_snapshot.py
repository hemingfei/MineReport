"""刷新东财概念快照：产出 app/data/em_concept_snapshot.json（随仓库提交、随版本发布）。

用法（backend/ 目录）：
    uv run python scripts/refresh_em_concept_snapshot.py [--sleep 1.0] [--inet 4]

为什么本地跑：东财 push2 对高频请求按出口 IP 直接断连（数据中心 IP 尤甚），部署机
在线抓取不可靠——题材种子导入任务因此只读仓库内置快照、零网络（app/themes.py
load_em_concept_seeds）。数据更新 = 本地重跑本脚本 → 提交 → 打 tag 发版，服务器
升级后导入即得新数据。

风控要点（2026-09-29 实测）：push2 掐连接按 (出口 IP, 协议族) 分路计——push2 有
AAAA 记录，系统默认优先 IPv6，IPv6 路径被掐时 requests/curl 全军覆没而 IPv4 仍
畅通。故本脚本默认强制 IPv4（--inet 4，对进程内所有连接生效）；被掐时用
curl -4 / curl -6 分别探测，哪个通走哪个，或换公网出口（如手机热点）。

与既有快照合并（merge_em_snapshot）：板块集以本次列表为准（官方撤销的移除），
本次抓取成功的板块用新成分，失败的板块沿用上次成分并标 stale；连续失败触发熔断
时已抓部分照常落盘，限流冷却后重跑即可补齐。~400 板块 × ~1.2s 全程约 9 分钟。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import themes  # noqa: E402
from app.errors import MasterDataError  # noqa: E402

SNAPSHOT_PATH = themes.EM_SNAPSHOT_JSON


def force_inet(family: int) -> None:
    """进程内强制地址族（akshare→requests→socket 同一进程）：IPv6 路径被东财掐时保命。"""
    orig = socket.getaddrinfo

    def patched(*args, **kwargs):
        return [r for r in orig(*args, **kwargs) if r[0] == family]

    socket.getaddrinfo = patched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep", type=float, default=1.0, help="逐板块成分股抓取间隔秒数")
    parser.add_argument(
        "--inet", choices=["4", "6", "auto"], default="4",
        help="强制地址族：4=仅 IPv4（默认，IPv6 被东财掐时必须）；auto=跟随系统",
    )
    args = parser.parse_args()
    if args.inet != "auto":
        force_inet(socket.AF_INET if args.inet == "4" else socket.AF_INET6)

    existing = None
    if SNAPSHOT_PATH.exists():
        existing = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        print(f"既有快照：{len(existing.get('concepts', []))} 个板块（generated_at={existing.get('generated_at')}）")
    print(f"地址族：{args.inet}，板块间隔 {args.sleep}s")

    def on_progress(boards: list, done: int, total: int, seeds: list, failed: list) -> None:
        """每 25 板块把进度合并落盘一次：长爬中途被杀（限流/断网/关机）不丢已抓部分。"""
        if done % 25 != 0 and done != total:
            return
        payload = themes.merge_em_snapshot(existing, boards[:done], seeds, failed)
        SNAPSHOT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"  进度 {done}/{total}（成功 {len(seeds)}，失败 {len(failed)}）——已落盘", flush=True)

    try:
        boards, seeds, failed, aborted = themes.fetch_em_concept_seeds(
            sleep=args.sleep, progress_cb=on_progress,
        )
    except MasterDataError as e:
        print(f"抓取失败：{e}")
        print(
            "东财 push2 掐连接按 (出口 IP, 协议族) 分路计，且被拒请求会给冷却期续费——"
            "别频繁探测。先静置十几分钟，再分别探测（输出 200 的族可用，之后就别再手动打了）：\n"
            '  curl.exe -4 -s -m 8 -o NUL -w "%{http_code}" "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14"\n'
            '  curl.exe -6 -s -m 8 -o NUL -w "%{http_code}" "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14"\n'
            "两族都断就换公网出口（如手机热点）。重跑时建议 --sleep 5 慢爬。"
        )
        sys.exit(1)

    payload = themes.merge_em_snapshot(existing, boards, seeds, failed)
    stale = sum(1 for c in payload["concepts"] if c.get("stale"))
    SNAPSHOT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    members = sum(len(c.get("members", [])) for c in payload["concepts"])
    print(
        f"快照已写入 {SNAPSHOT_PATH.name}：板块全集 {len(boards)}，本次成功 {len(seeds)}，"
        f"沿用上次成分 {stale}，成分股合计 {members}（噪音板块在导入加载时滤）"
    )
    if aborted:
        print(f"连续 {8} 板块失败疑似触发限流，已熔断收工；已抓部分已落盘，冷却后重跑补缺口。")
    if failed:
        print(f"{len(failed)} 个板块本次失败（已沿用上次成分/缺席）：" + "、".join(failed))
    if aborted or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
