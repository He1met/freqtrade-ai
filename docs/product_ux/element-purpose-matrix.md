# 全站元素目的矩阵与渐进式披露契约

Issue #556 的前端/文档契约。机器可校验的唯一来源是
[`frontend/src/ux/elementPurpose.ts`](../../frontend/src/ux/elementPurpose.ts)；本文件负责说明阅读方式、界面分层和验收边界。

## 目标与边界

普通模式的每个页面首屏都应回答：

1. 现在是什么页面、为什么需要它；
2. 当前状态是否可信、是否需要注意；
3. 用户要做的判断是什么；
4. 如果存在安全动作，唯一推荐的下一步是什么。

本交付只定义前端可消费的目的、动作、数据价值和披露层级，不复制或重新计算 backend 的业务状态。#555 的 `ProductJourney`、`primary_reason` 和 `next_action` 语义仍是依赖；在该依赖未提供稳定字段前，页面只能消费已有的状态/来源/证据模型，并对未知情况保持 `BLOCKED`、`API_GAP` 或 `NEEDS_ATTENTION`。

## 披露层级

| 层级 | 默认内容 | 不应出现的内容 |
| --- | --- | --- |
| `default` 普通模式 | 用户语言状态、当前决策值、更新时间/新鲜度、异常建议、一个页面或阶段的主操作 | database IDs、绝对路径、完整日志、raw status、ENV 值和无决策价值的技术字段 |
| `advanced-diagnostic` 高级诊断 | database IDs、artifact refs、来源、环境范围、原始状态、脱敏错误、日志和审计路径 | 任何 secret、token、密钥值、未脱敏环境变量和把诊断记录提升为完成结论的暗示 |

`details`、`ExpandableText`、`CopyableValue` 和 Local Strategy Lab 的核心/诊断证据分区是现有设计系统的复用点。高级诊断可以完整审计，但不能反向提升普通模式的产品结论。

## 路由矩阵

`default_primary_action_id` 是初始页面状态的唯一主操作；为空表示当前页面是只读核对页或没有安全可执行动作。Local Strategy Lab 使用 `primary_scope=stage`：生成、回测、评分、Dry-run 的主操作不会同时出现在一个阶段视图中。

| 路由 | 页面目的 | 普通模式要做的判断 | 默认主操作 | 高级诊断归属 |
| --- | --- | --- | --- | --- |
| 全局 shell | 导航、当前页面、来源可信度和诊断入口 | 我在哪里、内容能否用于决策 | 无 | raw status、来源细节、复制引用 |
| `/` 总览 | 汇总研究链真实进展 | 是否有可继续核对的真实记录 | 无 | 聚合来源和原始字段 |
| `/strategies` 策略 | 筛选有当前版本的策略 | 哪条策略值得查看详情 | 无（行级详情入口） | ID、路径、来源 |
| `/strategies/:strategyId` 策略详情 | 核对版本、文件、谱系和校验 | 当前版本能否进入回测 | 无 | 来源、谱系、Diff、校验错误 |
| `/generation-runs` 生成批次 | 核对 Provider、数量和产出 | 是否有可追踪的有效产出 | 无 | run ID、错误与关联版本 |
| `/local-strategy-lab` 实验室 | 生成 → 回测 → 评分 → 受控 Dry-run | 当前阶段是否满足前置 | `lab.generation.submit`（按阶段切换） | DeepSeek 单次调用、证据矩阵、完整 IDs/artifacts |
| `/backtest-runs` 回测批次 | 核对 run/task/result 和指标 | 哪条回测可验收 | 无 | profile、manifest、路径、stdout/stderr |
| `/backtest-tasks` 回测任务 | 区分排队、运行、失败和持久结果 | task 是否已有 result | 无 | task/run/result 关系和 artifact |
| `/hyperopt-runs` Hyperopt | 判断是否有可复核最佳结果 | 最佳结果是否可信 | 无 | spaces、完整参数、配置和 artifact |
| `/ranking` 策略排行榜 | 比较有真实结果身份的评分 | 哪个策略值得继续复核 | 无 | score/result IDs、路径和来源 |
| `/operator-dashboard` 运维面板 | 排序运行就绪、业务阻断和安全边界 | 当前首要阻断是什么 | 无 | readiness、ENV presence、artifact、治理事件 |
| `/okx-demo` OKX Demo | 只读核对 Demo readiness、订单和对账 | 生命周期是否有完整可接受证据 | 无 | 订单/成交关联、账户、仓位和对账快照 |
| `/live-governance` 实盘候选治理 | 只读查看候选、审批、部署治理和回滚 | 当前最高风险阻断是什么 | 无 | 风险检查、人工决策、回滚、监控告警 |
| `/freq-ui` | 收敛旧入口到唯一 OKX Demo 页面 | 是否已进入唯一只读页面 | 无（自动重定向） | 受控 FreqUI 链接，仅在高级诊断且边界通过时显示 |
| `*` 页面未找到 | 解释错误路径并提供安全恢复 | 是否返回总览 | `not-found.return-dashboard` | 当前路径值 |

## 统一 `ActionDescriptor`

所有主操作以及可影响用户判断的可操作元素都必须有已注册的 descriptor。字段名称保持稳定，便于矩阵、组件测试、可访问名称和后续 #555 产品状态投影共同消费。

