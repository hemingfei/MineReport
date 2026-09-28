# app/data 静态数据（随仓库走，勿删）

运行时主数据导入（`app/masterdata.py`）依赖的两个 fixture 与其重建工具。

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
