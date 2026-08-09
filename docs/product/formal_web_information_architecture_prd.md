# Freqtrade AI 正式网页信息架构简化 PRD

- 文档状态：`v0.3 已确认 / v0.4 调度设计补充待审阅`
- 版本：`v0.4`
- 日期：`2026-08-09`
- 适用范围：Freqtrade AI 正式 Web 界面、正式候选研究展示、OKX_DEMO 只读运行展示
- 实施门禁：用户明确确认本 PRD 前，只允许文档和只读审查；不得开始页面实现
- 长期规则：后续任何页面新增、删除、合并、改名、按钮或数据源调整，都必须先更新本 PRD 的“页面-区块-按钮功能清单”和“数据与数据库操作映射”，再实现和验收

## 1. 当前页面混乱问题（按优先级）

### P0：首屏不能回答用户最关心的问题

当前 `/` 主要统计策略、生成批次、回测批次、Hyperopt 和排行榜数量。它没有在一个视图中回答：

1. 项目数据是否真实、是否新鲜、是否可用；
2. 正式策略、正式候选、合格候选、运行中策略分别有多少；
3. 最新正式研究批次处于生成、验证、入库、合格/拒绝、部署交接的哪一步；
4. 当前部署状态、OKX_DEMO 运行状态；
5. 最近信号、订单、成交及明确的“无信号/无订单”结论。

风险：用户必须在多个技术页之间自行拼接结论；加载失败、空记录和真实的零容易混淆。

### P0：正式生命周期与开发实验仍在同一导航层

`策略工厂` 已是正式研究入口，但 `本地实验室（非正式）` 仍位于“工作台”一级导航，并提供生成、回测、导入、DeepSeek 单次 E2E、Dry-run 等大量操作。即使已有“非正式”文案，视觉权重仍接近正式入口。

风险：用户可能把 Local Strategy Lab 的单条实验、浏览器内操作反馈或 Dry-run 结果误解为正式候选已进入 `strategy_research_batches` / `strategy_research_candidates` 生命周期。

### P0：导航按技术模块拆分，而不是按用户任务组织

当前 11 个一级入口包含：总览、策略工厂、生成批次、本地实验室、回测批次、回测任务、Hyperopt、排行榜、实盘候选治理、运维面板、OKX Demo。生成、回测、评分、部署、运行被拆开，用户无法沿一条路径完成核对。

### P1：状态语言过多、过长且混用原始枚举

页面同时出现 `API_GAP`、`NOT_ACCEPTABLE`、`RUNNING`、`MISSING`、`STATUS_UNKNOWN`、`NOT_QUEUED_NO_QUALIFIED` 等原始枚举，以及中文解释。原始值适合审计详情，不适合首屏结论。

### P1：技术证据默认展开权重过高

ID、路径、digest、artifact、source trace、ENV presence、schema、完整 JSON 等内容在多个页面占据主要空间；真正的业务结论、拒绝原因和下一步反而不够突出。

### P1：重复页面与重复区块

- `策略工厂`、`生成批次`、`回测批次`、`回测任务`、`排行榜` 分别展示同一生命周期的不同切面；
- `总览`、`运维面板`、`OKX Demo` 都展示运行/状态信息，但没有明确职责边界；
- `Local Strategy Lab` 内又重复生成、回测、评分、Dry-run 和证据浏览。

### P1：空状态仍缺少“没有发生什么”的精确结论

必须持续区分：

- API 读取失败或超时：`未知`，不是 0；
- 尚未生成：`requested/generated/persisted/qualified/rejected = 0`，不是 10 条被拒绝；
- 已完整验证但 0 条合格：研究成功完成、部署不排队；
- 无信号：策略正常运行但条件未触发；
- 无订单：可能是无信号、风控阻断、无授权或链路失败，不能只显示 0。

### P2：历史/未来能力容易被理解为当前正式能力

`Hyperopt 参数优化` 与 `实盘候选治理` 当前并非正式候选生命周期的必要入口；“实盘”措辞还与本项目固定 `OKX_DEMO` 边界冲突。

## 2. 优化建议（按实施优先级）

1. **重做总览**：首屏只保留项目结论、四类数量、最新研究流水线、部署交接、OKX_DEMO、最近信号/订单。
2. **以策略工厂承载正式生命周期**：将正式候选、研究批次、策略库和排行榜整合为同一页面的分区/页签；唯一主操作为“手动运行一轮研究（10 条）”。
3. **导航降为三条正式主线**：`总览`、`策略工厂`、`模拟盘`；把回测/任务/运维证据放入上下文详情，把开发实验放入默认收起的“开发实验”。
4. **先锁定指标语义和数据源**：目录策略 active、研究候选 qualified、人工 approved、部署 ACTIVE 必须分开；每个数字只认第 8.5 节的唯一来源。
5. **先补最小只读缺口再展示**：运行中策略和最近 signal evaluation 目前缺正式读 API；确认方案前显示未知，不从其他状态倒推。
6. **统一短状态**：首屏只用“正常 / 运行中 / 需关注 / 已阻断 / 未开始 / 未知”；原始枚举放到展开详情。
7. **先结论后证据**：每个区块顺序固定为“结论 → 原因 → 下一步 → 技术证据”。
8. **空状态不可误导**：每个空状态明确数据源、业务含义和下一步，不用 mock 数据补齐成功。
9. **危险能力不进入本次 UI**：不新增部署、grant、启动/停止运行时、下单、重放订单或真实资金入口。

## 3. 产品目标与非目标

### 3.1 目标

- 用户打开正式页面后 10 秒内理解当前项目、研究、部署和 OKX_DEMO 状态；
- 用户能沿“生成 → 验证 → 入库 → 合格/拒绝 → 部署评审 → OKX_DEMO 运行 → 信号/订单”核对完整生命周期；
- 正式入口与开发实验在导航、文案、颜色和数据实体上清楚分离；
- 所有数字来自真实 API/数据库或明确标注为未知，不以 mock/fixture/fallback 伪装成功；
- 拒绝、阻断、未生成、无合格候选、无信号和无订单各有准确解释。

### 3.2 非目标与不可改变边界

本 PRD 及后续页面实现不得：

- 修改交易策略、研究阈值、质量契约、独立 OOS、lookahead、费用、滑点或 15% 回撤门；
- 在没有明确数据缺口、PRD 数据条目、数据归属、回填/兼容/恢复方案和验收证据时修改数据库 schema、migration、模型或 API；
- 修改 OKX_DEMO 风控、订单 writer、grant、执行编排、下单或对账逻辑；
- 读取、显示、记录或提交凭据；
- 切换到真实资金、真实订单或 live trading；
- 启动、停止、重启或接管运行时/自动化；
- 将 Local Strategy Lab 记录并入正式候选，除非未来另有经批准的正式迁移设计；
- 为展示效果制造 mock 成功、伪造候选、订单、成交或部署状态。

优先复用不等于强制兼容旧页面结构。若只读证据证明现有模型/API 无法支持清晰、可信、可追溯的正式页面，在用户确认对应 PRD 条目后，可以补充非交易控制面的必要字段、只读 API 投影或模型关系，但必须遵守第 8.4 节数据演进门。任何涉及 OKX_DEMO 下单、权限、凭据、真实资金、运行时 writer、grant 或订单链路的 schema/API 变化不在这项一般授权内，必须单独提出并等待确认。

## 4. 信息架构与视觉交互原则

### 4.1 信息架构原则

1. **结论先于过程**：首屏先回答“当前怎样、为什么、下一步是什么”，再提供完整证据。
2. **生命周期连续**：生成、验证、入库、合格/拒绝、部署评审、运行、信号和订单必须能顺序追踪，不能只按技术模块分散展示。
3. **摘要、列表、详情三层**：总览给少量关键结论；列表支持比较；单条详情承载完整指标、日志、路径和审计字段。
4. **读写严格分离**：只读状态与会触发写入的主操作在位置、按钮样式、文案和权限提示上明确区分；本次正式页只有现有的手动研究写操作。
5. **运行与研究分离**：回测或排名优秀不自动等于可部署；Demo 运行结果也不自动等于可进入 Live。
6. **状态可追溯**：运行中持续更新；失败保留记录；空、0、未知、阻断和未开始不可互换。
7. **渐进披露**：ID、路径、digest、artifact、原始枚举、完整 JSON 和长拒绝证据默认收起，需要时再展开。
8. **上下文不丢失**：从策略工厂进入批次、回测、排名或运行详情时，始终能返回所属策略/批次，不让用户在技术页迷路。
9. **模拟盘身份常驻**：`OKX_DEMO / 模拟盘` 在总览和模拟盘页持续可见；未来 Live 只读准备状态不能伪装为可切换能力。
10. **不做收益营销**：收益、评分与排名必须同时呈现风险、费用、回撤、验证范围和数据来源，不使用“推荐”“稳赚”等暗示。

### 4.2 视觉方向

整体采用克制、专业、适合长时间阅读的交易研究工作台风格：中性背景、清晰边界、少量语义色、紧凑但不拥挤。避免炫技渐变、霓虹、高饱和大色块、玻璃拟态、无意义动效和大段默认展开的技术文字。

- 页面主视觉来自信息层级和数字排版，不靠装饰；
- 同一屏最多一个主操作，主按钮只用于当前最重要且权限明确的动作；
- 卡片不做“每个字段一张卡”，相关字段组成一个有结论的区块；
- 盈利不使用大面积绿色庆祝；亏损/拒绝不使用整屏红色，仅在标签、边框或局部提示中表达；
- 所有状态同时使用短标签、图标/形状和文字说明，不能只靠颜色。

### 4.3 字号与数字层级

| 层级 | 桌面建议 | 窄屏建议 | 用途 |
| --- | --- | --- | --- |
| 页面标题 H1 | 28–32px / 1.2 | 24–28px / 1.25 | 每页唯一标题 |
| 区块标题 H2 | 20–22px / 1.35 | 18–20px / 1.35 | 主要区块 |
| 小标题 H3 | 16–18px / 1.4 | 16px / 1.4 | 卡片/详情标题 |
| 正文 | 14–16px / 1.5–1.65 | 14–16px / 1.5–1.65 | 结论与说明 |
| 辅助标签 | 12–13px / 1.4 | 12–13px / 1.4 | 时间、来源、次要字段 |
| 核心数字 | 24–32px / 1.15 | 22–28px / 1.2 | 策略、候选、运行中等关键计数 |
| 表格 | 13–14px / 1.4 | 13–14px / 1.4 | 高密度数据 |

正文使用系统无衬线字体；ID、digest、代码和路径使用等宽字体。数字使用 tabular numerals，百分比、小数位和单位在同一列保持一致。正文不得低于 14px，辅助标签不得低于 12px。

### 4.4 留白、栅格与卡片密度

- 采用 4px 基础间距；常用间距为 8、12、16、24、32、48px；
- 页面内容最大宽度建议 1440px，桌面 12 栏、平板 8 栏、窄屏 4 栏；
- 页面左右安全边距：桌面 24–32px，平板 20–24px，窄屏 16px；
- 一级区块垂直间距 32–48px，卡片间距 16–24px；
- 卡片内边距桌面 20–24px、窄屏 16px；同类摘要卡等高；
- 首屏摘要卡桌面最多四列，平板两列，窄屏单列或 2×2 紧凑计数；
- 默认卡片只展示一个结论、2–4 个关键值和一个下一步；超过范围进入 details、抽屉或独立详情页；
- 技术证据卡使用更低视觉权重的中性边框，不与业务结论卡竞争。

### 4.5 色彩、深浅色与可访问性

- 颜色采用语义 token，不在组件内散落固定色值；浅色与深色主题具有相同语义；
- 中性色用于背景、边框和正文；品牌/强调色只用于导航选中和主操作；
- 成功/正常使用低饱和绿色，运行中/信息使用蓝色，关注使用琥珀色，失败/危险使用红色，未知/未开始使用灰色；
- 正文与背景对比度至少符合 WCAG AA 4.5:1，大字号和非文本 UI 边界至少 3:1；
- 状态标签必须包含文字；图标增加 `aria-label` 或可见文字，装饰图标对读屏隐藏；
- 焦点环在深浅主题中清晰可见；键盘可到达所有链接、按钮、页签、details 和表格交互；
- 用户启用 reduced motion 时取消非必要过渡；主题切换不得造成内容闪烁或状态含义变化。

统一短状态仅使用：`正常`、`运行中`、`需关注`、`已阻断`、`未开始`、`未知`；业务需要时附第二行原因，原始枚举放在展开详情。

### 4.6 按钮、图标与交互层级

| 层级 | 用途 | 规则 |
| --- | --- | --- |
| 主按钮 | 当前页唯一关键写操作 | 每个视图最多一个；策略工厂为“手动运行一轮研究（10 条）” |
| 次按钮 | 刷新、返回、查看相关页面 | 中性样式，不与主按钮争夺视线 |
| 文本链接 | 查看详情、技术证据、关联记录 | 动词开头，链接目的可预期 |
| 危险按钮 | 破坏性或真实资金操作 | 本次正式页面不得出现 |

- 常用图标尺寸 16px，独立图标按钮 20px；不得用无标签图标承担关键业务操作；
- 点击/触控目标至少 44×44px；禁用态仍保留可读标签，并在相邻位置解释原因；
- 写操作提交后按钮进入明确 loading，禁止重复点击；超时显示“结果未知，先核对状态”，不自动重试；
- details 的摘要必须说明展开后能看到什么，不能只写“更多”。

### 4.7 数据表、窄屏与加载规则

- 表头名称简短稳定；状态、名称/对象、关键结果、时间、操作按阅读顺序排列；
- 行高不低于 48px，长文本最多两行截断并可展开；数字右对齐，文本左对齐；
- 状态列使用短标签，原因在相邻摘要或详情中；固定表头仅在不遮挡内容时使用；
- 表格必须提供清晰的空、加载、失败状态；数据刷新不使列宽剧烈跳动；
- 窄屏优先转为“摘要行 + 展开详情”或卡片列表；如必须横向滚动，首列保持对象名称且显示滚动提示；
- 390px 宽度下不得出现页面级水平溢出、按钮截断或必须缩放才能阅读；
- 骨架屏形状应对应最终布局，最多覆盖预计首屏；加载中不显示 0、成功色或陈旧结论；
- 超过 400ms 才显示骨架，短请求避免闪烁；超过既有请求超时后切换到明确失败/未知状态；
- 保留上次成功数据时必须标注“可能已过期”和最后更新时间，不能继续显示为当前正常。

### 4.8 三个正式入口的文字线框与视觉验收

#### 总览 `/`

```text
┌ 页面标题：总览 ── 数据新鲜度 ── [OKX_DEMO / Demo-only]
│ 结论：当前系统是否可理解、最大阻断、唯一建议下一步
├ [正式策略] [正式候选] [合格候选] [运行中策略]
├ 最新研究流水线：生成 → 验证 → 入库 → 合格/拒绝 → 部署评审
│                六计数 + 当前阶段 + 简短原因 + 查看策略工厂
├ 部署摘要（左）                     模拟盘健康（右）
│ 部署交接/运行策略/阻断              信号/风控/订单/成交/对账
└ 最近活动时间线（最多 5 条）── 查看模拟盘
```

视觉验收：1440×900 首屏无需滚动即可看见结论、四计数、研究阶段和 Demo 健康摘要；不存在超过两行的默认技术说明；任一源失败只影响对应卡片，不把整页变成 0；主视线按“结论 → 数字 → 流水线 → 下一步”移动。