| 字段 | 约束 |
| --- | --- |
| `action_id` | 稳定、可追踪的前端动作 ID；不得使用按钮文案作为唯一标识 |
| `verb` / `object` | 动作的动词和对象；`action_label` 必须同时包含二者 |
| `action_label` | 可直接作为按钮/链接的动作 + 对象名称 |
| `target` | 具体对象或记录范围 |
| `boundary` | 明确只读、本地、Dry-run 或其他安全边界；危险动作必须说明边界 |
| `prerequisite` | 执行前必须已确认的前置 |
| `expected_result` | 成功后用户能观察到的结果；不能用 HTTP 200 代替业务证据 |
| `failure_impact` | 失败时对当前任务和结论的影响 |
| `reversible` | 是否可逆；不可逆动作不得使用 `danger_level=none` |
| `danger_level` | `none`、`low`、`medium`、`high` 或 `critical` |
| `availability` / `disabled_reason` | `conditional` 动作必须定义禁用原因；UI 必须将原因紧邻控件显示 |
| `next_action` | 失败、阻断或完成后的唯一安全下一步 |

当前受控 Dry-run 动作分别注册为 `lab.dry-run.check`、`lab.dry-run.refresh`、`lab.dry-run.start` 和 `lab.dry-run.stop`。`start`/`stop` 始终保留本地受控 Dry-run 边界，不表示 Live 或真实资金可用。

## `DecisionValue`

默认展示的指标、状态和核心数据必须绑定 `DecisionValue`：

| 字段 | 说明 |
| --- | --- |
| `decision_question` | 用户看到这个值后要回答的问题 |
| `value_label` | 用户语言的值或状态摘要，不直接暴露 raw enum |
| `freshness` | 来源、更新时间规则、无法确认新鲜度时的显示方式 |
| `anomaly_advice` | 异常、过期、来源冲突或缺失时的建议 |
| `default_visibility` | 必须与矩阵 `disclosure` 一致 |

没有决策问题、更新时间/新鲜度或异常建议的数据元素，不得进入普通模式；应移入高级诊断或删除。

## 现有组件与稳定 ID 规则

| 组件/元素模式 | 稳定 ID 规则 | 默认归属 |
| --- | --- | --- |
| `AppLayout` 桌面/移动导航 | `global.nav.<route>`、`global.mobile-nav.toggle` | 普通模式 |
| `PageHeader`、`StatusBadge`、`FallbackNotice` | `global.page-header`、`global.status-badge`、`global.source-notice` | 普通模式 |
| 加载/空/失败态 | `global.empty-error-loading-state` | 普通模式 |
| 主操作按钮或主链接 | 与 descriptor 相同的 `data-action-id` | 普通模式，按页面/阶段最多一个 |
| `details` / `ExpandableText` 技术披露 | `<route>.<component>.audit-details` 或 `global.diagnostic-disclosure` | 高级诊断 |
| `CopyableValue` | `global.copyable-value`，只复制已显示且脱敏值 | 高级诊断 |
| Local Strategy Lab 核心证据/诊断证据 | `local-lab.evidence-browser`、`local-lab.audit-evidence` | 核心决策 / 高级诊断分层 |

重复的列表行或表格单元格使用同一个元素模式和稳定前缀，不为每条运行时记录创建不可追踪的文案 ID；运行时记录 ID 只在高级诊断层显示。

## 静态门与验收映射

`frontend/tests/elementPurposeContract.test.mjs` 已加入 `npm test`，执行以下检查：

- 所有产品路由至少有普通模式矩阵条目；默认主操作不超过一个；
- 每个 `data`/`status` 元素都有完整 `DecisionValue`；
- 每个 `action`/`navigation` 元素都有完整 `ActionDescriptor`；动作名称包含动词和对象，条件禁用动作有原因；
- `technical_only` 元素不能进入普通模式；可访问名称非空并包含动作动词；
- `primary-button`/`primary-link` 源码元素必须带已注册的 `data-action-id`；动态 Dry-run action 只允许解析到已注册前缀；
- 矩阵中的 stable ID 不重复，路由和 descriptor 引用都可解析。

当前主操作源码映射：

| 源码 | `data-action-id` | 设计归属 |
| --- | --- | --- |
| `GenerationStage` 提交 | `lab.generation.submit` | generation 阶段主操作 |
| `CandidateWorkbench` 触发回测 | `lab.backtest.trigger` | backtest 阶段主操作 |
| `CandidateWorkbench` 刷新结果 | `lab.backtest.refresh-results` | backtest 阶段次要核对动作 |
| `CandidateWorkbench` 导入评分 | `lab.score.ingest` | score 阶段主操作 |
| `DryRunDecisionPanel` | `lab.dry-run.<check\|refresh\|start\|stop>` | dry-run 阶段唯一动态主操作 |
| `NotFound` 返回 | `not-found.return-dashboard` | 404 页面恢复主操作 |
| `FreqUILink` 打开 | `freq-ui.open` | 高级诊断且受安全边界控制 |

## 已知 BLOCKED 项

- #555 的 ProductJourney 投影尚未提供稳定 API schema；本交付不新增后端投影、不建立第二套状态机。
- “默认页面首屏的唯一 `next_action`”只能由现有前端状态模型和本矩阵定义；接入 #555 后应把产品语义映射到 `ActionDescriptor`，并保持当前 fail-closed 证据边界。
- 本交付不宣称 OKX Demo、Live、writer 或任何真实交易生命周期完成；相关页面仍是只读诊断/对账展示。
