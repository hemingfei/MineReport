# Design: 冷静的金融编辑部（calm financial-editorial）

> 本文件是 MineReport 前端的设计契约：任何触及呈现层的改动先对照此处。
> 方向一句话论证：研报挖掘是给分析师用的高密度信息工具——信任来自「冷静、精确、可扫读」，
> 故取 industrial mono 的密度与对齐纪律 + Swiss editorial 的字阶与留白，装饰归零。

参考方法论：[ui-taste](https://github.com/ram-devv1/ui-taste)（决策协议 + 渲染批判循环）。

## Palette（≤3 个活跃色相 + 语义四色）

- Dominant ≈60%：冷灰面板 `--bg #f4f5f8`（暗色 `#111420`）——底色永远退后
- Neutral ramp ≈30%：`--surface #ffffff / --surface-2 #f1f3f7 / --border #e5e8ee`（同族冷灰，禁止纯 `#000`/`#fff` 文字面）
- Accent ≈10%：单一深靛蓝 `--primary #2d54b0`（暗色提亮为 `#8ca6f7`）——一屏只此一个强调声部
- 语义四色（ok/warn/danger）独立于品牌色相，饱和度同样受纪律约束
- 中性色已做冷调偏染（3–8%），设计意图是「用户以为是白，设计师看得出刻意」

## Type

- 字体：系统 CJK 栈（`system-ui / PingFang SC / Microsoft YaHei / Noto Sans CJK SC`）——
  显式决策：中文 webfont 数 MB 代价与数据工具首屏不成比例，身份感改由字阶与字重纪律承担
- 阶梯比例 1.25，锚点：UI 14–15px · 正文 16px · 页题/详情题 25px（移动端 20–21px）
- 行高反比：大字收紧（display 1.35）、小字放开（正文 1.65，研报正文 1.9）
- 数据场合一律 `font-variant-numeric: tabular-nums`；mono 只用于代码/邀请码/日志等语义本身等宽处
- 长说明段落限宽 `.measure`（≈620px，CJK 40 字/行）

## Form

- 圆角：8（控件）/ 12（容器）/ 全圆（徽章、分段控件）——不混用其他值
- 间距 8pt 网格（4px 半步用于密集表格微调）
- 阴影染冷蓝底色不纯黑；分区优先级 = 留白 → 底色阶差 → 阴影 → 1px 描边（border 是最后手段）。
  已落地口径：**容器（card/article/toc/骨架行）一律无描边**，border 槽位 transparent 保留给状态信号
  （task-done / task-failed / created-invitation 覆写颜色）；描边只属于控件（输入/按钮/分段）与语义态
- 明暗双主题：暗色以亮阶表 elevation（#111420→#181c28→#212633），强调色降饱和提亮

## Motion

- 性格：brisk / mechanical——160ms、`cubic-bezier(0.16,1,0.3,1)`，只动 transform/opacity
- 按压反馈 scale(0.98)；结构化内容加载用骨架屏，不用全页 spinner
- `prefers-reduced-motion` 全局降级

## This design will NOT use

- 渐变文字 / 紫蓝渐变 / 玻璃拟态 / 霓虹暗色（AI slop 高发路径，逐一封死）
- 卡片套卡片；每个容器默认描边（分隔先问留白够不够）
- 彩色左边条做装饰（仅限语义状态与 blockquote 惯例）
- 全大写间距字母标签铺排；装饰性图标压标题
- 一屏双强调色；无决策的默认样式（每个值都应能追溯到本契约）

## 审查循环

改呈现层后必须渲染并过 12 条批判（squint / swap / 焦点唯一 / 节奏 / 分隔阶梯 /
type texture / 色彩纪律 / 状态矩阵 / 动效 / 信任细节 / 陌生人测试），
修最差的三项再渲染。截图存 `.playwright-mcp/`（gitignore 范围）。