#### 策略工厂 `/strategies`

```text
┌ 页面标题：策略工厂 ── 门禁短标签 ── [手动运行一轮研究（10 条）]
│ 正式入口说明 + OKX_DEMO 安全边界（单行）
├ 最新研究：六计数 + 阶段进度 + 部署交接 + 最近完成时间
│ [最新研究] [候选] [策略库] [排行榜]
├ 当前页签主列表：短状态、名称、关键结果、拒绝原因摘要、时间
│   └ 展开：完整拒绝证据 / ID / path / digest / JSON
└ 空状态或分页/历史批次入口
```

视觉验收：主按钮是全页唯一高强调操作；质量契约首屏压缩为不超过两行；六计数顺序固定且在窄屏不混行；拒绝原因默认一行摘要，技术证据默认关闭；`QUALIFIED` 与已部署在视觉和文案上严格不同。

#### 模拟盘 `/okx-demo`

```text
┌ 页面标题：模拟盘 ── [OKX_DEMO] [Demo-only] ── 最后对账时间
│ 结论：运行健康/阻断原因/唯一建议下一步
├ [运行中策略] [最近信号] [开放订单] [持仓/成交]
├ 信号 → 意图 → 风控 → 订单 → 成交 → 对账（最近一条链路）
├ 最近订单表                         订单详情抽屉/展开区
└ Live 上线准备证据（只读、默认收起；无任何切换或启动按钮）
```

视觉验收：`OKX_DEMO` 与 `Demo-only` 在首屏常驻；订单为 0 时仍能看到信号/风控原因；订单详情不遮蔽安全状态；Live 准备卡视觉权重低于 Demo 运行主区，`Live 已获人工批准` 绝不使用主按钮或运行中样式。

## 5. 目标导航与生命周期路径

### 5.1 一级导航

| 分组 | 入口 | 目的 | 处理 |
| --- | --- | --- | --- |
| 正式工作台 | 总览 `/` | 当前项目、研究、部署、Demo 和最近活动的一页结论 | 保留并重做 |
| 正式工作台 | 策略工厂 `/strategies` | 正式候选生命周期与策略库唯一入口 | 保留、强化 |
| 正式工作台 | 模拟盘 `/okx-demo` | OKX_DEMO 运行、信号、订单、成交与对账 | “OKX Demo 执行”改名并保留 |
| 开发实验（默认收起） | Local Strategy Lab `/local-strategy-lab` | 单条本地开发实验和诊断 | 移动、加开发实验标识 |
| 开发实验（默认收起） | 技术运行证据 `/operator-dashboard` | 只读诊断、artifact、ENV presence | 移动 |
| 历史/高级（默认收起） | 生成/回测/任务/Hyperopt/治理详情 | 兼容旧链接和深入排障 | 从一级主导航移除，不删除路由 |

### 5.2 正式生命周期

`总览` → `策略工厂` → `正式研究批次` → `10 条候选生成` → `验证` → `全量入库` → `QUALIFIED / REJECTED / VALIDATION_FAILED` → `仅 QUALIFIED 进入既有部署评审` → `部署/运行状态` → `模拟盘` → `信号 → 意图 → 风控 → 订单 → 成交/对账`

关键规则：

- 在生成前失败：显示“未生成”，不能显示 10 条拒绝；
- `10/10/10/10` 且 `0 qualified`：显示“研究完成，无合格候选，未进入部署队列”；
- 读取失败/超时：显示“未知”，不显示 0；
- 模拟盘无订单：必须同时显示最近信号/风控结论，说明是自然无信号还是被阻断；
- Local Strategy Lab 不在这条路径中，只能从“开发实验”进入。

## 6. 正式页面定义

### 6.1 总览 `/`

- 页面目的：提供当前系统的单页事实摘要。
- 核心用户问题：现在是否正常？有多少正式策略/候选/合格/运行中？最新研究到哪一步？是否部署？Demo 最近发生了什么？
- 必须展示的数据：数据新鲜度、正式策略数、候选数、合格数、运行中数、最新研究六计数、部署交接、OKX_DEMO 安全边界、最近信号/订单/成交和对账结论。
- 主操作：`查看策略工厂`；次操作：`查看模拟盘`。
- 空状态：分别显示“尚无研究批次”“尚无合格候选”“尚未部署”“当前无信号”“当前无订单”；API 失败显示“状态未知”。

### 6.2 策略工厂 `/strategies`

- 页面目的：正式候选生命周期、批次证据和正式策略库的唯一入口。
- 核心用户问题：能否运行研究？最新一轮完成到哪里？为什么候选被拒绝？哪些已合格？哪些正在运行？
- 必须展示的数据：正式运行门禁、固定 10 条、质量契约摘要、六计数、批次状态/时间、候选状态/拒绝原因、部署交接、正式策略列表和运行状态。
- 主操作：`手动运行一轮研究（10 条）`。
- 空状态：无批次、未生成、研究失败、完整验证但无合格、无正式策略分别处理。

### 6.3 策略详情 `/strategies/:strategyId`

- 页面目的：查看一个正式策略的版本、验证、来源、部署和运行关联。
- 核心用户问题：这是哪个版本？来源与验证是否可信？是否部署/运行？
- 必须展示的数据：策略状态、当前版本、验证状态、来源、关联研究候选/批次（若存在）、部署状态、运行状态；技术 ID/路径/Diff 默认收起。
- 主操作：返回策略工厂；本次不新增写操作。
- 空状态：ID 不存在、版本缺失、来源未知、未部署。

### 6.4 模拟盘 `/okx-demo`

- 页面目的：只读展示 OKX_DEMO 的运行状态和最近信号/订单生命周期。
- 核心用户问题：Demo 是否安全运行？哪些策略在运行？最近有信号吗？订单/成交/对账如何？
- 必须展示的数据：`execution_target=OKX_DEMO`、`allow_real_funds=false`、`real_orders=false`、运行中策略数、最近信号/意图/风控、订单、成交、仓位和对账状态。
- 主操作：选择订单查看详情；本次不新增启动、停止、grant、下单或重试按钮。
- 空状态：无运行策略、无信号、无订单、无成交、证据读取失败分别处理。

### 6.5 开发实验 `/local-strategy-lab`

- 页面目的：本地单条策略生成、回测、证据核对和受控 Dry-run 的开发实验。
- 核心用户问题：本地实验前置条件是否满足？实验记录是否持久化？
- 必须展示的数据：醒目的“开发实验 / 非正式候选”标识、单条实验状态、API/DB 证据、操作边界。
- 主操作：保留现有实验操作，但不出现在正式一级导航，不影响正式候选统计。
- 空状态：保持现有 fail-closed 逻辑；任何操作反馈都不得显示为正式生命周期成功。

### 6.6 生成批次 `/generation-runs`（深入证据）

- 页面目的：审计旧生成链和 Local Lab 的 Provider 请求记录，不代表正式 10 条候选研究批次。
- 核心用户问题：请求是否真的创建记录？Provider/Model 是什么？请求、生成、接受、失败各多少？
- 必须展示的数据：run ID、来源、Provider/Model、状态、四计数、时间、错误。
- 主操作：展开错误、复制 run ID；无写操作。
- 空状态：API 已连接但无记录时显示“暂无生成记录，不代表生成成功”；来源不可用时显示未知。

### 6.7 回测批次 `/backtest-runs`（深入证据）

- 页面目的：按批次核对回测矩阵、任务完成度和真实 BacktestResult。
- 核心用户问题：哪些批次完成/阻断/失败？是否有真实结果？
- 必须展示的数据：run/task/result IDs、策略版本、pair/timeframe、profile、状态、进度、收益/回撤/胜率/交易数、阻断或失败原因。
- 主操作：展开技术详情、复制证据；无写操作。
- 空状态：无 run 显示尚无真实回测；task 成功但 Result 缺失显示不可验收。

### 6.8 回测任务 `/backtest-tasks`（深入证据）

- 页面目的：逐任务排查 pair/timeframe 执行和 artifact 入库结果。
- 核心用户问题：是哪一个任务阻断/失败？配置和结果证据在哪里？
- 必须展示的数据：task/run/version IDs、pair/timeframe、状态、config/result paths、错误、Result 指标和 artifact。
- 主操作：展开技术详情、复制证据；无写操作。
- 空状态：无任务、BLOCKED、FAILED、缺 Result 分别说明。

### 6.9 排行榜 `/ranking`（兼容路由，内容合并到策略工厂）

- 页面目的：比较具有真实 `StrategyScore` 和来源追踪的策略。
- 核心用户问题：谁分数最高？分数是否关联真实回测？为什么未入榜或被淘汰？
- 必须展示的数据：strategy/version/score/result IDs、总分与分项、scoring version、来源、失败/淘汰原因。
- 主操作：查看 ID/路径/来源；无写操作。
- 空状态：无真实 score 显示空榜；fixture/fallback 不参与排名；排名不代表已合格或可部署。

### 6.10 Hyperopt `/hyperopt-runs`（高级/历史）

- 页面目的：查看历史参数优化 artifact，不属于正式候选必经生命周期。
- 核心用户问题：是否有可追溯的优化记录和参数结果？
- 必须展示的数据：run 状态、strategy、spaces、best params、artifact 和来源。
- 主操作：展开参数与 artifact；无写操作。
- 空状态：后端 API/核心实体未确认时显示非核心/未知，不显示为正式成功。

### 6.11 技术运行证据 `/operator-dashboard`（开发实验）

- 页面目的：只读排查 runtime contract、诊断、artifact、ENV presence 和治理事件。
- 核心用户问题：哪个技术前置条件阻断？证据来源和新鲜度是什么？
- 必须展示的数据：系统结论、required diagnostics、runtime sections、artifact、ENV presence、audit、安全边界。
- 主操作：展开详情、复制本地证据；无运行控制。
- 空状态：endpoint/报告不可用显示不可用，不使用 fixture 替代；ENV 永远不显示值。

### 6.12 候选治理证据 `/live-governance`（高级/未来能力）

- 页面目的：只读展示候选、审批、部署和监控的历史/未来治理证据。
- 核心用户问题：是否存在可信治理记录？哪些字段仍不可用？
- 必须展示的数据：profile、approval、deployment、monitoring 的来源、状态、阻断和审计引用。
- 主操作：展开证据；无审批、部署或交易写操作。
- 空状态：active backend/DB 来源未确认时显示“未知/历史证据”，不得暗示 live ready。

以上深入证据路由继续保留以兼容旧链接，但从主导航移出；从策略工厂或模拟盘的上下文详情进入。`实盘候选治理` 改为“候选治理证据（只读/未来能力）”，不得暗示已启用真实资金。

## 7. 页面-区块-按钮功能清单

本清单是实现追溯基线。`UI-*` ID 必须在后续 Issue/PR/测试中引用。

