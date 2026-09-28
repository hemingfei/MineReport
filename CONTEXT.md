# MineReport

A股研报挖掘系统：汇聚多来源研报，逐篇 AI 分析（总结、题材、标的、作者、券商），再按题材跨文档综合出共性结论与共识标的。

## Language

### 文档与来源

**研报 (Research Report)**:
一份券商发布的 A 股研究报告原始文档（PDF/DOCX 等），是系统的最小入库单元。
_Avoid_: 论文、文档、资料

**来源 (Source)**:
一篇研报的获取渠道：手动上传，或某个研报平台连接器（如发现报告 MCP）。
_Avoid_: 渠道、平台

**连接器 (Connector)**:
对接外部研报平台、按题材定时拉取研报的可插拔数据源抽象。手动上传是内置的特例来源。
_Avoid_: MCP、插件

**订阅 (Subscription)**:
题材与连接器的绑定：调度器按间隔用题材名+同义词作查询词拉新研报，带自动下载开关与增量游标。
_Avoid_: 关注、收藏

**外部引用 (ExternalRef)**:
连接器条目在本库的投影，(connector_id, external_id) 唯一——同一篇外部报告无论被多少关键词/多少轮命中，只入库一次。
_Avoid_: 外链、快照

### 任务与管道

**任务 (Task)**:
API 与 worker 共享的异步队列载体（kind + payload + 状态机 uploaded→在途→done/failed）。kind 的处理器注册、入队口（enqueue）与领取后在途状态单点收敛在 worker 的 HANDLERS 注册表——新增任务类型只注册一项，不在入队点手拼 Task。前端对偶单点在 frontend/src/task.ts：终态副作用（useTaskTerminal）、result 键契约与错误文案（taskResultId/taskFailureText）、按钮状态词（taskStatusLabel）、进行中判定（isTaskSettled）——页面不手拼 result 键、不重写终态分支。
_Avoid_: 作业、job

### 接口与形状

**资源前缀 (Resource Prefix)**:
每个 router 一个资源前缀（/api/reports、/api/subscriptions…），模块内所有端点同根——子资源与动作挂在根下（发现记录列表与下载同为 /api/subscriptions/refs…，额度为 /api/subscriptions/quota），不立裸 /api 路径；管理端点归 /api/admin（如 connector-runs）。新增端点先定归属根，再写路径。
_Avoid_: 裸 /api 前缀、单数资源名（connector 形）

### 分析产物

**分析 (Analysis)**:
LLM 对单篇研报 markdown 的结构化提取结果：总结、题材归属、标的列表、评级等。一篇研报对应一份当前分析，可随 prompt 迭代重跑。
_Avoid_: 摘要、解读

**投影 (Projection)**:
分析结果的可查询关联表（题材关联 report_themes / 标的 analysis_targets / 署名 analysis_authors），行随分析版本链生成、携带 analysis_id。"当前版投影"指 analysis_id 等于研报当前分析指针的行——重跑换版后旧版投影不计入查询口径。
_Avoid_: 中间表、关联表（泛称时）

**题材 (Theme)**:
受控词表中的核心分类维度，是综合分析的入口。由 LLM 提议、人工审核入库，带定义与同义词，演化管理。例：AI 算力、创新药。
_Avoid_: 板块、赛道、标签（与 tag 混用时）

**tag**:
自由打的轻量补充标签，无需审核，用于次要维度聚合。例：深度报告、财报季。
_Avoid_: 题材、关键词

**标的 (Target)**:
研报中提及并给出观点的 A 股上市公司（代码 + 名称），是题材标的池的成员。
_Avoid_: 股票、个股、代码

**券商 (Broker)**:
研报的发布机构。
_Avoid_: 机构、出版社

**综合分析 (Synthesis)**:
对同题材（或同 tag / 勾选多篇）研报集合的 LLM 二次分析：共性结论、共识标的、分歧点，带原文引用回链。按需生成、缓存、可手动刷新。
_Avoid_: 汇总、聚合分析、专题

**题材成员 (ThemeMembership)**:
题材与标的之间的关联实体，携带来源（种子数据 / 分析提取 / 人工维护）、加入日期与活跃标记，使题材标的池成为可运营对象。
_Avoid_: 成分股、板块成员、题材标的（作实体名时）

**分析师 (Author)**:
研报署名的研究人员，以执业证书号为唯一键，缺失时按"姓名+券商"兜底。与券商关联，可回查其覆盖的题材与标的。
_Avoid_: 作者（作实体名时）、写手

**综合分析身份 (Synthesis Key)**:
综合分析的输入集合（题材 / tag / 手选研报集合）的哈希指纹 + 版本号。同指纹不重算，刷新即版本递增。

### 用户与角色

**管理员 (Admin)**:
管理用户邀请与题材词表审核的角色。

**分析师 (Analyst)**:
上传研报、触发分析、参与综合分析的角色。

**读者 (Reader)**:
只浏览研报、分析产物与综合分析的角色。
