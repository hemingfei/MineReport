# app/data 静态数据（随仓库走，勿删）

运行时主数据导入（`app/masterdata.py`）依赖的两个 fixture 与其重建工具，
以及题材种子导入（`app/themes.py`）依赖的东财概念快照。

## em_concept_snapshot.json

题材种子导入的东财半边数据源：概念板块全集 + 各板块成分股快照。
`worker` 的 `import_themes` 任务只读本文件、零网络——东财 push2 接口对高频请求
按 IP 直接断连（实测连 curl 都被掐、冷却分钟级以上），部署机（数据中心 IP）在线
抓取不可靠。噪音板块（行情/风格/资金面类，见 `themes._NOISE_*`）在导入加载时滤，
规则演化无需重抓快照。

**当前来源（2026-10-01）**：新浪财经概念板块（vip.stock.finance.sina.com.cn
getHQNodeData，175 节点、成分翻页到空页自校验）——东财 push2 把家宽/数据中心 IP
全部拉黑且封禁长效，境外 IP 一律 502 地域拒，首份快照改由新浪替代产出（文件名与
格式不变，`code` 字段为新浪 gn_* 节点 id，加载与噪音过滤逻辑不受来源影响）。
东财风控解除后重跑下方刷新脚本即会整体覆盖回东财口径（板块集以本次列表为准）。

刷新（东财口径）：backend/ 下 `uv run python scripts/refresh_em_concept_snapshot.py`
（~400 板块 × ~1.4s 全程约 10 分钟），与既有快照合并——板块集以本次列表为准，
失败的板块沿用上次成分并标 stale，限流冷却后重跑即可补齐。产出提交进仓库，
随版本发布到服务器。

## sw2021_l3.csv

申万 2021 行业分类标准码（6 位，前 2 一级 / 前 4 二级 / 全 6 三级，如 480301=股份制银行Ⅲ）
→ 三级名称及一二级名称码表，335 行。官网 xls（个股→分类码权威映射）不含名称，
指数接口（含名称）用的是另一套指数码（801xxx.SI），两套编码无公开对照——
`sw2021_bridge.py` 经"行业指数成分股 ⊢ xls 分类码前缀"投票桥接产出（2026-09-28 首跑，
L1=31 / L2=131 / L3=335 全部命中、零失败；抽查 48=银行、340501=白酒Ⅲ 正确）。

申万修订行业分类（约年更）后重跑 `sw2021_bridge.py` 刷新。

## swsresearch_ca.pem

www.swsresearch.com 服务端不下发中间证书（leaf 直接由 GeoTrust G2 TLS CN RSA4096
SHA256 2022 CA1 签发，根为 DigiCert Global Root G2），标准证书链校验必然失败。
本 bundle = 中间证书（取自 DigiCert 官方 CA 仓库 cacerts.digicert.cn）+ 根证书，
供 `requests.get(..., verify=本文件)` 补全链后照常执行完整校验——不是跳过校验。
官网换发证书（换 CA）后需重建本文件。