| ID | 页面 / 区块 | 按钮或链接（位置） | 触发动作与数据/API | 加载 / 成功 / 失败 / 无数据 | 权限与风险提示 | 处理 |
| --- | --- | --- | --- | --- | --- | --- |
| UI-G-01 | 全局侧栏 / 正式工作台 | 总览（顶部） | 路由到 `/`；页面发起只读聚合查询 | 路由即时；页面各区块独立加载/失败 | 只读 | 保留 |
| UI-G-02 | 全局侧栏 / 正式工作台 | 策略工厂 | 路由到 `/strategies` | 同上 | 页面含唯一正式研究写入口 | 保留 |
| UI-G-03 | 全局侧栏 / 正式工作台 | 模拟盘 | 路由到 `/okx-demo` | 同上 | 固定 OKX_DEMO，只读 | 改名保留 |
| UI-G-04 | 全局侧栏 / 开发实验 | 展开/收起开发实验 | 仅前端导航状态，不调用 API | 默认收起 | 明示“非正式候选” | 新增 |
| UI-G-05 | 开发实验分组 | Local Strategy Lab | 路由到 `/local-strategy-lab` | 页面独立加载 | 可能有本地研究写入；不进入正式生命周期 | 移动 |
| UI-G-06 | 开发实验分组 | 技术运行证据 | 路由到 `/operator-dashboard` | 只读运行证据 | 不得展示 ENV 值 | 移动/改名 |
| UI-D-01 | 总览 / 项目结论 | 查看策略工厂（首屏右上） | 路由 `/strategies` | 无业务写入 | 只读导航 | 新增 |
| UI-D-02 | 总览 / 模拟盘摘要 | 查看模拟盘 | 路由 `/okx-demo` | 无业务写入 | 只读导航 | 新增 |
| UI-D-03 | 总览 / 最新研究 | 展开批次详情 | 展示已加载批次计数、拒绝摘要、时间、原始状态 | 加载禁用；失败显示未知；无批次显示说明 | 只读，不触发刷新/研究 | 新增 |
| UI-D-04 | 总览 / Execution Target | 无按钮；只读状态卡 | 展示当前仅为 `Demo-only`，以及未来 Live 准备状态的只读投影 | Live 来源不可用时显示“Live 未配置/状态未知”，绝不推断已批准 | 不提供切换、配置凭据、批准或启动按钮；“已获人工批准”不等于已启动 | 新增状态规范，是否实现待用户确认 |
| UI-S-01 | 策略工厂 / 正式研究控制 | 手动运行一轮研究（10 条）（唯一主按钮） | `POST /api/strategy-research/formal-run`；后端按既有协调器启动 | 加载“正在提交”；成功进入运行中并轮询；`BLOCKED/FAILED` 显示原因；超时显示“提交结果未知，先查状态”，不得重试 | 不收集凭据；不授权 Dry-run、grant、下单；需既有唯一所有权/锁门禁 | 保留，文案简化 |
| UI-S-02 | 策略工厂 / 最新批次 | 查看完整批次 | 展开已由 `GET /api/strategy-research-batches` 读取的批次与候选 | API 失败=未知；无批次=尚未生成 | 只读 | 合并现有 details |
| UI-S-03 | 策略工厂 / 候选列表 | 查看拒绝原因 | 展开候选 `rejection_reasons` 与摘要证据 | 无拒绝原因且 QUALIFIED 显示“通过研究质量门”；未知不显示通过 | 只读，原始 JSON 放二级详情 | 新增/重排 |
| UI-S-04 | 策略工厂 / 候选列表 | 查看技术证据 | 展开 digest、path、完整 evidence snapshot | 缺失标“证据缺失” | 只读 | 收起 |
| UI-S-05 | 策略工厂 / 正式策略库 | 策略名称链接 | 路由 `/strategies/:strategyId` | ID 无效进入不存在状态 | 只读 | 保留 |
| UI-S-06 | 策略工厂 / 分区导航 | 最新研究 / 候选 / 策略库 / 排行榜 | 页面内锚点或页签；排行榜使用现有只读 API | 各分区独立状态 | 不用页签隐藏错误红点 | 新增，合并旧页入口 |
| UI-S-07 | 策略工厂 / 批次详情 | 复制报告路径/摘要/ID | 浏览器剪贴板，仅复制当前字段 | 空值禁用；失败显示复制失败 | 可能含本地路径，不自动外发 | 保留在详情 |
| UI-SD-01 | 策略详情 / 页头 | 返回策略工厂 | 路由 `/strategies` | 即时 | 只读 | 新增 |
| UI-SD-02 | 策略详情 / 来源与版本 | 查看来源追踪 / 查看 Diff / 复制 ID 或路径 | 展开已加载字段/写剪贴板 | 缺失明确显示 | 不复制凭据；默认收起代码与路径 | 合并保留 |
| UI-GR-01 | 生成批次 / 行详情 | 查看完整错误 / 复制批次 ID | 只读已加载数据、剪贴板 | 无错误显示“未记录错误” | 开发证据 | 保留在深入证据页 |
| UI-BR-01 | 回测批次 / 技术详情 | 展开技术详情 / 复制 ID、路径 | 读取共享 loader 的 backtest 数据 | 缺 Result 明示不可验收 | 只读 | 保留在深入证据页 |
| UI-BT-01 | 回测任务 / 技术详情 | 展开技术详情 / 复制 ID、路径 | 同上 | BLOCKED/FAILED/无 Result 分开 | 只读 | 保留在深入证据页 |
| UI-R-01 | 排行榜 / 行详情 | 查看 ID、路径和来源 | `GET /api/ranking` 及失败原因/lineage 只读数据 | 无真实 score 显示空状态；fallback 不计入排名 | 只读；排名不等于可部署 | 移入策略工厂并保留旧路由 |
| UI-H-01 | Hyperopt / 行详情 | 参数、spaces 与 Artifact | 只读历史数据 | API 不存在/来源非核心时明确标注 | 非正式生命周期 | 移入高级/历史 |
| UI-O-01 | 模拟盘 / 订单表 | 选择订单行 | 仅设置前端 selectedId，展示当前 `GET /api/okx-demo/observability` 结果内详情 | 无订单显示原因导向的空状态 | 只读，不发送订单请求 | 保留 |
| UI-O-02 | 模拟盘 / 订单详情 | 复制 ID / 展开生命周期 | 剪贴板/本地展开 | 字段缺失标未知 | 订单 ID 不是授权；不提供重放 | 合并保留 |
| UI-O-03 | 模拟盘 / 上线准备证据 | 查看 Live 准备证据（只读详情） | 仅展开未来经批准 API 返回的检查项、人工批准摘要、过期时间和审计引用；当前 API 未定义 | 无数据=`Live 未配置`；部分检查=`Live 准备中`；完整且有效的人工作业凭据=`Live 已获人工批准`；读取失败=`未知` | 无“切换到 Live”“启动”“下单”按钮；不读取/显示密钥；证据详情不能充当授权 | 新增状态规范，API 待核对 |
| UI-OP-01 | 技术运行证据 / 各详情组 | 展开 Runtime/Artifacts/ENV/治理/安全详情 | 只读共享 loader / runtime endpoints | 不可用显示原因 | ENV 只显示 presence，绝不显示值 | 移入开发实验 |
| UI-OP-02 | 技术运行证据 / 字段 | 复制路径/Schema/来源 | 剪贴板 | 空值禁用 | 本地证据不自动外发 | 保留 |
| UI-LG-01 | 候选治理证据 / 卡片 | 展开候选/审批/部署/监控详情 | 只读 loader 数据 | 无后端路由时标“历史/未知”，不能显示成功 | 未来能力，不授权真实部署 | 改名并移入高级 |
| UI-L-01 | Local Lab / 工作流 | 阶段 1–4 按钮 | 仅切换/检查当前实验阶段 | 前置未满足禁用 | 非正式实验 | 保留，强化开发标识 |
| UI-L-02 | Local Lab / 生成 | 提交生成 | `POST /api/strategy-generation-runs`，固定 `requested_count=1` | 加载/成功需 API+DB ID；超时未知；失败显示错误；空输入禁用 | 需要 operator token 和一次性 Provider 授权；不进入正式候选 | 保留 |
| UI-L-03 | Local Lab / 生成 | 取消 | 仅取消当前前端请求/状态；后端是否已写入必须刷新核对 | 取消不等于后端未写入 | 不重放 | 保留 |
| UI-L-04 | Local Lab / 证据 | 刷新持久结果 | 重新调用共享只读 API | 加载禁用；失败保留上次数据但标过期/未知 | 只读 | 保留 |
| UI-L-05 | Local Lab / 证据浏览 | 当前核心 / 诊断、证据类型、记录行 | 前端筛选和选择已加载数据 | 无数据给精确空状态 | 只读 | 保留 |
| UI-L-06 | Local Lab / 回测 | 运行本地回测 | `POST /api/backtest-runs/local` | BLOCKED/FAILED/成功均需 DB ID；超时后刷新核对 | 仅本地回测，不授权交易 | 保留 |
| UI-L-07 | Local Lab / 回测 | 导入 Artifact | `POST /api/backtest-tasks/{task_id}/artifact-ingest` | 幂等冲突/失败显示原始原因；成功需 Result/Score ID | 仅持久化本地结果 | 保留 |
| UI-L-08 | Local Lab / 高级 E2E | DeepSeek 单次 E2E | `POST /api/strategy-generation-runs/deepseek-single` | 前置不满足禁用；超时未知 | 一次性真实 Provider 授权；不得泄密 | 保留但默认收起 |
| UI-L-09 | Local Lab / Dry-run | readiness / start / stop（现有条件按钮） | `/api/dry-run/readiness`、`/control/start`、`/control/stop` | fail-closed；必须显示 manifest/snapshot | 仅本地受控 Dry-run；不是 OKX_DEMO 正式部署 | 保留但开发隔离 |
| UI-NF-01 | 404 | 返回总览 | 路由 `/` | 即时 | 只读 | 保留 |

## 8. 数据与数据库操作映射

### 8.1 操作分类与边界

| 分类 | 页面允许范围 | 唯一写入者/权限边界 | 本任务处理 |
| --- | --- | --- | --- |
| R：只读查询 | 总览、策略库、研究批次、排名、运行状态、信号/订单/成交/对账 | Web UI 只读；API/repository 读取 | 允许调整展示 |
| RW-R：候选研究写入 | 策略工厂手动研究；Local Lab 单条实验是另一条开发链 | 正式研究必须经过既有 coordinator、锁、所有权门；Local Lab 维持既有 operator 授权 | 只调整已有按钮展示，不改写入逻辑 |
| RW-D：部署写入 | 既有自动部署评审读取 `status=QUALIFIED` | 仅既有唯一主任务/自动化写入者 | 页面只读展示；不新增按钮 |
| RW-O：订单链路 | 信号、意图、风控、approved execution、writer、订单、成交、对账 | 既有 OKX_DEMO 唯一 writer | 页面只读；禁止改动或触发 |

### 8.2 逐页面/区块/按钮映射

| 映射 ID / 对应 UI | 分类 | 读取 API | 写入 API | 表/实体与字段范围 | 前置、唯一写入者、审计/幂等 | 失败/超时页面反馈 |
| --- | --- | --- | --- | --- | --- | --- |
| DATA-D-01 / UI-D-01~03 | R | `GET /api/strategies`、`/strategy-versions`、`/strategy-research-batches`、`/strategy-research/formal-run`、`/ranking`、`/okx-demo/observability`；`strategy_deployments` / `signal_evaluations` 当前无正式读 API，运行中策略和最近信号不得从其他状态猜测 | 无 | `strategies(id,status,current_version_id)`；`strategy_versions(id,validation_status)`；研究批次/候选计数与状态；score；OKX observability allowlist；运行中事实应来自 `strategy_deployments.status=ACTIVE`，信号事实应来自 `signal_evaluations` | 聚合必须保留 source/freshness；任何一个关键源失败，不用 0 替代；`strategies.status=active` 不等于已部署运行 | 现有 API 缺口下对应计数显示“未知/暂不可用”；不得拿策略目录状态代替运行状态 |
| DATA-LIVE-RO-01 / UI-D-04、UI-O-03 | R（未来 Live 准备只读投影） | API 路径待核对；本任务不得新增或假定 endpoint | 无 | Demo 与 Live 的 execution target、账户 fingerprint、检查项、批准摘要、批准范围/过期时间、变更/回滚演练审计引用；实体名、表名、字段名和 schema 全部待核对 | 必须由未来独立 Live 控制面/审计域提供；Web UI 不是批准者或写入者；Demo 批准、grant、凭据和订单 ID 不得复用 | 缺数据=`Live 未配置`；部分/过期/不一致=`Live 准备中或已阻断`；API 失败=`未知`；不得显示可切换 |
| DATA-S-01 / UI-S-01 | RW-R | `GET /api/strategy-research/formal-run` | `POST /api/strategy-research/formal-run` | coordinator 状态文件/锁；成功流程写 `strategy_research_batches` 与 `strategy_research_candidates`。具体中间进程字段不由 UI 写 | 前置：coordinator `READY`、无 active run、锁与所有权满足；正式 runner 为唯一写入者；`run_id`、`report_digest`、`batch_id+candidate_name/code_digest` 唯一；POST 的重复/并发语义以 coordinator 现状为准，不在本任务改变 | `BLOCKED` 展示 reason；HTTP 超时=结果未知，禁用自动重试，继续 GET 状态核对 |
| DATA-S-02 / UI-S-02~04 | R | `GET /api/strategy-research-batches?limit=20`、必要时 `GET /api/strategy-research-candidates?status=...` | 无 | batches：`id,run_id,status,requested/generated/persisted/qualified/rejected,failure_reason,selection_policy,report_path,digest,commit,completed_at,created_at`；candidates：`id,batch_id,name,status,loadable,static_check,lookahead_status,score,validation_passed,deployable_candidate,rejection_reasons,evidence_snapshot` | `QUALIFIED` 查询仍受官方质量契约过滤；UI 不重算/改写状态 | API 失败=未知；无 rows=尚无持久化批次；不得显示“全部拒绝” |
| DATA-S-03 / UI-S-05~07 | R | `GET /api/strategies`、`/strategy-versions`、`/ranking` | 无 | `strategies`、`strategy_versions`、`strategy_scores` 的展示字段；`strategy_research_candidates` 只有 `batch_id/source_path/code_digest`，没有指向正式策略、版本、批准或部署的 FK | 只读；剪贴板不写数据库；正式候选到正式策略的跨链关联必须由可审计 receipt/FK/API 证明，不能按名称或 digest 猜测 | 单一 API 失败仅影响对应分区；未建立可证明关联时显示“正式入库衔接待核对” |
| DATA-SD-01 / UI-SD-* | R | 当前共享 loader：`GET /api/strategies`、`/strategy-versions`；部署/运行关联当前无正式读 API | 无 | strategy/version 全字段的展示子集；运行事实实体已确认为 `strategy_deployments`，并通过 `strategy_id/strategy_version_id/candidate_approval_id` 关联正式批准链；页面投影缺失 | 不加载 generated_code 全文到首屏；路径/ID 收起；`Strategy.status` 只表达目录生命周期 | 不存在=404 式空状态；部署投影不可用时显示未知，不显示未部署 |
| DATA-GR-01 / UI-GR-01 | R | `GET /api/strategy-generation-runs` | 无 | `strategy_generation_runs`：`id,status,provider,model,prompt hash/summary,params snapshot,requested/generated/accepted/failed,error,timestamps` | 来源必须标 real/non-core/unknown | 失败保留记录；无数据不代表 provider 正常 |
| DATA-B-01 / UI-BR/BT | R | `GET /api/backtest-runs`、`/backtest-tasks`、`/backtest-results` | 无 | `backtest_runs(id,execution_scope_id,strategy_version_id,profile,status,counts,timestamps)`；tasks 的 pair/timeframe/status/path/error；results 的 IDs/path/metrics/profit/drawdown/win_rate/trades/timerange | Result 必须可关联 run+task；读页面不启动回测 | task succeeded 但无 Result=不可验收；API 失败=未知 |
| DATA-R-01 / UI-R-01 | R | `GET /api/ranking`，兼容失败原因/lineage API | 无 | `strategy_scores(id,strategy_id,strategy_version_id,backtest_result_id,scoring_version,total/profit/risk/stability/quality_score,metrics_snapshot,created_at)` | 排名必须关联真实 score；缺 backtest_result 时按现有契约解释，不自行判合格 | 无真实分数=空榜；fallback/fixture 不进入正式数字 |
| DATA-O-01 / UI-O-01~02 | R（订单链路只读） | `GET /api/okx-demo/observability?limit=N` | 无 | allowlist 涉及 full chain：generation/strategy/version/backtest/score、candidate approval、signal snapshot、`trade_intents`、`risk_decisions`、`approved_executions`、`exchange_orders`、`exchange_fills`、authoritative snapshots/events、reconciliation；页面字段以 schema allowlist 为准 | OKX_DEMO 唯一 writer 边界不变；UI 不获取 grant、不 POST、不重放 unknown historical order | API 失败=全部未知；无订单需结合 signal/risk 显示原因；无 fill 不等于订单失败 |
| DATA-OP-01 / UI-OP-* | R | `/runtime/read-only`、`/runtime/operator-status`、governance event 兼容路径；当前 `/api` 前缀匹配状态需在实现前核对 | 无 | 主要为 artifact/runtime report；若有 DB archive ID 则展示。ENV 仅 `name,present,required,source` | 只读，禁止显示值；来源路由不明确时标待核对 | endpoint 不可达=不可用，不使用 fixture 作为正常 |
| DATA-LG-01 / UI-LG-01 | R | frontend 尝试 `/api/live-candidates/governance` 等；当前 active backend/DB 映射待核对 | 无 | profile/approval/deployment/monitoring 多为 artifact/schema summary；正式 DB 表待核对 | 不得作为当前正式部署事实 | 明示“来源未知/历史证据”，不显示 live ready |
| DATA-L-01 / UI-L-02 | RW-R（开发实验） | 提交后共享只读 API | `POST /api/strategy-generation-runs` | 写 `strategy_generation_runs`、`strategies`、`strategy_versions` 及批准目录文件；字段按现有 generation service | operator token 只在请求内存；`requested_count=1`；Provider 一次性授权；幂等键/重复提交语义待核对 | 超时=未知，刷新 DB 证据；不可直接重提 |
| DATA-L-02 / UI-L-06 | RW-R（开发回测） | backtest GET APIs | `POST /api/backtest-runs/local` | 写 `backtest_runs`、`backtest_tasks`；成功执行后的 result 写入路径以现有 service 为准 | 本地 preflight；execution scope；run/task 唯一约束 | BLOCKED 也应有持久证据；超时刷新核对 |
| DATA-L-03 / UI-L-07 | RW-R（结果入库） | backtest/ranking GET APIs | `POST /api/backtest-tasks/{task_id}/artifact-ingest` | 写/更新 `backtest_results`、`strategy_scores`；task-result 一对一，score `(strategy_version_id,scoring_version)` 唯一 | Artifact 必须可验证且关联 task；幂等细节按现有 ingest service，待实现前测试核对 | 冲突/重复/失败显示明确原因；成功必须出现 Result/Score ID |
| DATA-L-04 / UI-L-08 | RW-R（受控 Provider） | operator readiness + evidence GET | `POST /api/strategy-generation-runs/deepseek-single` | 与 generation 实体/文件写入相关；精确写字段同现有 service | 真实 Provider 仅一次授权，key 不进入 UI/DB/log | 超时未知，禁止自动重试 |
| DATA-L-05 / UI-L-09 | RW-R（开发 Dry-run） | `GET /api/dry-run/management` 等 | `POST /api/dry-run/readiness`、`/control/start`、`/control/stop` | 主要写本地 manifest/status artifact；是否涉及 DB 表当前未确认，标“待核对” | 只允许本地受控 Dry-run；不等于正式 OKX_DEMO 部署 | 失败/超时 fail-closed；显示 manifest/snapshot 证据 |
| DATA-DP-01 / 部署摘要（无按钮） | RW-D 的只读投影 | 候选 `QUALIFIED` 查询；`strategy_deployments` 当前无正式读 endpoint，需在确认后优先设计 additive 只读投影 | UI 无 | `strategy_deployments(id,execution_target_id,candidate_approval_id,strategy_id,strategy_version_id,candidate_digest,instrument_id,timeframe,status,active_slot,evidence_snapshot,disabled_*,timestamps)`；只读投影还应包含可核对的 approval/strategy/version 摘要 | 仅既有自动化/唯一主任务写；容量、CI、所有权门不变；页面不得产生、批准、停用部署 | 未知≠未部署；无 qualified 显示“不排队”；无读 API 时显示“部署状态暂不可用” |

