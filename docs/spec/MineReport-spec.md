# MineReport 技术方案（Spec）

> 汇自 wayfinder 地图（issue #1）的全部已定决策：#2 markitdown 研究、#3 fxbaogao 研究、#4 主数据研究、#5 凭据、#6 分析链路 spike、#7 数据模型与 API grilling、#8 连接器 grilling。词汇表见 `CONTEXT.md`；研报身份决策见 `docs/adr/0001`。

## Problem Statement

A 股研报散落在各券商 App、研报平台和个人下载文件夹里：单篇看完了，跨报告的共性观点（几家券商同时在推什么题材、什么标的）却要靠人脑拼。手动上传的 PDF 打不开表格、看不了正文结构，也没人帮忙总结。研报越攒越多，挖掘效率越低。

## Solution

一个自部署的多用户 Web 系统：上传或定时订阅拉取研报 → 自动转 markdown（省流量、阅读友好）→ LLM 逐篇提取结构化分析（总结、题材、标的、评级、作者、券商）→ 题材受控词表聚合 → 按需生成同题材跨报告综合分析（共性结论、共识标的、分歧点）。三角色（管理员/分析师/读者）邀请制使用。

## User Stories

### 上传与转换

1. 作为分析师，我想拖拽上传 PDF/DOCX 研报，所以我不需要改变现有收集习惯。
2. 作为分析师，我想上传后立即得到反馈（任务进行中），所以我知道系统在处理而不是卡死。
3. 作为分析师，我想重复上传同一篇研报时系统自动并入既有条目，所以库不会越来越乱。
4. 作为读者，我想在浏览器里直接阅读排版友好的 markdown 正文，所以不用下载几十 MB 的 PDF 也省流量。
5. 作为分析师，我想上传扫描版或加密 PDF 时得到明确报错，所以不会出现"转换成功但内容为空"的静默事故。

### 单篇分析

6. 作为分析师，我想每篇研报自动生成结构化分析（券商、作者、日期、评级、总结、题材、标的、风险提示），所以我不用逐篇人工摘录。
7. 作为分析师，我想标的提取自动规范成 6 位股票代码并校验，所以 LLM 凭记忆补的代码不会污染标的池。
8. 作为分析师，我想无法自动确认的标的名进入人工确认队列，所以模糊匹配的边界情况有人工兜底。
9. 作为分析师，我想 prompt 迭代后可以批量重跑历史研报的分析，所以分析质量可以随模型升级持续改善。
10. 作为分析师，我想看到每个分析版本的审计信息（prompt 版本、模型、token 用量、耗时），所以我能评估重跑的成本与收益。
11. 作为分析师，我想自由给研报打 tag（如"深度报告""财报季"），所以次要维度的聚合不被题材词表束缚。

### 题材与标的池

12. 作为管理员，我想 LLM 提议的新题材进入待审状态而不是直接入库，所以题材词表不会碎片化。
13. 作为管理员，我想合并题材时同义词自动关联，所以"AI算力"和"算力"不会各立门户。
14. 作为分析师，我想浏览某个题材下的全部研报和标的池，所以我快速掌握该题材的全貌。
15. 作为管理员，我想初始题材词表用东财概念板块+申万二级骨架一键导入，所以系统第一天就可用而不是空库。
16. 作为分析师，我想查询某分析师覆盖过哪些题材与标的，所以我能追踪明星分析师的观点迁移。

### 综合分析

17. 作为分析师，我想选定一个题材后一键生成跨报告综合分析（共性结论、共识标的、分歧点），所以不用自己读十几篇对比。
18. 作为分析师，我想综合分析的结论带原文引用回链，所以每个共性判断都能溯源验证。
19. 作为分析师，我想综合分析结果缓存、输入没变不重算，所以不会重复烧 token。
20. 作为分析师，我想手动刷新综合分析以纳入最新研报，所以结论不会过期。

