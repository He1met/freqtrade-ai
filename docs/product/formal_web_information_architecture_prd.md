# Freqtrade AI 正式网页信息架构简化 PRD

- 文档状态：`DRAFT / 待用户确认`
- 版本：`v0.1`
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
4. **统一短状态**：首屏只用“正常 / 运行中 / 需关注 / 已阻断 / 未开始 / 未知”；原始枚举放到展开详情。
5. **先结论后证据**：每个区块顺序固定为“结论 → 原因 → 下一步 → 技术证据”。
6. **空状态不可误导**：每个空状态明确数据源、业务含义和下一步，不用 mock 数据补齐成功。
7. **危险能力不进入本次 UI**：不新增部署、grant、启动/停止运行时、下单、重放订单或真实资金入口。

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
- 修改数据库 schema、migration、ACL、候选生命周期或状态机；
- 修改 OKX_DEMO 风控、订单 writer、grant、执行编排、下单或对账逻辑；
- 读取、显示、记录或提交凭据；
- 切换到真实资金、真实订单或 live trading；
- 启动、停止、重启或接管运行时/自动化；
- 将 Local Strategy Lab 记录并入正式候选，除非未来另有经批准的正式迁移设计；
- 为展示效果制造 mock 成功、伪造候选、订单、成交或部署状态。

## 4. 竞品/同类产品信息架构对比

本对比只使用公开官方资料，不登录账号、不读取用户数据。

| 产品 | 官方信息架构模式 | 借鉴 | 不采用 |
| --- | --- | --- | --- |
| Freqtrade / FreqUI | REST API 将只读状态/交易列表与 `start`、`stop`、删除交易等写操作分开；Webserver 可查看并复用回测结果。 | 正式页面复用 Freqtrade 的“运行结果与回测证据”概念；明确读写动作和风险。 | 不把 `/start`、`/stop`、删除/重载交易等控制放进本次正式页面。 |
| QuantConnect | 以 Project 为容器，Backtests 列表进入单次结果；结果页顶部先显示运行统计，再深入曲线、交易、日志和详细指标；Backtest 与 Live 是不同上下文。 | “列表 → 单次结果”“顶部摘要 → 详情”“运行中持续更新”“错误仍保留记录”。 | 不引入项目/组织/分享/云编译概念，不把回测优秀直接等同可部署。 |
| TradingView Strategy Report | 用 Overview、Performance、Trades analysis、Risk ratios、List of trades 分层；首屏少量核心指标，详情按主题切换。 | 生命周期详情用页签/可展开区块，避免把全部指标平铺；交易明细独立。 | 不采用只看收益的单一排序，也不隐藏成本、回撤、OOS 和质量门。 |
| Hummingbot Dashboard | 明确 Configure → Backtest → Upload config → Deploy → Manage instances；部署后在 Instances 看状态和日志。 | 清晰的阶段路径、阶段前置条件、配置/验证/部署实体分离。 | 不采用“回测满意即可上传部署”的宽松门；本项目必须只读 `QUALIFIED` 并遵守唯一主任务和容量门。 |
| 3Commas | Dashboard 聚合统计；Bots 列表展示名称、PnL、活跃交易和状态；详情页把设置、短统计、图表、活跃交易、最近事件分区；历史列表可展开。 | 总览聚合、列表行简洁、详情展开、最近事件解释“为什么没交易”。 | 不采用启停 bot、市场价卖出、加仓、复制、删除等交易控制；不采用营销型“推荐策略”或收益承诺。 |
| Alpaca Paper Trading | Paper 与 Live 使用不同账号/凭据/endpoint，并明确 paper 是模拟，无法完全反映滑点、队列等现实因素。 | 全局固定展示 `OKX_DEMO / 模拟盘`，不让模拟结果看起来像真实资金结果。 | 不提供切换到 Live 的入口，也不在页面处理密钥。 |

官方参考：