### 8.3 数据真实性规则

1. `database` 或具有可追溯 DB IDs 的 `api_aggregate` 才能计入正式指标。
2. `fixture`、`fallback`、`mock`、`unknown` 不得计入正式成功数字。
3. 页面加载时不先渲染 0；使用 skeleton/“读取中”。
4. 跨 API 聚合时，必须保留每个区块的来源和新鲜度，不用一个全局 source 覆盖所有结论。
5. 所有写操作超时后默认结果未知；先 GET/DB 对账，不自动重试。

### 8.4 必要数据库/API 演进门

#### 基本原则

1. **优先复用，但不被旧展示绑架**：先证明现有表、字段、关系和 API 投影是否能够表达正式页面需要的事实；不为了维持旧页面的技术分栏而牺牲清晰度和可追溯性。
2. **缺口先入 PRD**：任何新增/迁移必须先在“页面-区块-按钮功能清单”和“数据与数据库操作映射”增加唯一 `UI-*` / `DATA-*` 条目，说明页面问题、缺失事实和为什么现有字段不能满足。
3. **只增加必要能力**：优先新增只读聚合/投影、索引或明确关系；只有在无法可靠推导时才新增持久字段或模型。
4. **保留历史与来源**：不得把历史记录改写成当前状态，不得无证据直接 DROP 表/列、清空或覆盖旧数据；新旧值、迁移批次和来源必须可追溯。
5. **危险域单独审批**：触及 OKX_DEMO writer、订单、grant、凭据、权限、真实资金、Live、账户或执行编排时立即停止一般页面实施，另立 PRD/ADR 并请求确认。

#### 每项数据演进必须填写的条目

| 必填项 | 要求 |
| --- | --- |
| 页面问题与证据 | 受影响 `UI-*`、当前 API/DB 返回、无法表达的用户事实、复现方式 |
| 数据归属 | source of truth、owner service/repository、唯一写入者、读者和生命周期 |
| 模型/API 方案 | 新增/调整的实体、字段、关系、约束、索引、endpoint、request/response；未知项继续标待核对 |
| 字段范围 | 类型、可空性、枚举/单位、默认值、敏感级别、target scope、freshness |
| 回填方案 | 历史来源、可验证映射、批次大小、校验、无法回填时的 `unknown/null` 语义；禁止伪造默认成功 |
| API 兼容 | 旧 endpoint/字段保留期、版本或 additive 方案、客户端升级顺序、弃用通知和回滚 |
| 审计 | migration/change ID、执行者、开始/完成时间、数量、前后摘要、失败原因、证据 digest |
| 幂等与并发 | migration 幂等键、重复执行结果、锁/事务/唯一约束和中断恢复 |
| 恢复方案 | 备份/快照、可逆 migration 或 forward-fix、恢复步骤、验证和责任人 |
| 验收 | API、DB、页面三层对账；历史/空/失败/部分回填/回滚测试；性能和 secret scan |
| 依赖核对 | runtime、worker、automation、scripts、reports、tests 和外部消费者的 `rg`/调用证据 |

#### 删除或收缩字段的额外门禁

删除、重命名为不兼容含义、改变类型/枚举、收紧 nullable 或停止返回旧 API 字段，必须同时满足：

- 已通过代码、运行时配置、worker、automation、脚本、报告、测试和已知外部消费者核对，确认无未迁移依赖；
- 旧数据已完成可验证迁移，前后行数/关联/digest 对账通过；
- 已有可恢复备份或可执行的 forward-fix，且恢复演练通过；
- 兼容窗口结束并有明确弃用证据；
- 独立 PR/迁移评审通过。

任一条件未知时只允许 additive 变更或保留旧字段，不执行 DROP/破坏性 migration。

#### 实施顺序

`只读数据缺口复现` → `更新 PRD 的 UI/DATA 条目` → `确认数据 owner 与危险域边界` → `设计 additive/迁移/回填/兼容/恢复` → `用户确认` → `独立 migration/API 实现` → `API/DB/页面三层对账` → `观察兼容期` → `满足额外门禁后才考虑收缩旧字段`。

### 8.5 现有数据结构正确性与适配性只读审计（2026-08-09）

#### 审计边界、方法与结论

本次审计只读取当前 PostgreSQL catalog、`freqtrade_ai_schema_migrations`、ORM model、repository/service、FastAPI router/Pydantic DTO 和前端 API consumer。执行数据库查询时使用只读事务；未修改 schema、数据、ACL、运行时、writer、订单或凭据，也未通过启动研究或制造订单补充样本。任务所有权可见性检查完整后才开始审计。

当前 migration 账本有 24 条记录，范围为 `20260727_08` 至 `20260804_36`；数据库记录的最高 schema 版本与代码 `SCHEMA_VERSION` 均为 `20260804_36`。关键表、外键、CHECK、UNIQUE 和索引存在。总体结论不是“结构错误”，而是“核心交易链完整性约束较强，但正式页面所需的跨域只读投影和候选衔接证据仍不完整”。本轮不得据此直接改表；建议优先补只读 API/DTO，只有不能可靠推导的事实才进入第 8.4 节的数据演进门。

特别说明：本项目当前使用 `backend/app/db/migrations.py` 的事务型 Python migration 与 `freqtrade_ai_schema_migrations` 账本，不是 Alembic。研究批次/候选表是在“当前版本已为 v36”分支里用 `checkfirst=True` 添加的 v36 additive extension，账本没有单独版本号记录其安装时点。表本身已存在且约束可核对，但迁移历史不能独立证明该扩展的精确落地批次；以后新增结构应使用新的显式版本，避免继续扩大同版本漂移。

#### 结构审计表

| 实体/表 | 当前用途 | 关键字段与关系 | 页面/API 消费者 | 完整性/一致性风险 | 是否可复用 | 建议（本轮只记录） | 影响范围 | 验收证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `freqtrade_ai_schema_migrations` + `backend/app/db/migrations.py` | 记录 PostgreSQL schema 版本并以事务升级/校验 | `version UNIQUE, applied_at`；代码目标 `20260804_36`；DDL 后运行 metadata contract 检查 | db-init/db-verify 与后端启动；页面不直接消费 | 研究两表为同版本 additive extension，缺独立 migration 版本/安装时间证据 | 部分复用 | 保留现有账本；下一次结构变化使用新版本和明确 migration 证据，不回写/伪造旧历史 | 所有未来 schema/API 数据演进 | 只读查询版本历史；代码版本相等；catalog 确认研究表存在；新 migration 测试需覆盖旧 v36→新版本与重复执行 |
| `strategies` / `strategy_versions` | 正式策略目录、不可混淆的版本和源码/验证状态 | strategy `slug UNIQUE,current_version_id FK,status(draft/active/archived)`；version `strategy_id/generation_run_id/parent_version_id FK`、`(strategy_id,version) UNIQUE`、`file_path UNIQUE`、`validation_status(pending/passed/failed)` | `GET /api/strategies`、`/strategy-versions`；总览、策略工厂、详情、Local Lab | `strategies.status=active` 是目录状态，不等于 `strategy_deployments.status=ACTIVE`；若直接用于“运行中”会误报 | 是 | 保留；在 DTO/文案明确“正式策略/当前版本/目录状态”；运行中计数只读部署表 | 总览计数、策略库、详情 | API strategy/version ID 与 DB 对账；测试证明 active 目录策略未部署时运行中仍为 0/未知 |
| `strategy_research_batches` / `strategy_research_candidates` | 正式 10 条研究的持久批次、质量门结果、失败证据 | batch `run_id UNIQUE,report_digest UNIQUE,status GENERATED/VALIDATED/FAILED,六计数,policy/safety/window`；candidate `batch_id FK CASCADE,(batch_id,name)/(batch_id,digest) UNIQUE,status QUALIFIED/REJECTED/VALIDATION_FAILED` | `GET /api/strategy-research-batches`、`/strategy-research-candidates`、`/strategy-research/formal-run`；策略工厂 | 只有 batch FK；没有到 `strategies/versions/validation/approval/deployment` 的结构化关系；handoff 文案由合格数推导，不能证明已创建正式版本或部署 | 批次展示可复用；跨链关联不足 | 保留表与质量门；页面把“待部署评审”与“已部署”拆开；先核对既有自动化是否有外部 receipt，若无再提 immutable promotion linkage/receipt，不按名称/digest 猜 | 正式研究流水线、候选详情、部署交接 | 10/10/10/10/qualified/rejected 与候选行数对账；0 qualified 显示不排队；任一已部署声明可追到 strategy_version、approval、deployment ID |
| `strategy_validation_plans` / `strategy_validation_windows` | 正式策略版本的独立验证计划、窗口和提升证据 | plan 关联 `strategy_version`、promotion backtest/run/task/result，状态 `DECLARED/RUNNING/PASSED/BLOCKED`；window 关联 plan/backtest 并有唯一窗口/角色约束 | 当前未发现正式读 API/前端 consumer；服务和 repository 内部消费 | 与研究候选 `evidence_snapshot/window_evidence` 是两种证据模型；candidate 没 FK 到 plan，页面无法证明它们属于同一生命周期 | 模型可复用，页面投影缺失 | 保留；在产品文案区分“研究候选验证证据”和“正式策略独立验证计划”；如需跨链展示，先补只读 DTO/明确 linkage | 策略详情、验证证据、未来审批 | 给定 strategy_version 可查唯一计划和窗口；状态/窗口数量/引用 backtest ID 对账；没有 linkage 时显示待核对 |
| `backtest_runs` / `backtest_tasks` / `backtest_results` / `strategy_scores` | 回测执行、任务结果与版本化评分 | run/task/result FK 链；result 与 task 关联；score 关联 strategy/version/backtest result，`(strategy_version_id,scoring_version) UNIQUE` | backtest APIs、ranking API、深入证据页和 Local Lab | 排名是正式策略版本评分，不等于研究候选 `score`；混用会制造“研究候选已上榜”错觉 | 是 | 保留；UI 分开 candidate research score 与 canonical strategy score，显示 result/scoring version/source | 策略工厂排行榜、详情、回测证据 | 排名行可追到 score→result→task/run→version；缺链时不得进入正式榜 |
| `full_chain_runs` / `full_chain_stage_runs` | 从研究到 reconciliation 的阶段化 lineage 与幂等执行记录 | run 保存各阶段关键 ID、status/error/timestamps；stage 对 run/stage 与 idempotency 设唯一约束 | full-chain repository/services；observability lineage 摘要 | 这是 canonical promotion/execution chain，不自动包含 `strategy_research_candidate.id`；“formal research handoff”跨链是否写入本表待核对 | 是 | 保留；页面只展示有 ID 的 lineage；如补研究关联，优先不可变外键/receipt 并保留旧行 unknown | 策略详情、部署详情、模拟盘证据 | 任一订单/部署可沿 run/stage 追溯到 version/backtest/score/approval；断链明确标未知 |
| `strategy_candidate_approvals` / `strategy_deployments` | 人工批准精确的正式策略版本，并绑定 OKX_DEMO 部署 | approval 关联 full chain/version/backtest result/score，状态 `PENDING/APPROVED/REJECTED/EXPIRED/REVOKED`；deployment 关联 approval/strategy/version，target CHECK=`OKX_DEMO`，状态 `ACTIVE/DISABLED`，active slot 1–3 partial UNIQUE | repository/services；当前未发现正式 GET DTO/前端 consumer | DB 能表达运行中策略，但页面/API 无投影；若拿 qualified candidate 或 strategy active 替代会误报。deployment 删除/禁用是运行时危险域 | 结构可复用，只读 API 缺失 | 保留 schema/唯一 writer；确认后优先新增 additive read-only deployment summary DTO/endpoint，不新增写按钮、不改变 writer | 总览运行中计数、策略详情、模拟盘部署摘要 | DTO 的 ACTIVE 数与 DB 按 target/status 对账；每行可追到 approval/version；DISABLED 不计运行中；写路径测试零变化 |
| `signal_evaluations` / `full_chain_signal_snapshots` | 每个 active deployment、已闭合 candle 的耐久评估，以及与批准候选绑定的不可变信号证据 | evaluation `deployment_id FK CASCADE,target CHECK OKX_DEMO,(deployment,instrument,timeframe,candle) UNIQUE`；状态 `PENDING/LEASED/NO_ACTION/ACTIONABLE/BLOCKED/FAILED`；lease/fencing 约束与索引；snapshot 唯一 run/digest、有过期时间 | execution orchestrator/repository；observability 有 lineage/snapshot 摘要但不提供 evaluation 列表 | 当前无正式读 API/DTO 展示最近 NO_ACTION/ACTIONABLE/失败；仅看订单无法解释“无订单” | 是，需只读投影 | 保留；确认后设计最近信号只读 DTO，字段最小化并带 evaluation/deployment IDs、状态、candle、原因、freshness；不暴露 writer 控制 | 总览最近信号、模拟盘活动 | DTO 行与 evaluation 唯一键/状态对账；NO_ACTION 与 API 失败/无记录三态测试；LEASED 细节默认技术详情 |
| `trade_intents` / `risk_decisions` / `approved_executions` | 信号后的意图、风险裁决与 writer 前批准执行 | 通过 target、intent、budget/snapshot/grant 等 FK 和 UNIQUE/CHECK 绑定；多个约束固定 `OKX_DEMO`；approved execution 不等于交易所已接受订单 | execution services、observability allowlist | 状态语义跨阶段，若 UI 把 approved 当 order success 会误导；属于 writer/订单危险域 | 是，仅只读 | 保留不动；页面用“意图→风控→批准执行”分层，不新增 POST/重试/重放 | 模拟盘最近活动、阻断原因 | 任一展示行含 lineage/database ID；risk rejected 不生成订单；approved 无 exchange order 时明确“尚未证明下单” |
| `exchange_orders` / `exchange_fills` / `exchange_positions` | OKX_DEMO 订单、成交和权威仓位快照 | order target+client/exchange order UNIQUE，intent FK；fill target+fill UNIQUE、order FK CASCADE；position target+instrument+side UNIQUE；target 均 CHECK `OKX_DEMO` | `/api/okx-demo/observability` orders/positions；writer/reconciliation 服务 | 订单 status 为外部状态字符串，缺统一 DB CHECK；accepted/存在 order/fill/position 是不同事实。API 超时后不能自动重试 | 是，仅只读 | 保留；页面依次显示订单、成交、仓位/对账证据；状态规范化只在 DTO 展示层，原始值收起；本任务不改订单枚举或写入 | 模拟盘订单/成交/仓位 | order ID、client ID、fill IDs、数量和状态与 observability/DB 对账；无 fill 不显示订单失败；unknown historical order 不重放 |
| `reconciliation_runs` / `okx_demo_reconciliation_states` | 本地与交易所权威状态对账、冻结和恢复依据 | run target CHECK `OKX_DEMO`，状态 `RECONCILED/DRIFTED/STALE/UNKNOWN/RECOVERED`，artifact `PENDING/READY`，摘要/DB IDs/digest/timestamps；state 每 target 唯一 | observability latest reconciliation、运行时服务 | `RECOVERED`/`RECONCILED` 需要 freshness；API 缺失不能当无差异；artifact 尚未 READY 时证据不完整 | 是 | 保留；首屏显示短结论+时间，技术详情展示 source/core/artifact；未知/过期保持 fail-closed | 模拟盘健康、订单终态、未来上线检查 | latest run target/status/freshness 与 state 对账；DRIFTED/STALE/UNKNOWN 明确阻断；artifact pending 不显示证据完整 |
| `execution_scopes` 与未来 Live 域 | 给执行链提供 target scope 基础实体 | `scope_id` 被多个 FK 引用；但 deployment、signal、approval、intent/order/fill/reconciliation 等下游 CHECK 明确固定 `OKX_DEMO` | 当前 OKX_DEMO 服务；未来 Live 尚无可信 API/DTO/表映射 | 不能因存在通用 scope 表就认为当前 schema 支持 Live；直接放宽 CHECK 会跨越凭据、writer、ACL、审计和资金边界 | OKX_DEMO 可复用；Live 不可直接复用 | 保留现状；未来按第 14 节另立控制面/数据域设计，所有表/API/ACL 先标待核对，禁止把 target 改成开关 | Demo-only 标签、未来 readiness 只读状态 | 当前所有危险链 target 仍为 OKX_DEMO；无 Live 按钮/凭据读取；未来专项证明隔离后才设计 migration |
| FastAPI DTO / 前端 data loader | 将 DB/服务事实投影到正式页面并标 source/freshness | research DTO 完整暴露 batch/candidate；observability DTO 暴露 readiness/intents/orders/positions/reconciliation/lineage；MVP loader 还存在 mock/fallback 类型但会标 source | Dashboard、Strategies、OKX Demo、Ranking、Local Lab | 缺 deployment/signal read DTO；旧 loader 可返回 fixture/mock，若未按 core source 过滤会污染正式数字；部分 frontend path 由 base URL 加 `/api`，文档需写最终路由语义 | 部分复用 | 正式页只消费 real/core allowlist；新增投影优先 additive endpoint/字段；为每个区块保留 source/freshness/error，不做全局假正常 | 三个正式入口及所有状态 | contract tests 覆盖 DTO 字段和 source；fixture/mock 不计入正式数字；API 失败显示未知；页面计数与 DB 对账 |

