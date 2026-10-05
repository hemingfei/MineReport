# PDF 转换引擎用 pymupdf4llm，markitdown 仅保留 docx 一路

转换管道的 PDF 引擎从 markitdown 0.1.8（pdfminer.six 系）换成 pymupdf4llm 1.28.2（PyMuPDF 系）；docx 仍走 markitdown（mammoth）不变。

## 背景

研报详情页正文碎成满屏管道符（用户 2026-10-05 报告），两个原因叠加：

1. **markitdown 的词坐标"表单启发式"误判研报首页**：它对每页按词的 x 坐标聚类猜列，猜出多列就把整页输出成伪表格。研报首页（左侧正文 + 右侧侧边栏 + 多栏抬头）被误判，产出大半为空的碎表、词被拆碎（`Table_EPS` → `T ab le_ EP S`）、东吴 Word 模板隐藏域（`[Table_EPS]` 等）原样漏出。
2. **自家清洗规则删了 GFM 表格分隔行**：`clean_markdown` 曾把 `|---|` 行当噪音丢弃，导致即使引擎输出了规整表格，前端 remark-gfm 也渲染不成表。

## 换代实测（2026-10-05，三份真实研报样本）

东吴安洁点评（问题样本）+ 国元 AI 深度 + 西南农业策略：

- 东吴首页"盈利预测与估值"表完整成表（6 列、无空列、无残留）；侧栏市场数据表成表。
- `[Table_` 隐藏域残留 3 份均为 0（新引擎能读出字面文本，清洗层正则可剥）。
- 耗时 3~11s/份（CPU、无模型权重）；字符产出比 markitdown 更干净（清洗压缩率 4% vs 17~23%）。
- 新增噪音类型：图表文字残渣块（HTML 注释对包裹）与 `<mark>` 高亮标记，清洗层新增对应规则。

## 决策

- **依赖**：`pymupdf4llm>=0.0.27` 入库；markitdown 收窄为 `[docx]` extra（pdfminer/pdfplumber 出依赖树）。
- **worker raw 缓存 key 换代**：`{storage_key}.raw.md` → `{storage_key}.pymupdf.raw.md`，旧产物自然废弃，任务重试不会命中旧引擎输出。
- **存量回填**：`backend/scripts/reconvert_markdown.py`（幂等，逐文件提交，写回 markdown_text + 新 raw 缓存 + search_vector）。
- **清洗规则**：保留 `|---|` 分隔行（教训固化）；新增图表残渣块/隐藏域/`<mark>` 三条规则；页码等原有规则保留。

## Considered Options

- **docling / MinerU**（被否）：质量好但需模型权重，阿里云小服务器部署不动，镜像体积暴涨。
- **VLM 逐页转 markdown（火山方舟）**（留作后备）：质量上限最高，但每份报告增加成本与分钟级延迟，同步 worker 不宜；若未来 pymupdf4llm 对某类版式失效再评估。
- **自研 pdfplumber 拼装**（被否）：维护成本高，重复造 pymupdf 已有的布局/表格启发式。

## 已知代价

- PyMuPDF 为 **AGPL-3.0**：内部自用/私有部署无碍；若未来对外 SaaS 需开源改造或购买商业授权，届时再评估。
- 无框线三线表（表头无底纹）识别仍可能失败，spike 样本未覆盖全部券商模板；quality gate 是逐字节参考测试 + 端到端表格断言，后续遇到坏样本先调 `to_markdown` 参数再考虑引擎级方案。