### 订阅与连接器

21. 作为分析师，我想订阅一个题材让系统定时从发现报告拉新研报，所以我不用手动盯平台。
22. 作为分析师，我想订阅自动用题材名+同义词做查询，所以同义词词表的治理红利直接变成召回率。
23. 作为管理员，我想按订阅开关自动下载 PDF（默认开），所以下载额度消耗可控。
24. 作为管理员，我想看到连接器运行日志（成功/失败/死信/额度），所以外部平台故障我能第一时间知道。
25. 作为分析师，我想手动单篇下载 PDF 时先看到额度提示，所以不会无意中耗尽 VIP 权益。

### 权限与管理

26. 作为管理员，我想邀请用户并分配角色，所以库的访问范围可控。
27. 作为管理员，我想默认禁止读者下载原始 PDF（可配置放开），所以版权风险可控。
28. 作为管理员，我想软删除的研报 30 天内可恢复，所以误删不是灾难。
29. 作为任何用户，我想按关键词全文搜索研报（标题/正文/总结）且中文分词质量好，所以搜"算力"能召回"AI 算力"相关报告。

## Implementation Decisions

### 架构总览

- **后端**：Python FastAPI（单体 API 服务）+ **独立分析 worker 容器**（转换/分析/订阅调度都在 worker 里跑，与 API 分离重启互不影响，预留横向扩展）。两者通过共享 PostgreSQL 任务表通信，不引入消息队列。
- **前端**：React SPA（Vite + TypeScript），markdown 渲染阅读。
- **数据库**：PostgreSQL + zhparser 中文分词扩展（全文检索），Docker 镜像需自带 zhparser。
- **存储**：原始文件存本地卷，存储层抽象接口（`put/get/delete`），可后换 S3/MinIO。
- **部署**：Docker Compose 单机（api / worker / postgres / caddy 或 nginx 反代）。规格假设：≤50 用户、≤1 万篇研报、日增 ≤200 篇、单篇分析 1~2 分钟。
- **LLM**：OpenAI 兼容 API，`base_url`/`api_key`/`model` 全部环境变量配置。所有凭据（LLM、fxbaogao）只从环境变量读取，源码、示例、测试不写入可用凭据字面量。

### 数据模型（实体与关键约束）

- **ResearchReport（研报）**：业务唯一键 = (标题归一, 券商, 发布日期) **纯组合键**（ADR-0001，含已知误合并风险的接受与缓解）；多来源文件挂同一研报下。`file_sha256` 仅存属性。
- **Analysis（分析）**：版本链（version++），研报指向当前版本；每版记录 prompt_version、model、prompt/completion tokens、耗时。结构（spike 验证过的 schema，来自 #6 原型）：

```json
{
  "broker": "str", "authors": [{"name": "str", "cert": "str|null"}],
  "publish_date": "YYYY-MM-DD", "report_type": "点评|深度|策略|其他",
  "title": "str", "summary": "str(≤200字)",
  "themes": [{"name": "str", "reason": "str"}],
  "targets": [{"code": "6位|null", "name": "str", "stance": "推荐|提及|回避",
               "view": "str", "has_forecast": "bool",
               "code_source": "text|inferred|manually_confirmed"}],
  "rating": {"action": "买入|增持|中性|减持|卖出|null", "maintained": "bool|null"},
  "risk_notes": "str"
}
```