#### 状态转换与页面语义核对

| 生命周期 | 数据库允许状态 | 已确认转换/终态语义 | 页面规则 |
| --- | --- | --- | --- |
| 正式研究批次 | `GENERATED / VALIDATED / FAILED` | 完整报告持久化为 `VALIDATED`；预生成或验证流水线失败可持久化 `FAILED`；同一 run/digest 幂等返回原记录 | `FAILED + generated=0` 是未生成；`VALIDATED + qualified=0` 是完成但不排队 |
| 研究候选 | `QUALIFIED / REJECTED / VALIDATION_FAILED` | 正常质量门形成 qualified/rejected；流水线未完成但已有候选证据时保留 validation_failed | 三种终态分开；`VALIDATION_FAILED` 不并入质量拒绝，也不进入部署评审 |
| 正式版本验证 | plan `DECLARED/RUNNING/PASSED/BLOCKED`；window `DECLARED/READY/PASSED/BLOCKED` | 由 validation service/repository 管理，独立于 research candidate JSON | 没有 plan linkage 时不得显示“正式独立验证已通过” |
| 候选批准 | `PENDING / APPROVED / REJECTED / EXPIRED / REVOKED` | 批准绑定精确 version/result/score/digest 且有过期时间 | “合格候选”不等于“已批准”；过期/撤销不显示有效 |
| 部署 | `ACTIVE / DISABLED` | ACTIVE 有 1–3 slot；DISABLED 必须有 time/reason 且 slot 为空 | 只有 target=`OKX_DEMO` 且 status=`ACTIVE` 计入运行中 |
| 信号评估 | `PENDING / LEASED / NO_ACTION / ACTIONABLE / BLOCKED / FAILED` | LEASED 受唯一 consumer、expiry、heartbeat、fencing 约束；其余为无 lease 状态 | `NO_ACTION` 是正常终态；PENDING/LEASED 不提前显示为无信号；失败与 API 未知分开 |
| 对账 | `RECONCILED / DRIFTED / STALE / UNKNOWN / RECOVERED` | 还需结合 authoritative time 与 artifact readiness | 状态和新鲜度同时展示；未知/过期不显示健康 |

#### 正式页面指标的唯一来源契约

| 页面指标 | 唯一可信来源 | 禁止替代 | 当前适配结论 |
| --- | --- | --- | --- |
| 正式策略数 | `strategies` 中满足产品定义的正式目录行；定义需在实现前固定是否包含 archived | strategy research candidate 数、文件数量 | API 已有；需固定过滤语义 |
| 正式候选/合格/拒绝数 | `strategy_research_batches` 最新或选定批次 + 其 candidates | Local Lab 生成数、ranking 行数 | API 已有，可复用 |
| 运行中策略数 | `strategy_deployments` where target=`OKX_DEMO`, status=`ACTIVE` | `strategies.status=active`、qualified 数、allowlist 容量 | DB 可表达，正式读 API 缺失 |
| 最近信号 | `signal_evaluations`，必要时关联 deployment/approval/version | 最近订单、snapshot 存在与否 | DB 可表达，正式读 API 缺失 |
| 最近订单/成交/仓位 | observability allowlist 对应 `exchange_orders/fills/positions` | approved execution、accepted 文案、前端 mock | API 已有，可复用并需保留 source |
| 最新对账 | observability latest reconciliation 对应 `reconciliation_runs/state` | runtime HTTP 200、订单存在 | API 已有，可复用并需 freshness |
| Live 准备/批准 | 未来独立控制面与审计域 | `execution_scopes` 中出现名称、Demo 批准、ENV presence | 当前不存在；只定义文案，所有映射待核对 |

#### 按优先级处理的数据适配缺口

1. **P0：补部署与信号的只读事实投影（需 PRD 确认后另做）**。先评估在既有 observability DTO 中增加只读摘要，或建立专用 GET endpoint；不得新增写 API，也不改变 repository/writer。没有该投影前，首屏对应区块必须显示“暂不可用/未知”。
2. **P0：消除“active”的双重语义**。正式策略目录 active 与 OKX_DEMO deployment ACTIVE 必须用不同中文标签和不同计数来源，并加入 contract/UI tests。
3. **P1：核对 formal research → canonical strategy promotion 的唯一 receipt**。当前数据库关系不能证明该跨链交接；先核查自动化/报告是否已经保存不可变映射。只有确证没有可靠来源，才按 8.4 节提出 additive linkage/receipt、回填和兼容方案。
4. **P1：分开两类验证证据**。研究批次的 candidate/window JSON 与正式 strategy validation plan/window 不得在 UI 合并成同一“验证已通过”。若要统一展示，必须先定义 lineage。
5. **P1：后续结构变化使用新 migration 版本**。不要继续向已记录的 `20260804_36` 静默添加表/列；保留现有历史，不伪造旧 migration 行。
6. **P1：Live 不能通过放宽 target CHECK 适配**。当前订单与部署域有意固定 OKX_DEMO；未来 Live 需独立 threat model、writer/credential/ACL/audit 设计和专项批准。
7. **P2：索引只按真实查询证据增加**。研究批次按时间排序、候选按状态过滤目前没有专用 catalog index；实现分页后以 `EXPLAIN (ANALYZE, BUFFERS)` 和规模基线判断，不能因页面需求臆测加索引。

#### 本次只读审计证据清单

- PostgreSQL：在 `BEGIN TRANSACTION READ ONLY` 中读取 current database/schema、migration version、`information_schema.columns/table_constraints/key_column_usage` 和 `pg_indexes`，事务结束无写入；
- migration：核对 `SCHEMA_VERSION`、升级分支、研究表 additive extension 和 `schema_problems` 的预期 metadata；本次未执行 db-init/db-verify；
- backend：核对 strategy/research/validation/score/full-chain/deployment/signal/execution/reconciliation ORM、repository/service 与 FastAPI/Pydantic 暴露范围；
- frontend：核对 Dashboard、Strategies、Ranking、OKX Demo、Local Strategy Lab 的 loader、source marker 和正式研究 API consumer；
- 证据限制：未用页面样本反推 schema，未读取/打印凭据，未触发正式研究，未读取或重放未知历史订单。暂不能由关系/DTO证明的内容统一标为“待核对”。

## 9. 状态与空状态文案规范

| 业务状态 | 首屏短标签 | 必须解释 | 禁止文案 |
| --- | --- | --- | --- |
| API/来源失败 | 未知 | “无法确认当前状态，不代表没有记录” | “0 条”“空闲” |
| 未启动研究 | 未开始 | “尚无持久化批次” | “10 条已拒绝” |
| 研究运行中 | 运行中 | 当前阶段、开始时间、最新计数 | “成功” |
| 预生成失败 | 已阻断/失败 | `generated=0` 与原因 | “候选验证失败” |
| 完整验证、0 合格 | 已完成·无合格 | 完整六计数和“不进入部署队列” | “研究失败” |
| 有合格候选 | 待部署评审 | qualified 数和现有交接状态 | “已部署” |
| 无信号 | 正常·等待信号 | 最近评估时间与自然 NO_ACTION | “系统未运行” |
| 有信号但被风控阻断 | 已阻断 | 风控原因和证据 ID | “无订单”作为唯一解释 |
| 订单无成交 | 订单处理中/未成交/终态 | 订单状态与对账结论 | “下单成功”仅凭 accepted |
| 当前仅配置 Demo | Demo-only | `OKX_DEMO` 与 Live 域严格隔离；没有 Live 切换能力 | “可一键切换” |
| 未建立 Live 独立域 | Live 未配置 | 未配置不等于故障，也不读取凭据确认 | “Live 可用” |
| Live 检查未全部完成 | Live 准备中 | 显示通过/阻断/未知检查、责任人角色和过期时间 | “即将自动上线” |
| 有完整且有效的显式人工批准证据 | Live 已获人工批准 | 仅代表批准记录有效；不代表已启动、已下单或真实资金已暴露 | “Live 已运行” |
| Live 准备证据读取失败 | 未知 | 无法确认配置或批准状态，保持 Demo-only | “Live 未配置”或“已批准” |

## 10. 当前区块删除、收起与迁移方案

| 当前内容 | 方案 | 目标位置/原因 |
| --- | --- | --- |
| Dashboard 旧 MVP 计数与流程文字 | 替换 | 换成正式生命周期和 Demo 摘要 |
| 生成批次一级导航 | 移除一级入口，保留路由 | 策略工厂“历史批次/技术证据” |
| 回测批次、回测任务一级导航 | 移除一级入口，保留路由 | 候选/策略详情的验证证据链接 |
| 排行榜一级导航 | 合并 | 策略工厂页签/分区 |
| Hyperopt 一级导航 | 迁移到高级/历史 | 不属于当前正式必经路径 |
| 实盘候选治理 | 改名并迁移 | “候选治理证据（只读/未来能力）”，避免真实资金暗示 |
| 运维面板 | 迁移到开发实验 | 正式首屏只显示简短健康结论 |
| Local Strategy Lab | 迁移且默认收起 | 明确非正式实验 |
| 大段质量契约、ID、路径、JSON | 首屏摘要 + details | 业务结论优先，证据仍可审计 |
| Copy 按钮 | 仅保留技术详情 | 降低首屏噪音 |

## 11. 分阶段实施方案

### 阶段 0：PRD 确认（当前）

- 完成页面/信息架构只读审查、长期基线、按钮与数据映射；
- 完成第 8.5 节 PostgreSQL schema、migration、关系/约束、状态、DTO 与前端消费者只读审计；
- 将“运行中策略/最近信号只读 API 缺口”“active 双重语义”“研究候选到正式策略衔接待核对”等发现作为后续设计输入，不在本阶段修复；
- 用户确认范围、导航、页面职责和按钮处理；
- 不改页面代码、schema、数据、运行时或订单链路。

### 阶段 0.5：视觉原型与截图评审门

仅在用户确认 PRD 后开始。先在真实前端路由中完成三个正式入口的可看原型，不扩展后端能力，不触发研究、运行时或订单写入：

1. 使用现有真实只读 API 和组件状态；不得用 mock/fixture 制造成功截图；
2. 主写按钮保持既有门禁，视觉核验不得点击“手动运行一轮研究（10 条）”；
3. 至少核验 1440×900、1024×768、390×844 三种视口；
4. 覆盖浅色、深色（若现有产品支持）、加载、空、失败/未知和有真实数据状态；
5. 在真实页面中检查键盘焦点、窄屏溢出、表格/详情展开、状态对比度和骨架稳定性；
6. 提交总览、策略工厂、模拟盘的首屏及必要展开态截图，并附对应 `UI-*` / `DATA-*` ID；
7. 用户明确确认视觉原型后，才进入阶段 1 的完整实现和其余页面扩展。

视觉原型评审未通过时，只迭代布局、样式、文案和只读展示；不得借机新增 API、schema、ACL、Live 控制、运行时控制或订单能力。

### 阶段 1：导航与总览最小改造

- 一级正式导航降为总览/策略工厂/模拟盘；
- 开发实验默认收起；
- 总览接真实现有 API，区块级 fail-closed；
- 优先复用现有 API；若证据证明不足，先按第 8.4 节更新 PRD 并确认，再以独立、可回滚变更补充必要的非危险域字段或只读投影；
- 不新增研究以外的写 API，不触及 OKX_DEMO writer、订单、权限、凭据或真实资金域。

### 阶段 2：策略工厂信息层级

- 正式研究控制简化；
- 最新批次流水线、六计数、候选拒绝原因、策略库、排行榜整合；
- 保留唯一手动研究按钮及现有门禁。

### 阶段 3：模拟盘摘要与最近活动

- 基于现有 observability allowlist 展示风控、订单/成交/对账；运行中策略和最近 signal evaluation 只有在用户确认后，按第 8.5 节补充最小 additive 只读投影才能显示真实值；
- 若只读投影未完成或来源失败，对应区块显示“未知/暂不可用”，不以策略目录 active、qualified 数或订单倒推；
- 不增加任何订单/运行控制。

### 阶段 4：深入证据页降噪与兼容

- 保留旧路由；统一“结论 → 原因 → 下一步 → 证据”；
- 增加旧链接回到正式上下文的导航；
- Local Lab 强化开发实验视觉标识。

每阶段独立测试、截图核对和 PR 审查；若需要新 API 或字段，先更新本 PRD 并再次确认，不猜测数据库映射。

## 12. 验收标准

### 12.1 信息架构