- [Freqtrade REST API](https://www.freqtrade.io/en/stable/rest-api/)
- [Freqtrade Backtesting](https://www.freqtrade.io/en/latest/backtesting/)
- [QuantConnect Projects](https://www.quantconnect.com/docs/v2/cloud-platform/projects)
- [QuantConnect Backtest Results](https://www.quantconnect.com/docs/v2/local-platform/backtesting/results)
- [TradingView Strategy Report](https://www.tradingview.com/support/solutions/43000764138-tradingview-strategy-report-how-to-start/)
- [Hummingbot Dashboard](https://hummingbot.org/dashboard/)
- [Hummingbot Backtesting Strategies](https://hummingbot.org/dashboard/backtest/)
- [3Commas DCA Bot Management](https://help.3commas.io/en/articles/6722124-dca-bot-manage-your-dca-bots)
- [Alpaca Paper Trading](https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading)

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
| DATA-D-01 / UI-D-01~03 | R | `GET /api/strategies`、`/strategy-versions`、`/strategy-research-batches`、`/strategy-research/formal-run`、`/ranking`、`/okx-demo/observability`；运行中策略计数的精确来源待核对 | 无 | `strategies(id,status,current_version_id)`；`strategy_versions(id,validation_status)`；研究批次/候选计数与状态；score；OKX allowlist | 聚合必须保留 source/freshness；任何一个关键源失败，不用 0 替代 | 区块级“未知/读取失败”；保留最后更新时间，不显示综合正常 |
| DATA-S-01 / UI-S-01 | RW-R | `GET /api/strategy-research/formal-run` | `POST /api/strategy-research/formal-run` | coordinator 状态文件/锁；成功流程写 `strategy_research_batches` 与 `strategy_research_candidates`。具体中间进程字段不由 UI 写 | 前置：coordinator `READY`、无 active run、锁与所有权满足；正式 runner 为唯一写入者；`run_id`、`report_digest`、`batch_id+candidate_name/code_digest` 唯一；POST 的重复/并发语义以 coordinator 现状为准，不在本任务改变 | `BLOCKED` 展示 reason；HTTP 超时=结果未知，禁用自动重试，继续 GET 状态核对 |
| DATA-S-02 / UI-S-02~04 | R | `GET /api/strategy-research-batches?limit=20`、必要时 `GET /api/strategy-research-candidates?status=...` | 无 | batches：`id,run_id,status,requested/generated/persisted/qualified/rejected,failure_reason,selection_policy,report_path,digest,commit,completed_at,created_at`；candidates：`id,batch_id,name,status,loadable,static_check,lookahead_status,score,validation_passed,deployable_candidate,rejection_reasons,evidence_snapshot` | `QUALIFIED` 查询仍受官方质量契约过滤；UI 不重算/改写状态 | API 失败=未知；无 rows=尚无持久化批次；不得显示“全部拒绝” |
| DATA-S-03 / UI-S-05~07 | R | `GET /api/strategies`、`/strategy-versions`、`/ranking` | 无 | `strategies`、`strategy_versions`、`strategy_scores` 的展示字段；研究候选到正式策略的直接 FK/映射当前未在 model 中确认，标注“待核对” | 只读；剪贴板不写数据库 | 单一 API 失败仅影响对应分区 |
| DATA-SD-01 / UI-SD-* | R | 当前共享 loader：`GET /api/strategies`、`/strategy-versions`；部署/运行关联精确 API 待核对 | 无 | strategy/version 全字段的展示子集；部署记录实体来源待核对 | 不加载 generated_code 全文到首屏；路径/ID 收起 | 不存在=404 式空状态；关联未知不得显示未部署 |
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
| DATA-DP-01 / 部署摘要（无按钮） | RW-D 的只读投影 | 候选 `QUALIFIED` 查询和部署状态 API（精确 endpoint 待核对） | UI 无 | 可能涉及 `strategy_deployments` repository/实体；当前正式页面映射待核对 | 仅既有自动化/唯一主任务写；容量、CI、所有权门不变 | 未知≠未部署；无 qualified 显示“不排队” |

### 8.3 数据真实性规则

1. `database` 或具有可追溯 DB IDs 的 `api_aggregate` 才能计入正式指标。
2. `fixture`、`fallback`、`mock`、`unknown` 不得计入正式成功数字。
3. 页面加载时不先渲染 0；使用 skeleton/“读取中”。
4. 跨 API 聚合时，必须保留每个区块的来源和新鲜度，不用一个全局 source 覆盖所有结论。
5. 所有写操作超时后默认结果未知；先 GET/DB 对账，不自动重试。

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

- 只读审查、竞品对比、长期基线、按钮与数据映射；
- 用户确认范围、导航、页面职责和按钮处理；
- 不改页面代码。

### 阶段 1：导航与总览最小改造

- 一级正式导航降为总览/策略工厂/模拟盘；
- 开发实验默认收起；
- 总览接真实现有 API，区块级 fail-closed；
- 不新增后端写 API。

### 阶段 2：策略工厂信息层级

- 正式研究控制简化；
- 最新批次流水线、六计数、候选拒绝原因、策略库、排行榜整合；
- 保留唯一手动研究按钮及现有门禁。

### 阶段 3：模拟盘摘要与最近活动

- 基于现有 observability allowlist 展示运行中策略、信号/风控、订单/成交/对账；
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
- [ ] schema、ACL、策略阈值、风控、writer、下单逻辑无变更；
- [ ] 无 mock/fixture/fallback 伪装真实成功。

### 12.5 可追溯性与测试

- [ ] 每个实现改动引用至少一个 `UI-*` 和 `DATA-*` ID；
- [ ] 前端 unit/build 与相关 Playwright 路由/空状态测试通过；
- [ ] 现有 strategy research API/backend 测试通过；
- [ ] `git diff --check` 与 secret scan 通过；
- [ ] 浏览器核对真实、空、加载、失败四类状态；
- [ ] 不启动/停止运行时，不触发正式研究或订单来“制造”截图数据。

## 13. 待用户确认的产品决策

1. 是否同意一级正式导航精简为“总览 / 策略工厂 / 模拟盘”？
2. 是否同意排行榜合并到策略工厂，旧 `/ranking` 路由继续保留？
3. 是否同意生成批次、回测批次、回测任务、Hyperopt、运维证据、治理证据从一级导航移除但保留路由？
4. 是否同意把“实盘候选治理”改名为“候选治理证据（只读/未来能力）”？
5. 是否同意后续实现按阶段 1→4 分 PR 或至少分可独立审查的 commits？

用户确认前，实施状态保持：`NOT_STARTED`。