- **Theme（题材）**：受控词表，状态机 `待审 → 在册 → 合并/停用`；带定义与同义词组。
- **ThemeMembership（题材成员）**：题材↔标的中间实体，`source ∈ {seed, analysis, manual}`，带加入日期与活跃（退池）标记。
- **Target（标的）**：主数据实体（akshare 全量 + 申万行业快照含生效日期 + 历史名称）。规范化瀑布：主数据贴表→名称归一化（剥 ST/空格/全角/公司后缀）→精确匹配→模糊匹配（ratio≥0.85 唯一命中）→人工确认队列。`code_source=inferred` 强制落队列。
- **Author（分析师）**：执业证书号唯一键，缺失按"姓名+券商"兜底。
- **Synthesis（综合分析）**：身份 = 输入集合（一期仅 theme_id）哈希指纹 + 版本号；记录输入研报 id 集合作证据边界；缓存，手动刷新即 version++。
- **Connector/Subscription/ExternalRef**：连接器注册表；`Subscription(theme_id, connector_id, keywords, orgs?, schedule, auto_download)`；`ExternalRef(connector_id, external_id)` 唯一防重复入库。
- **Task（任务）**：状态机 `uploaded → converting → analyzing → done | failed`，失败可分阶段重试；API 与 worker 共享的任务队列载体。
- **Tag**：自由标签，研报↔tag 多对多。
- **User**：邀请制，角色 admin/analyst/reader。

### 转换管道（来自 #2 研究的实测结论）

1. 前置：pypdf 检测加密（空密码解密重写）；每页字符数探测识别扫描版 → 路由 OCR 或明确拒收（markitdown 对扫描版静默返回空串，必须设字符数闸门）。
2. 转换：markitdown 出正文流。
3. 清洗：剥离页眉页脚/免责声明/页码/孤儿图注/孤立数字行（#6 spike 的正则清洗压掉 17~23% 字符且主题更收敛）。
4. 校验：输出字符数闸门。
5. 表格不依赖 markitdown 的启发式（实测碎片化严重）；结构化数据靠 LLM+schema 从清洗后 markdown 抽取。

### 分析管道（来自 #6 spike 的 18 组实测）

- **策略**：正则清洗 + **整篇单次调用**（成本/速度/质量全面胜过分块与分段定位：深度报告 1 次调用、~14k prompt + 3k completion tokens、52~103s）。分块仅留作 >50k 字符超长文档兜底。
- **publish_date**：正则锚定首页优先，LLM 兜底。
- **标的校验**：LLM 提取的代码若文本中不存在 → `code_source=inferred` → 人工确认队列（spike 证实 LLM 会用世界知识回填代码，恰好对但不可信）。
- **prompt 版本化**：prompt 模板入库带版本号，重跑时记录。

### API 面（REST，约 18 端点）

- 上传 `POST /api/reports`（multipart，202+task）；列表 `GET /api/reports`（过滤：题材/tag/券商/日期/标的/搜索词，分页）；详情/正文/文件 `GET /api/reports/{id}`、`/markdown`、`/file`
- 分析 `POST /api/reports/{id}/reanalyze`、`GET /api/reports/{id}/analyses`（版本历史）；tag 增删
- 题材 `GET/POST /api/themes`（提议）、`PATCH /api/themes/{id}`（审核/合并/停用，admin）、`GET /api/themes/{id}/reports|members`
- 综合 `POST /api/syntheses`（一期仅 theme_id 输入，202+task）、`GET /api/syntheses/{id}`
- 管理 `POST /api/admin/invitations`、`GET /api/tasks/{id}`（轮询）、`GET /api/targets`（主数据搜索/人工确认队列）
- **异步任务一律轮询**（202 + task_id，前端 3~5s 轮询），不用 SSE/WebSocket。

### 权限矩阵

| 资源 | 管理员 | 分析师 | 读者 |
|---|---|---|---|
| 用户邀请/禁用 | ✓ | | |
| 题材词表审核/合并/停用 | ✓ | 提议 | 查看 |
| 研报上传/删除（软删，30 天恢复） | ✓ | ✓（仅自己的） | |
| 触发/重跑分析、综合分析 | ✓ | ✓ | |
| 浏览/搜索/阅读、分析版本回看 | ✓ | ✓ | ✓ |
| 原始文件下载 | ✓ | ✓ | ✗（`ALLOW_READER_DOWNLOAD` 可放开） |
| 订阅管理 | ✓ | ✓ | |