- [ ] 打开总览 10 秒内能找到项目状态、四类数量、最新研究、部署、Demo、最近信号/订单；
- [ ] 一级正式导航只有总览、策略工厂、模拟盘；
- [ ] Local Strategy Lab 位于默认收起的开发实验分组，并持续显示“非正式候选”；
- [ ] 旧路由可访问，书签不失效。

### 12.2 生命周期准确性

- [ ] 生成、验证、入库、合格、拒绝、部署交接逐步展示且计数可对账；
- [ ] `generated=0` 不显示为候选拒绝；
- [ ] 完整验证但 0 qualified 显示为研究完成、不排队；
- [ ] 只有 `status=QUALIFIED` 能显示进入既有部署评审；
- [ ] Local Lab 数据不计入正式候选/正式策略数字。

### 12.3 状态与错误

- [ ] 加载中不显示 0；失败/超时显示未知；
- [ ] 每个区块有成功、加载、失败、无数据状态；
- [ ] 拒绝/阻断显示简短原因，可展开完整证据；
- [ ] 无信号、无订单、无成交分别解释。

### 12.4 安全边界

- [ ] 全局持续显示 OKX_DEMO；
- [ ] 页面不存在真实资金/live 切换、grant、手动下单、重放或运行时启停入口；
- [ ] 不显示/存储凭据或 ENV 值；
- [ ] 未经第 8.4 节缺口证据、用户确认和独立 migration/API 评审，不变更 schema；ACL、策略阈值、风控、writer、下单逻辑始终不在本任务变更范围；
- [ ] 无 mock/fixture/fallback 伪装真实成功。
- [ ] Demo 与 Live 状态、凭据、数据、审计和批准证据不得复用或直接切换；
- [ ] 即使显示“Live 已获人工批准”，页面也不存在真实资金启动、真实下单或一键切换操作；
- [ ] Live 状态来源未知、过期或不一致时保持 `Demo-only` 并显示未知/阻断。

### 12.5 可追溯性与测试

- [ ] 每个实现改动引用至少一个 `UI-*` 和 `DATA-*` ID；
- [ ] 前端 unit/build 与相关 Playwright 路由/空状态测试通过；
- [ ] 现有 strategy research API/backend 测试通过；
- [ ] `git diff --check` 与 secret scan 通过；
- [ ] 浏览器核对真实、空、加载、失败四类状态；
- [ ] 不启动/停止运行时，不触发正式研究或订单来“制造”截图数据。

### 12.6 视觉与交互质量

- [ ] 三个正式入口符合第 4.8 节文字线框，首屏主结论和下一步无需寻找；
- [ ] 桌面 1440×900、平板 1024×768、窄屏 390×844 均无页面级水平溢出、遮挡或截断；
- [ ] 字号、间距、栅格、卡片密度、按钮和图标符合第 4.3–4.7 节，不出现随意例外；
- [ ] 同一视图最多一个高强调主按钮，危险/真实资金操作不存在；
- [ ] 状态颜色语义一致，同时有短标签和文字/图标，关闭颜色感知也能判断；
- [ ] 正文与背景达到 4.5:1、非文本边界和大字号达到 3:1；键盘焦点清晰；
- [ ] 技术证据、完整拒绝信息、路径、ID、digest 和 JSON 默认收起，业务结论不被淹没；
- [ ] 表格数字对齐、单位一致、行高不低于 48px，长内容可展开，窄屏有明确降级方案；
- [ ] 骨架与最终布局一致，加载不先显示 0/成功，刷新不产生明显布局跳动；
- [ ] 深浅主题使用同一语义 token，`prefers-reduced-motion` 下无非必要动效；
- [ ] 视觉原型截图经过用户明确确认后才进入阶段 1。

### 12.7 数据演进与兼容

- [ ] 每个新增/迁移字段或 API 都有对应 `UI-*` / `DATA-*`、数据 owner、唯一写入者和缺口证据；
- [ ] 回填不制造默认成功；无法可靠回填的数据使用明确的 `unknown/null` 语义；
- [ ] additive、兼容、弃用和客户端升级顺序有文档与测试；
- [ ] migration 可重复执行或明确 fail-safe，具有事务/锁、审计计数和中断恢复证据；
- [ ] API、数据库和页面按 ID/数量/状态完成三层对账，历史记录仍可追溯；
- [ ] 删除/收缩字段前已确认无运行时依赖、完成迁移与兼容窗口，并验证可恢复方案；
- [ ] 未经单独确认，不存在涉及 OKX_DEMO writer、订单、grant、权限、凭据、真实资金或 Live 控制面的 schema/API 变化。

### 12.8 结构审计与数据适配

- [x] PostgreSQL catalog、migration 账本、ORM 关系/约束、API DTO 与前端消费者完成只读核对，结果写入第 8.5 节；
- [x] 正式策略、研究批次/候选、验证、评分、批准/部署、ACTIVE、信号、意图/风控、订单、成交、仓位和对账均有结构审计条目；
- [x] 未来 Demo→Live 明确为独立域；当前 target CHECK 固定 OKX_DEMO 的事实已记录，不提出放宽约束；
- [ ] 实现前为每个正式指标锁定唯一 source of truth，并对 active/qualified/approved/deployed/running 等相近状态做 contract test；
- [ ] `strategy_deployments` 与 `signal_evaluations` 的最小只读投影方案经用户确认，或页面明确接受持续显示未知；
- [ ] formal research candidate 到 canonical strategy/version/approval/deployment 的 receipt 或缺失结论完成专项核对，任何历史回填均有可验证来源；
- [ ] 新 migration 使用新版本号并覆盖旧 v36 升级、重复执行、失败回滚和 schema contract；不伪造现有 migration 历史；
- [ ] 查询性能改动有真实分页查询和 `EXPLAIN` 证据；没有证据不增加索引。

## 13. 待用户确认的产品决策

1. 是否同意一级正式导航精简为“总览 / 策略工厂 / 模拟盘”？
2. 是否同意排行榜合并到策略工厂，旧 `/ranking` 路由继续保留？
3. 是否同意生成批次、回测批次、回测任务、Hyperopt、运维证据、治理证据从一级导航移除但保留路由？
4. 是否同意把“实盘候选治理”改名为“候选治理证据（只读/未来能力）”？
5. 是否同意后续实现按阶段 1→4 分 PR 或至少分可独立审查的 commits？
6. 是否同意第 14 节只作为未来受控上线的设计基线，本轮页面最多展示只读准备状态，绝不实现 Live 控制面？
7. 是否同意先执行阶段 0.5 的三个真实页面视觉原型/截图评审，确认后再扩展完整功能？
8. 是否同意采纳第 8.5 节审计结论：下一阶段先设计最小只读 deployment/signal 投影，并专项核对正式研究候选到 canonical strategy 的可审计 receipt；在这些事实可证明前，页面显示未知而不是推断？
9. 是否同意第 15 节的任务职责、共享 coordinator、数据独立性和错峰原则作为未来 automation 变更基线？具体时刻仍须在每次实施前只读复核并单独确认。

状态说明：用户已确认 v0.3 并授权阶段 0.5；本次 v0.4 补充按最新要求仅更新文档，页面原型实施暂不继续。恢复阶段 0.5 或调整 automation 均等待后续明确指令；本章不构成调度变更授权。

## 14. 从 OKX_DEMO 迁移到真实盘的受控上线流程（未来设计基线）

### 14.1 本章边界

本章只定义未来如何理解、审查和展示 Live 上线准备，不授权或实现任何真实资金能力。本任务以及基于本 PRD 的当前页面改造不得：

- 创建、启用或切换到 Live execution target；
- 读取、验证、保存或展示 Live API key、secret、passphrase；
- 提交真实订单、创建真实 grant、启动 Live writer 或修改运行时；
- 新增“一键切换到 Live”“开始真实交易”“批准并启动”等按钮；
- 修改 schema、migration、ACL、订单链路或现有 OKX_DEMO 风控。

任何未来 Live 实现都必须先有新的范围明确的设计文档、威胁建模、数据/API/schema 评审、独立 Issue/PR、人工授权和验收。本章不是实现许可。

### 14.2 Demo 与 Live 的严格隔离模型

`OKX_DEMO` 与未来 Live 必须被视为两个独立系统域，而不是同一配置中的布尔开关：

| 隔离面 | OKX_DEMO | 未来 Live | 不允许 |
| --- | --- | --- | --- |
| Execution target | 固定 `OKX_DEMO` | 独立 target ID，名称和契约待未来设计 | 原地改 target、复用 manifest |
| 凭据域 | Demo 专用凭据与 fingerprint | Live 专用 vault/账户/权限边界，具体方案待核对 | 共用 key/secret/passphrase、从页面复制迁移 |
| 账户与资金域 | 模拟账户、模拟资产 | 独立真实账户和明确资金预算 | 用 Demo balance/position 证明 Live 状态 |
| 数据域 | Demo signal/intent/risk/order/fill/reconciliation | Live 同类实体必须有独立 target/account scope；具体 schema 待核对 | 混表后仅靠前端过滤、复用 DB IDs |
| Writer/lease 域 | 既有 OKX_DEMO 唯一 writer | 独立 Live writer、fencing、lease 和 kill boundary，未来设计 | 同一 writer 动态切换 target |
| 审计域 | Demo 批次、批准、grant、订单与对账审计 | Live 独立变更、批准、暴露、订单、停止和回滚审计 | Demo 批准/grant 继承到 Live |
| 幂等域 | Demo idempotency keys | Live 必须带独立 target/account/change scope | 跨 target 重用 request/order key |

默认规则：只要 Live 独立域的任一身份、凭据 presence、权限、数据源、审计或 writer 状态无法确认，系统保持 `Demo-only`。不存在从 Demo “直接切换”到 Live 的正常路径；未来只能创建并逐阶段批准一个新的 Live rollout。

### 14.3 迁移前置检查清单

以下检查必须全部有新鲜、可追溯、与精确 strategy version / target / account scope 绑定的证据。具体持续时间、阈值、角色名称和 API/表字段均待未来专项评审，本 PRD 不猜测。

| 检查域 | 最低问题 | 必须证据 | 失败处理 |
| --- | --- | --- | --- |
| 策略持续表现 | 是否在足够长的 Demo 观察窗保持稳定，而非单次高收益？ | 多窗口表现、交易数、回撤、费用后收益、异常期记录 | 阻断，不降低门槛 |
| 独立验证 | 是否通过独立 OOS、walk-forward、bull/range/bear、lookahead、费用和滑点验证？ | 不可变报告 digest、代码/参数/数据版本 | 任一缺失或漂移即重新验证 |
| 运行健康 | runtime、writer、lease、时钟、数据新鲜度和告警是否稳定？ | 健康窗口、事件、故障恢复记录 | 未达标保持 Demo-only |
| 订单与对账 | Demo intent→risk→order→fill→position→reconciliation 是否持续闭环？ | 完整 lineage、未知订单统计、差异与终态 | 未知/未对账记录为 0 才可继续，阈值待评审 |
| 风险限额 | Live 最大资金、单笔/单日损失、仓位、频率、品种和总暴露是否冻结？ | 版本化风险预算及 hash | 任何缺省值都阻断 |
| 账户/权限 | Live 账户、地区/合规、API 权限、提币禁用和 IP/网络边界是否经独立核对？ | 只展示 fingerprint、presence 和权限摘要，不含密钥 | 权限过宽、身份变化或来源未知即阻断 |
| 人工审批 | 是否由规定角色审阅精确版本、风险预算和窗口？ | 审批人角色、时间、对象 digest、范围、过期时间、决定和理由 | 缺一项、过期或对象变化即无效 |
| 回滚演练 | 是否在无真实资金或影子环境演练停止新意图、冻结 writer、处理已知订单和人工接管？ | 演练批次、步骤、结果、未解决事项 | 演练未通过不得批准最小暴露 |
| 可观测与值守 | 告警、审计、升级联系人、人工停止路径是否可用？ | 通知演练、值守确认、只读 dashboard 证据 | 无人在环或告警不可靠即阻断 |
| 变更冻结 | 策略代码、参数、依赖、target、账户与风险配置是否与审批对象完全一致？ | commit/digest/manifest 对账 | 任一变化使批准失效，从对应阶段重来 |

### 14.4 分阶段人工确认与一次性批准

未来 rollout 至少需要明确的人工作业分段；优先采用多人职责分离。若组织规模暂不能多人，也必须由同一操作者在不同阶段进行显式、不可继承、带理由的人工确认，且页面浏览/展开证据不算确认。

建议阶段：

1. **范围确认**：冻结 strategy version、Live account fingerprint、风险预算、允许品种、时间窗和回滚负责人。
2. **证据复核**：研究/风险复核者确认独立验证、Demo 持续表现和质量门。
3. **运行复核**：运维/安全复核者确认隔离域、权限、可观测、对账和回滚演练。
4. **影子阶段批准**：一次性批准只读/影子验证，不允许真实订单。
5. **最小暴露批准**：仅在未来独立实现并通过专项验收后，针对精确预算和时间窗签发另一份一次性批准；不得由影子批准升级或复用。
6. **逐级扩大复核**：每次扩大品种、资金、频率或时间都视为新变更，重新审批，不允许自动晋级。

每份批准必须绑定：execution target、account fingerprint、strategy/version/code digest、risk policy digest、允许阶段、最大暴露、起止时间、批准角色、批准理由、前置证据 digest、rollback plan digest 和唯一 change/rollout ID。任何对象变化、过期、撤销或审计缺口都使批准失效。

批准记录只是未来控制面可消费的证据，不是订单授权本身。UI 只能读取和展示摘要，不得创建、延长、复制、重放或消费批准。

### 14.5 渐进上线阶段

| 阶段 | 允许能力 | 页面状态 | 晋级条件 | 本任务是否实现 |
| --- | --- | --- | --- | --- |
| L0 Demo-only | 仅现有 OKX_DEMO | `Demo-only` | 无 | 保持现状 |
| L1 Live 未配置 | 仅有未来规划，无 Live 域/凭据 | `Live 未配置` | 独立设计和安全评审 | 只定义文案 |
| L2 Live 准备中 | 建立隔离域和只读 readiness；不得真实下单 | `Live 准备中` | 前置检查、审计和职责确认完整 | 只定义只读状态；API 待核对 |
| L3 影子/只读 | 读取允许的市场/账户摘要或镜像信号，对比理论执行；零真实订单、零真实资金暴露 | `Live 准备中 · 影子验证` | 影子差异、延迟、风控和对账达到未来阈值 | 不实现 |
| L4 已获人工批准 | 存在有效的最小暴露一次性批准证据，但尚未开始 | `Live 已获人工批准` | 未来独立的启动控制面再次核对全部条件 | 只定义只读状态，不提供按钮 |
| L5 最小暴露 | 未来限定单一策略/品种、极小预算、短窗口的可撤销 canary | 本 PRD 不定义“运行中”页面行为 | 独立实施/验收/值守授权 | 不实现 |
| L6 逐步扩大 | 每次扩大均为新 rollout | 本 PRD 不定义 | 新审批、新预算、新验收 | 不实现 |

不允许从 L0/L1/L2 直接跳到 L4/L5；不允许依据 Demo 盈利、单次回测、一个人的口头确认或页面状态自动晋级。

### 14.6 停止条件、熔断与回滚

任何未来 Live 阶段出现以下任一情况都必须停止新意图/新暴露并 fail closed；具体自动动作必须在未来订单链路专项设计中确定，本任务不实现：

