"""刷新东财概念快照：产出 app/data/em_concept_snapshot.json（随仓库提交、随版本发布）。

用法（backend/ 目录）：
    uv run python scripts/refresh_em_concept_snapshot.py [--sleep 1.0]

为什么本地跑：东财 push2 对高频请求按 IP 直接断连（数据中心 IP 尤甚，实测连
curl 都被掐、冷却分钟级以上），部署机在线抓取不可靠——题材种子导入任务因此只读
仓库内置快照、零网络（app/themes.py load_em_concept_seeds）。数据更新 =
本地重跑本脚本 → 提交 → 打 tag 发版，服务器升级后导入即得新数据。

与既有快照合并（merge_em_snapshot）：板块集以本次列表为准（官方撤销的移除），
本次抓取成功的板块用新成分，失败的板块沿用上次成分并标 stale——大面积失败时
等冷却后重跑本脚本即可，缺口逐次补齐。~400 板块 × ~1.4s 全程约 10 分钟。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import themes  # noqa: E402

SNAPSHOT_PATH = themes.EM_SNAPSHOT_JSON


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep", type=float, default=1.0, help="逐板块成分股抓取间隔秒数")
    args = parser.parse_args()

    existing = None
    if SNAPSHOT_PATH.exists():
        existing = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        print(f"既有快照：{len(existing.get('concepts', []))} 个板块（generated_at={existing.get('generated_at')}）")

    boards, seeds, failed = themes.fetch_em_concept_seeds(sleep=args.sleep)
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
    if failed:
        print(f"{len(failed)} 个板块本次失败（已沿用上次成分/缺席）：" + "、".join(failed))
        print("东财限流冷却后重跑本脚本可补齐缺口。")


if __name__ == "__main__":
    main()