### 连接器（来自 #3 研究与 #8 定案）

- **pull 契约**：连接器实现 `discover(query, since) -> [ReportRef]` 与 `fetch(ref) -> File`；调度器统一做 去重（组合键 + ExternalRef）→ 下载 → 入库回调（接既有任务管道）。
- **订阅**：绑题材，题材名+同义词展开为查询词（fxbaogao 无行业 facet，关键词是唯一过滤）；默认间隔 6h，pubTime 时间窗增量，错峰抖动。
- **调度**：APScheduler 进 worker 容器；失败指数退避重试 3 次（1m/5m/25m）→ 死信入连接器日志 → 连续 5 轮失败告警；连接器全局限速 1 req/s。
- **fxbaogao 首接**：REST 直连（httpx + Bearer，不跑 MCP 协议）；`search` 元数据字段实测为 reportId/title/orgName/industryName/页数/pubTime/命中段落（**无作者、无评级**——靠 LLM 从 PDF 抽）；PDF 下载 URL 带时效，拿到即下载，只存文件；`auto_download` 默认开，管理后台按订阅可改，手动下载扣额度前提示。
- **安全约束（全部出站请求）**：仅 http/https；发请求前校验 host，拒绝 localhost、环回、私有、保留地址。httpx 客户端统一挂 host 校验钩子，api.fxbaogao.com 与 dr.fxbaogao.com 经校验后放行。

### 冷启动

- 主数据：akshare（MIT）全量代码+名称（~5.5k 行）+ 申万官网分类表快照 + 东财概念板块成分股。
- 题材种子：东财概念（~460，滤行情类噪音）+ 申万二级（134）骨架；成分股直接作为 ThemeMembership(source=seed) 入库。

## Testing Decisions

- **测试哲学**：只测外部行为，不测实现细节。仓库尚无测试先例，本 spec 建立基线。
- **主缝合口（最高层，一个）**：**HTTP API 层**（FastAPI TestClient + 真实 Postgres 测试库）。绝大多数用户故事（上传→任务→分析落库→列表过滤→权限拒绝）都能从这个缝合口端到端验证，包括轮询语义与权限矩阵。
- **次缝合口（两个，mock 外部依赖）**：
  1. **LLM 分析管道**：mock OpenAI 兼容端点返回预录响应（spike 的 18 份真实输出 JSON 作为测试夹具），验证清洗、publish_date 正则锚定、code_source 瀑布与人工确认队列的路由逻辑——LLM 本身的质量不测（spike 已人工验收）。
  2. **连接器调度**：mock fxbaogao REST（httpx MockTransport），验证订阅查询展开、ExternalRef 去重、退避重试/死信、host 校验钩子（含对 localhost/私有地址的拒绝用例——安全约束的回归测试）。
- **夹具来源**：`research/markitdown-samples/`（3 份真实 PDF + md）与 `research/analysis-spike/out/`（18 份真实 LLM 输出）转为测试资产。

## Out of Scope

- 移动端 App / 小程序
- 行情数据接入、量化回测
- 多租户 SaaS 化（计费、组织隔离）
- tag / 手选研报集合的综合分析输入（二期）
- fxbaogao 之外其他平台连接器的预研（契约已抽象，按需实现）
- OCR 管道（扫描版研报明确拒收而非路由 OCR）

## Further Notes

- 前端 markdown 阅读体验细节（目录、图表占位、字号）由实现会话自决。
- 综合分析缓存失效：一期采用"输入指纹未变即命中缓存 + 手动刷新强制 version++"，题材词表变更不自动失效（刷新按钮兜底）。
- 全部凭据（`LLM_*`、`FXBAOGAO_API_KEY`）只从环境变量或密钥服务读取；`.env` 已在 `.git/info/exclude` 排除，源码/示例/测试不写入可用凭据字面量。