- execution target、account fingerprint、strategy/version、policy digest 或批准对象不一致；
- 批准缺失、过期、撤销、范围超出或无法读取；
- writer 唯一性、lease/fencing、时钟、数据新鲜度或审计链不确定；
- 风险预算、仓位、损失、频率、品种或时间窗越界；
- 出现未知订单、重复提交嫌疑、accepted 但找不到订单、异常仓位或余额差异；
- reconciliation 连续失败、权威交易所状态与本地状态不一致；
- 凭据权限、账户状态、网络出口或交易所规则发生变化；
- 告警、值守、日志、审计或回滚负责人不可用；
- 策略行为、信号分布、滑点、成交质量或风险指标显著偏离已批准基线；
- 人工发出停止/撤销决定。

回滚原则：

1. 先阻止新意图和新暴露，冻结变更范围并保存审计快照；
2. 只处理身份明确、权威状态已确认的订单/仓位；未知历史订单不得猜测、重放或自动补单；
3. 取消已知订单、reduce-only 降低已知仓位等动作的授权与顺序由未来专项设计决定，不由本 PRD授权；
4. 保留变更、批准、停止原因、人工决策、订单/成交/仓位和 reconciliation 证据；
5. 回到 `Demo-only` 不等于问题已解决；必须完成事后复盘和新的 rollout 才能再次准备 Live。

以下情形禁止自动恢复，必须人工调查并签发新的批准：未知订单或仓位、重复/幂等状态不确定、审计缺口、账户/权限变化、凭据疑似泄露、target 身份漂移、未完成对账、风险越界、批准过期/撤销、回滚步骤未完成或外部交易所状态不可确认。

### 14.7 UI 只读状态与证据要求

正式 UI 未来最多显示以下状态；这些是展示语义，不是当前后端枚举：

- `Demo-only`：唯一可确认的 active execution target 是 `OKX_DEMO`；
- `Live 未配置`：没有可验证的独立 Live 准备域；
- `Live 准备中`：存在准备记录，但检查未全部通过、证据不完整、已过期或仍处于影子阶段；
- `Live 已获人工批准`：存在与精确对象绑定且未过期的批准摘要；**不表示 Live 已启动或真实订单已获授权**；
- `未知`：Live 只读状态源读取失败或证据相互矛盾；页面仍保持 Demo-only。

允许展示：target ID、account fingerprint、strategy/version digest、阶段、检查通过/阻断/未知计数、批准角色（不展示不必要的个人敏感信息）、批准时间/过期时间、最大批准暴露摘要、change ID、evidence/rollback/audit refs、最后核对时间。

禁止展示或操作：任何 secret/passphrase/token、完整账户敏感信息、批准创建/续期/撤销控件、Live target 选择器、真实资金金额输入、启动/停止 Live bot、下单、补单、重放、grant、writer lease 操作。

### 14.8 数据库、API 与审计设计要求（全部待专项核对）

本章不指定或修改 schema。未来设计至少要回答以下问题，未确认前一律标“待核对”：

- execution target、账户 fingerprint、rollout/change、检查结果、批准、风险预算、影子比较、熔断、停止和回滚分别由哪些实体持久化；
- Demo 与 Live 如何在数据库、连接、schema 或强制 target scope 上隔离，不能只依赖前端过滤；
- 只读 readiness API 的 endpoint、response schema、字段 allowlist、新鲜度和认证方式；
- 哪个独立服务是批准/rollout/Live writer 的唯一写入者，ACL 如何阻止 Web UI 写入；
- 每次批准和执行如何绑定不可变 digest、过期时间、一次性消费状态和撤销状态；
- 幂等键如何包含 target/account/rollout/strategy/risk scope，并防止跨 Demo/Live 复用；
- 如何记录 actor role、decision、reason、before/after、timestamp、correlation/change ID 和证据 digest；
- 超时后如何只读核对，不自动重试未知批准、未知订单或未知回滚动作；
- 审计保留、访问控制、脱敏、导出、事后复盘和合规要求。

当前 PRD 只允许未来页面消费只读投影。任何新增表、migration、ACL、写 API、凭据接入或 Live 控制面都必须先另立 PRD/ADR 并获得明确批准。

### 14.9 本章验收标准

- [ ] 文档和 UI 文案明确 Demo/Live 不共用 target、凭据、数据、writer、批准、幂等或审计域；
- [ ] 页面没有从 Demo 直接切换 Live 的控件或暗示；
- [ ] 前置检查覆盖持续表现、独立验证、运行健康、对账、风险、账户权限、人工审批和回滚演练；
- [ ] 每一阶段批准均是显式、限范围、限时、可撤销且不可继承的一次性证据；
- [ ] 影子/只读早于任何最小暴露，最小暴露和逐步扩大均需未来独立授权；
- [ ] 停止、熔断、回滚和禁止自动恢复的情形有明确只读状态；
- [ ] `Live 已获人工批准` 不被渲染为“已启动/已运行/可下单”；
- [ ] 未知 API/表/字段/ACL/幂等细节保持“待核对”，本任务零 schema 和零订单链路变更。

## 15. Codex 策略生成与定时任务运行设计（建议基线）

### 15.1 本章定位、边界与非永久事实声明

Codex 可以参与策略构思、代码生成、研究执行和证据整理，但“Codex 生成了一个 `.py` 文件”不等于该策略已经成为正式候选。任何 Codex 生成或修改的策略，只有完成正式研究 coordinator 的生成、验证、全量持久化和质量门，形成 `strategy_research_batches` / `strategy_research_candidates` 记录后，才进入正式候选生命周期；只有 `status=QUALIFIED` 才能被既有部署评审读取。Local Strategy Lab、聊天输出、本地文件、单次回测或定时任务成功提示均不能替代该生命周期。

本章定义未来创建、修改和验收 Codex 定时任务时应遵守的产品与数据契约，不授权本次执行任何调度变更。本次工作不得：

- 创建、启用、暂停、重排、修改或删除 Codex 定时任务；
- 触发手动研究、定时研究、部署评审、运行监督或历史整理；
- 修改数据库、schema、API、运行时、writer、订单、ACL、凭据或 OKX_DEMO 风控；
- 把本章建议时刻、当前状态、任务名称或运行节奏当作永久配置；
- 因任务超时、任务列表不可见或状态未知而接管、补跑、重放或创建第二个 writer。

本章出现的“当前”仅指 `2026-08-09` 对本机 automation metadata 和仓库代码的只读核对快照。任何后续实施、页面展示或故障判断都必须重新读取当时的任务定义、状态、可见性和运行证据；无法完整读取时统一为 `UNKNOWN / NO_OP_FAIL_CLOSED`。

### 15.2 Codex 生成策略进入正式生命周期的唯一推荐路径

```text
Codex 研究目标/变更假设
  → 生成或修改候选源码（仍是研究输入，不是正式候选）
  → FormalStrategyResearchCoordinator.start(trigger=manual|automation)
  → 所有权、OKX_DEMO、市场数据、候选集合、锁与槽位 preflight
  → 固定 10 条生成/加载/静态检查/lookahead/独立窗口/费用与滑点验证
  → 全量持久化 batch + candidates + report digest
  → QUALIFIED / REJECTED / VALIDATION_FAILED
  → 仅 QUALIFIED 由既有部署评审读取
  → canonical strategy/version/backtest/score/approval/deployment（跨链 receipt 仍按 8.5 节待核对）
  → OKX_DEMO 信号/意图/风控/订单/成交/对账
```

路径规则：

1. Codex 只负责提出或生成研究输入，不能直接把状态写成 `QUALIFIED`、`APPROVED`、`ACTIVE` 或“已部署”；
2. 研究策略源码、repository commit、数据窗口、质量契约和报告 digest 必须绑定同一批次，后续修改源码会形成新证据，不覆盖旧批次；
3. 候选必须全量入库，包括拒绝和验证流水线失败记录；不得只保存表现最好的一条；
4. 研究任务不得直接调用订单、grant、writer 或 Live 能力；部署评审不得降低研究门槛；
5. 若 formal research candidate 到 canonical strategy/version 的 receipt 仍不可证明，页面和任务报告只能写“待部署评审/衔接待核对”，不能写“已部署”；
6. 任意源、锁、所有权、数据新鲜度、运行状态或写入结果未知时停止在当前阶段，保留证据并 fail closed。

### 15.3 当前自动化只读快照（非永久配置）

以下表只记录本次审阅时读取到的 automation metadata，用于解释错峰建议。它不是运行健康证明，也不保证下一次仍存在、启用或按相同节奏执行。

| 任务名称（当前快照） | 当前 metadata 状态 | 当前 metadata 频率 | 当前环境/模型 | 在本章中的归类 | 每次实施前必须核对 |
| --- | --- | --- | --- | --- | --- |
| 每15分钟策略进化研究 | `ACTIVE` | 每小时 `:00/:15/:30/:45`，second 0 | local / `gpt-5.6-sol` / medium | 正式研究生成、验证与候选入库 | 任务 ID、prompt、项目/目录、所有权、coordinator、最近 run/batch、可见性和是否仍 ACTIVE |
| 监督 OKX_DEMO 多策略持续运行 | `ACTIVE` | 每小时 `:05/:20/:35/:50`，second 0 | local / `gpt-5.6-sol` / medium | OKX_DEMO 运行监督 | 是否只读、唯一 runtime/writer 所有权、read-only endpoints、最近 reconciliation、不可见来源 |
| 每小时合格策略自动部署 | `ACTIVE` | 每小时 `:10`，second 0 | local / `gpt-5.6-sol` / medium | 合格候选部署评审/自动部署 | 是否只读 `QUALIFIED`、容量/CI/所有权、唯一 deployment writer、最近 no-op/部署 receipt |
| 每两天整理全部 Codex 任务与定时任务 | `ACTIVE` | 每两天 10:00，second 0 | local / `gpt-5.6-terra` / low | 任务历史整理 | 完整任务可见性、活跃/等待用户任务、归档范围、不可恢复影响和通知策略 |
| Freqtrade AI 每三小时自动开发 | `PAUSED` | metadata 中为指定小时的 minute 0 / second 0 | local / `gpt-5.6-sol` / high | 不属于本章核心运行链 | 是否仍暂停；不得与正式研究、runtime 或 writer 混为一条任务 |
| 分钟行情/数据入库与质量检查 | `待核对` | 本次 automation metadata 未发现独立定义 | 表/API/writer 待核对 | 建议新增的独立数据任务职责，不是本次创建项 | 先审计现有数据获取者、文件/DB owner、重复下载、时区、延迟和 runtime 依赖 |

当前快照显示多数任务在 `second=0` 启动，且每两天 10:00 的历史整理与 10:00 的研究槽位存在同秒触发可能；暂停的自动开发若未来恢复，也可能与 minute 0 的任务竞争。该结论只用于提出建议，不能据此自动调整任务。

### 15.4 定时任务清单、输入输出与写入边界

| 任务 ID / 职责 | 推荐触发语义 | 输入 | 输出 | 数据表 / API / Artifact | 唯一写入者与权限 | 可并行 / 必须互斥 | 失败或超时的 fail-closed 行为 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `TASK-DATA-01` 分钟行情入库与质量检查 | 每分钟处理上一根已闭合 candle；入库和质量检查作为一个有 receipt 的数据周期，独立于策略研究 | 精确 execution target、instrument、timeframe、交易所时间、上一根闭合 candle、source response/digest、上次 watermark | 不可变行情记录或受控数据文件、ingest receipt、quality result、freshness/continuity/duplicate/timezone/latency 指标、watermark | 当前正式研究读取 Freqtrade market data catalog/file；runtime 有 trusted market snapshot 模型；专用分钟入库表、写 API、质量 receipt 实体均`待核对`，不得猜测 | 未来专用 market-data ingestor 为唯一数据 writer；Codex 任务只调用受控入口并读取 receipt；不得写 strategy/order 表 | 可与只读监督并行；与同 target/instrument/timeframe 的下一次 ingest 互斥；研究只读取最后一个已提交且质量通过的不可变 snapshot，不能读取写入中的文件 | 源超时、缺 candle、重复冲突、时区不一致、延迟超阈值、部分写入或 receipt 未确认=`DATA_QUALITY_BLOCKED/UNKNOWN`；保留原证据，不推进 watermark，不触发或放行依赖该数据的研究，不自动重复写入未知周期 |
| `TASK-RESEARCH-01` Codex 策略研究生成、验证与候选入库 | 当前建议保留 15 分钟业务节奏，但实际频率/时刻每次核对；手动和定时共用 coordinator | 研究目标、候选源码集合、repository commit、固定质量契约、已通过的数据质量 receipt/market data、当前所有权证据、OKX_DEMO 安全 manifest | formal run state、10 条候选报告、`strategy_research_batches`、`strategy_research_candidates`、六计数、拒绝/失败原因、deployment handoff 状态 | `GET/POST /api/strategy-research/formal-run`；`GET /api/strategy-research-batches`、`/strategy-research-candidates`；正式 worker 和报告 artifact | `FormalStrategyResearchCoordinator` + 持锁 worker 是唯一入口/写入路径；定时任务不得直写候选表或绕过 service | 手动与定时研究全局互斥；同一 15 分钟 slot 互斥；可与监督的只读查询并行；部署评审只能读取已提交终态批次 | preflight 失败记录 `BLOCKED` 且 `generated=0`；已有锁=`ACTIVE_RESEARCH`；同 slot 已持久化=`DUPLICATE_SLOT`；HTTP/任务超时=结果未知，先 GET/DB 核对，禁止自动重跑；已有候选后验证失败则全量保留为 `VALIDATION_FAILED`/FAILED batch，不制造 qualified |
| `TASK-DEPLOY-01` 合格候选部署评审与受控自动部署 | 当前快照为每小时；推荐与研究错峰，只消费已提交 `QUALIFIED` 和完整 canonical promotion evidence | `status=QUALIFIED` 候选、官方质量契约、canonical strategy/version/backtest/score/approval receipt、CI、容量、target、风险策略、唯一所有权和 deployment slots | 明确的 `DEPLOYED / NOT_QUEUED_NO_QUALIFIED / BLOCKED / NO_OP_FAIL_CLOSED` receipt；若部署成功则可追溯 approval/deployment IDs | 读取 research candidates；canonical 链涉及 `full_chain_runs/stages`、`strategy_candidate_approvals`、`strategy_deployments` 等；正式部署读/写 API 路径和 research→canonical receipt 仍按 8.5 节`待核对` | 仅既有 canonical deployment automation/主任务可写 approval/deployment；网页和普通 Codex 研究任务无写权限 | 可并行读取旧的终态批次；同 candidate/version/target/slot 的评审与部署互斥；不得与另一个 deployment writer 并行；监督只读可并行 | 无 qualified 是正常 no-op；receipt/CI/容量/所有权/target 任一未知即不部署；超时不重放，先按 candidate/version/idempotency key 查 receipt；不能因追求活动而降低门槛或制造 ACTIVE |
| `TASK-SUPERVISE-01` OKX_DEMO 运行监督 | 当前快照为每 15 分钟且较研究错后 5 分钟；建议保留错峰思想，实际频率每次核对 | 完整任务可见性、唯一 runtime/writer ownership、`/runtime/read-only`、execution target、observability、exchange state、订单/成交/仓位、latest reconciliation、automation guard | 只读健康结论、状态变化、异常/未知原因、evidence IDs、必要通知；不以任务本身制造订单 | `/runtime/read-only`、`/runtime/execution-target`、`GET /api/okx-demo/observability`、reconciliation/exchange-state 等现有只读接口；guard/reconciliation/order/fill/position 只读实体 | 监督任务只写自己的任务报告/通知；canonical runtime/writer 仍是唯一交易链写入者；不得因监督失败启动第二 runtime/writer | 可与数据 ingest、研究和部署的只读阶段并行；涉及任何恢复/写动作时必须退出一般监督范围并取得单独授权；同一 runtime ownership 检查不可被多个任务解释为接管权 | `unavailableHosts/unavailableSources`、timeout、pending、错误或所有权不完整均为 UNKNOWN；返回 `NO_OP_FAIL_CLOSED`，不启动/停止/重启、不 grant、不下单、不重放未知历史订单；重复无变化只做简短报告 |
| `TASK-HISTORY-01` Codex 任务历史整理 | 低频、低优先级、避开研究/监督/部署窗口；当前快照为每两天 | 完整线程/任务/automation 列表、状态、最后活动、待用户输入、分支/PR/CI 摘要、不可见来源 | 归档建议或经权限允许的可恢复整理结果、保留/跳过原因、数量与审计摘要 | Codex task/automation metadata；不读取或写入交易数据库；具体 task API 以运行时工具为准 | 专用任务整理自动化是唯一整理者；不能修改其他任务的 runtime/writer ownership；归档属于外部状态写入，必须按任务契约显式授权 | 不能与被整理任务的 active turn/等待关键 CI/等待用户批准阶段竞争；可在系统低峰读取快照；同一 task archive 操作幂等 | 任务可见性不完整、状态 unknown/active、存在未提交改动、等待用户或证据不足时跳过；不得误归档、删除分支/worktree 或丢弃历史；失败只记录待核对，不循环重试 |

### 15.5 并行与互斥矩阵

| 发起任务 \ 同时存在任务 | 数据入库 | 策略研究 | 部署评审 | Demo 监督 | 历史整理 |
| --- | --- | --- | --- | --- | --- |
| 数据入库 | 同 target/instrument/timeframe 互斥；不同明确分区可并行 | 可并行，但研究只读上一个已提交质量通过版本 | 可并行，部署不得读取写入中的 market artifact | 可并行只读；监督不得改 watermark | 可并行，历史任务不接触数据 store |
| 策略研究 | 只读稳定 snapshot | 手动/定时/重复 slot 全部互斥 | 可并行处理更早的终态候选；同一未提交批次禁止 | 可并行只读；资源不足时研究应让位于 runtime 安全 | 历史整理不得归档 active research task |
| 部署评审 | 无数据写入权限 | 只读终态 qualified | 同 target/candidate/version/slot 唯一 writer 互斥 | 只读监督可并行；任何恢复动作另行授权 | 历史整理不得归档 active deployment task |
| Demo 监督 | 只读 | 只读 | 只读 | 可有多个观察者但只有一个 canonical ownership 结论；不得出现第二 writer | 历史整理不得把 active/unknown 监督任务当完成 |
| 历史整理 | 不接触 | 仅在已终态且可见时整理 | 同左 | 同左 | 单一整理任务；归档/取消动作幂等 |

资源优先级建议：`OKX_DEMO 安全与对账 > 分钟数据完整性 > 部署/研究写入 > 历史整理`。优先级只决定冲突时谁应等待或 no-op，不扩大任何任务权限。

### 15.6 建议错峰调度表（不在本次执行）

以下时间使用项目统一时区（建议显式 `Asia/Shanghai`，同时在数据内部存 UTC）；秒数用于减少 Codex 任务索引和本机资源在同一秒争用。实施前必须结合当时任务耗时分布、scheduler 支持能力和 runtime 负载重新评审。

| 任务 | 当前只读快照节奏 | 建议节奏 | 建议原因与冲突处理 |
| --- | --- | --- | --- |
| 分钟行情/质量 | 未发现独立 automation，待核对 | 每分钟 `second=08`，处理上一根闭合 candle；超过 60 秒时下一 tick 只核对同一 idempotency key 并 fail closed | 先让整点 candle 完成，再入库；为 quarter-hour 研究留出质量检查时间；同一分区单锁防止重叠 |
| 策略研究 | `:00/:15/:30/:45 second=0` | 保留每 15 分钟业务节奏，建议改为 `:00/:15/:30/:45 second=35` | 等待当前分钟数据 receipt；避开所有 second 0 任务；研究未结束时下个 slot 由共享锁拒绝，不排队补跑 |
| Demo 监督 | `:05/:20/:35/:50 second=0` | 保留每 15 分钟且相对研究错后 5 分钟，建议 `:05/:20/:35/:50 second=20` | 每个监督点先等本分钟数据任务完成；与研究、部署错开；监督超时不触发恢复 |
| 部署评审 | 每小时 `:10 second=0` | 每小时 `:12 second=20` | 避开 second 0 和当前整点研究；只读最近已提交 qualified，不等待或接管仍运行的研究 |
| 历史整理 | 每两天 10:00 `second=0` | 每两天 10:42 `second=20`，或另选经负载证据确认的低峰 | 消除与 10:00 研究的同秒冲突；远离部署和监督分钟；若仍有 active task 则跳过而不是强制整理 |
| 暂停的自动开发 | 当前 `PAUSED`，原 metadata 为若干小时 minute 0 | 保持不属于核心调度；若未来另行恢复，先选择独立 minute/second 并重新做所有权审查 | 防止“自动开发”误触正式研究/runtime/writer；恢复需要独立确认，不由本章授权 |

调度不得依赖“任务通常几分钟就结束”的假设。每项写任务必须同时有逻辑 idempotency key、数据库/文件唯一约束和可证明的 lock/lease；错峰只是降低争用，不是并发安全机制。

### 15.7 Codex 新建或修改定时任务的必填字段

任何未来 automation 变更必须先在 PRD/Issue/变更记录中填写下表；缺一项即不得创建或更新任务。任务 prompt 不得包含凭据值、真实账户敏感信息或授权绕过语句。

| 必填字段 | 要求 |
| --- | --- |
| 任务 ID / 名称 / owner | 稳定 ID、面向用户的名称、业务 owner、唯一 writer owner；说明是否替代已有任务，禁止语义重复任务 |
| 目标与非目标 | 一句话可验收目标；明确不能触发的研究、部署、runtime、订单、Live、凭据和外部写入 |
| 频率与时区 | interval、建议 minute/second、时区、首个生效窗口、错峰理由、最长运行时间和 overlap 策略；不得只写“定期” |
| 执行环境 | local/worktree、项目/仓库、canonical root、分支策略、依赖环境；正式 runtime/writer 只能由已确认 canonical 环境拥有 |
| 模型与推理 | model、reasoning effort、为什么匹配任务风险；模型变化视为任务变更并重新验收 |
| 输入与 source of truth | 精确 API/表/artifact、target、时间窗口、freshness、schema/version、不可见源处理；不从 UI 文案反推事实 |
| 输出与写入范围 | 写 API/表/文件/任务状态/通知的 allowlist、字段范围、唯一写入者、审计 ID；只读任务明确写“无业务写入” |
| 权限范围 | 最小权限、禁止项、是否需要 operator approval；不得把任务 prompt 当作 grant、凭据或真实资金授权 |
| 幂等、锁与互斥 | idempotency key 组成、slot/window、lock/lease/fencing、重复触发结果、手动入口关系、与其他任务的并发矩阵 |
| 超时、失败与重试 | timeout 后的 UNKNOWN 核对路径、可重试/不可重试分类、最大次数和退避；订单/部署/研究未知写入默认不自动重放 |
| 归档与保留 | automation run/thread、日志、DB receipt、artifact、截图的保留期和归档条件；active/unknown/待用户任务不得归档 |
| 通知策略 | 哪些状态通知、去重/节流、失败和安全事件优先级、无变化 no-op 摘要；通知不泄露 secret |
| 变更与回滚 | before/after、change ID、批准人、启停窗口、恢复旧任务定义的方法；回滚不删除历史 run/receipt |
| 验收证据 | 至少包含 task definition 快照、一次安全 no-op/受控测试、锁/重复触发测试、失败/timeout、输出 ID、数据/页面对账和 secret scan |

### 15.8 手动运行与定时运行共享 coordinator

当前代码已经提供应保留的核心模式（是否仍然有效须在实施时复核）：

- 页面 `POST /api/strategy-research/formal-run` 调用 `FormalStrategyResearchCoordinator.start(db, trigger="manual")`；
- 定时脚本 `scripts/trigger_formal_strategy_research.py` 调用同一个 `start(..., trigger="automation")`；
- coordinator 先核对 OKX_DEMO 安全 manifest、30 分钟内唯一所有权证据、Freqtrade binary、正式市场数据和固定 10 条候选集合；
- manual 和 automation 共用同一个 file lock、state artifact 和 15 分钟 slot `run_id`；
- `strategy_research_batches.run_id UNIQUE` 与 report/candidate unique constraints 提供持久化防重边界；
- worker 继承同一 lock FD，在后台完成研究并写回同一状态，不由页面另起一条生成路径。

必须持续满足的交互语义：

| 场景 | coordinator / 页面 / automation 行为 |
| --- | --- |
| 手动点击时定时研究已持锁 | 返回 `ACTIVE_RESEARCH`，按钮保持禁用/运行中；不排队第二轮 |
| 定时 tick 时手动研究已持锁 | 定时任务记录 no-op/blocked receipt；不得重试或创建新 worker |
| 同一 15 分钟 slot 已有 batch | 返回 `DUPLICATE_SLOT`；读取已有 batch，不重新生成/入库 |
| state 写 RUNNING 但锁不存在 | `RUN_STATE_INCONSISTENT`；人工核对，不猜测完成、不自动修复 |
| POST 或 automation 超时 | 结果未知；先 GET formal-run，再按 run_id 查 batch；在确认没有写入前仍不得重复提交 |
| preflight 阻断 | `generated/persisted/qualified/rejected=0`，原因属于未生成门禁，不记成候选拒绝 |
| worker 在已有候选后失败 | 保留 failed batch/`VALIDATION_FAILED` 候选和原因；禁止只保留成功子集 |

未来若改变 slot 粒度、锁实现、worker 环境或 candidate count，必须先更新本章、兼容/迁移策略和重复触发测试；不得在 automation prompt 中实现另一套防重逻辑。

### 15.9 分钟级数据入库与质量检查契约

分钟数据任务必须是策略研究的独立上游能力：它有自己的 owner、writer、watermark、幂等键、质量 receipt 和告警；研究任务只能消费已完成的结果，不能在研究脚本里顺带下载、修补或覆盖分钟数据。当前专用表/API 尚未核对，本章不指定 schema 名称，也不授权新增。

每个数据周期至少需要以下只读可展示字段；字段落点全部待数据专项审计：

| 维度 | 最低字段/证据 | 质量判定 |
| --- | --- | --- |
| 身份 | target、exchange、instrument、product type、timeframe、source endpoint/version | 与任务 allowlist 完全一致；不能用 Demo/Live 模糊名称 |
| 时间 | candle open/close UTC、exchange timestamp、received_at、ingested_at、quality_checked_at、display timezone | 全部 timezone-aware；页面可用本地时区展示但保留 UTC；未来时间或顺序逆转即阻断 |
| 新鲜度 | latest closed candle、expected close、age、freshness threshold/version | 超阈值显示“数据过期”，不把旧数据当当前行情 |
| 连续性 | expected/observed candle count、first/last timestamp、missing intervals | 任一必需 interval 缺失时列出范围；不静默 forward-fill 后标通过 |
| 重复 | natural key（建议 target+instrument+timeframe+open time）、source digest、duplicate/conflict count | 完全相同重复可幂等 no-op；同 key 不同内容为冲突并阻断 |
| 延迟 | exchange→received、received→ingested、ingested→quality checked 的 p50/p95/max 或单周期值 | 阈值版本化；延迟异常与 API 失败分开显示 |
| 内容 | OHLCV 合法性、非负 volume、high/low 边界、closed/final 标识、行数、文件/批次 digest | 未闭合 candle 不进入研究；数值/摘要不一致时隔离该周期 |
| 审计 | ingest run ID、idempotency key、writer、source digest、row count、watermark before/after、status/reason | 成功、失败、重复、冲突和超时都有 receipt；不能只留日志文本 |

正式页面建议语义：

- 总览显示“行情新鲜 / 延迟 / 缺口 / 未知”的短结论、最新闭合时间和检查时间；
- 策略工厂显示本轮研究绑定的数据 receipt/digest 和窗口，不提供“忽略数据质量继续”按钮；
- 模拟盘继续显示 runtime/trusted snapshot 与 reconciliation 的新鲜度，不能用研究数据质量替代交易所权威状态；
- API 失败、从未运行、数据过期、存在缺口、重复 no-op 和内容冲突必须是不同状态；
- 研究在数据任务失败后显示 `DATA_QUALITY_BLOCKED` 或等价待核对状态，不能自行下载数据后绕过上游 receipt。

### 15.10 页面与按钮边界

本章不新增页面按钮。现有唯一正式研究写入口仍是 `UI-S-01 / DATA-S-01` 的“手动运行一轮研究（10 条）”，它与定时研究共享 coordinator。总览、策略工厂和模拟盘未来若展示任务计划、下一次运行、数据新鲜度、最近 supervision 或 deployment receipt，必须先在第 7、8 节增加新的 `UI-TASK-*` / `DATA-TASK-*` 条目，写明：

- 数据是否来自 automation metadata、coordinator state、数据库 receipt 或 runtime API；
- 计划时间与最近实际运行时间，不能把“已调度”显示为“已完成”；
- loading、unknown、paused、blocked、no-op、timeout 和 stale 的页面状态；
- 页面只读或会触发哪一个共享 coordinator；
- task ID、run ID、batch ID、change ID 和 freshness 如何审计；
- 是否涉及外部状态写入、需要何种权限和单独确认。

正式页面不得提供“立即部署”“补跑订单”“接管 runtime”“跳过数据质量”“恢复 Live”或任意跨越现有门禁的任务按钮。

### 15.11 验收标准

- [ ] Codex 生成/修改的策略只有经过 formal coordinator、10 条验证与全量持久化后才显示为正式候选；
- [ ] 分钟数据、研究、部署、监督和历史整理五项任务均记录输入、输出、数据/API、唯一 writer、并发和 fail-closed 行为；
- [ ] 手动与定时研究调用同一 coordinator、lock、state、slot 和数据库唯一约束，重复触发测试通过；
- [ ] 分钟行情任务独立于研究，能区分 freshness、continuity、duplicate、timezone、latency 和 unknown；
- [ ] 当前任务名称、状态和频率均标为日期快照，页面或文档不把它们当永久事实；
- [ ] 建议调度避免当前已知同秒冲突，并明确错峰不能替代 lock/idempotency；
- [ ] 新建/修改 automation 前填写目标、频率、环境、模型/推理、权限、幂等/锁、归档、通知、回滚和验收证据；
- [ ] ownership/task/API 可见性不完整时返回 UNKNOWN/NO_OP_FAIL_CLOSED，不创建重复任务、第二 runtime 或第二 writer；
- [ ] 无新增或修改 automation、数据库、schema、runtime、订单、风控、凭据或真实资金能力；
- [ ] 本章任何待核对表/API/状态在实现前完成专项只读审计，不以推测补齐。
