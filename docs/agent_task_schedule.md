# Agent Task Schedule

本计划定义 AI Agent / 团队角色的周期性治理任务和触发任务。所有任务默认只在本地、
开发、测试或 GitHub 治理上下文中运行；不得启动真实 dry-run、live trading、真实下单、
真实交易所连接、真实 K 线下载、生产部署或 Freqtrade 源码修改。

## 任务计划

| 任务 | 负责人 | 频率 / 触发 | 允许范围 | 必需输出 | Fail-closed 条件 |
| --- | --- | --- | --- | --- | --- |
| 每日 PM 需求范围巡检 | Product Manager | 每日 | 检查新 Issue、PR 描述、roadmap 和 Feature Intake；识别是否越过阶段边界。 | 范围巡检记录、需要拆分的 Issue、scope creep 判定。 | 缺少 Feature Intake、验收标准、不做范围或阶段授权时标记 `BLOCKED`。 |
| 每日 QA 本地 DB seed + UI E2E | QA Test Engineer | 每日 | 仅 local/dev/test 数据库；允许 reset、migration、seed、插入脏数据、页面和数据库对账。 | seed 记录、migration 结果、UI E2E 结果、DB/UI 对账报告、缺陷 Issue。 | 发现 production/shared/remote 连接、数据来源不明、页面与数据库不一致或脏数据未 fail-closed 时停止。 |
| 每日策略生成到回测闭环测试 | QA Test Engineer + Quant / Data | 每日 | 使用 fake/mock provider、fixture runner 或本地已有数据；不得连接真实交易所或下载 K 线。 | 生成到回测闭环报告、artifact / manifest 检查、fallback/mock 来源说明。 | 缺少本地数据、fallback 未明示、artifact 不可追溯或结果伪装成真实交易时停止。 |
| 每日 SRE 运行健康检查 | SRE / DevOps | 每日 | 本地服务、CI 状态、只读诊断 API、artifact 目录、日志脱敏和配置安全。 | 健康检查报告、CI 状态、诊断输出、运行 blocker。 | 发现密钥落盘、真实交易控制、远端写入、生产部署入口或诊断数据缺失时停止。 |
| 每日 Security 安全边界扫描 | Security / Risk | 每日 | secret scan、配置扫描、PR diff、交易控制关键字和高风险能力边界。 | 安全扫描结果、风险 Issue、禁止项确认。 | 发现真实密钥、live trading 启动、真实交易所连接、真实下单、K 线下载或未审批高风险能力时停止。 |
| 每周 Architect 架构审查 | Architect Engineer | 每周 | 检查模块边界、schema/API 契约、数据流、依赖、PR 体积和阶段拆分。 | 架构审查记录、拆分建议、技术债 Issue、接口契约更新建议。 | PR 过大、跨阶段耦合、数据库/页面契约不一致或引入未审批基础设施时停止。 |
| PR 触发回归任务 | QA Test Engineer + Release Manager | 每个 PR | 根据 diff 选择 pytest、compileall、frontend build、relevant smoke、API/UI/DB 对账和 secret scan。 | PR 验收清单、命令结果、跳过说明、scope creep 检查。 | 必需验证失败、docs-only 跳过原因缺失、安全边界不清或验收证据不完整时阻止合并。 |
| 手动破坏性本地全链路验收 | QA Test Engineer + SRE / DevOps + Security / Risk | 人工触发 | 仅 local/dev/test；允许清空本地测试数据库、重跑 migration、构造坏数据、模拟失败、执行 UI/API 对账。 | 手动验收报告、破坏性操作范围、恢复步骤、风险结论。 | 目标不是 local/dev/test、可能触达 production/shared/remote、需要真实密钥、真实交易所或真实 dry-run/live 时不得执行。 |

## QA 数据库造数边界

QA 任务明确允许以下操作，但只允许在 local、dev 或 test 数据库中执行：

- 重置 local/dev/test 数据库。
- 执行 migration。
- 创建 seed 数据。
- 插入脏数据、边界数据、缺失字段、非法状态和不一致引用。
- 通过 API、UI 和数据库查询进行对账。
- 验证页面展示、API response、artifact 和数据库记录是否一致。

QA 任务明确禁止：

- 操作 production、shared、remote 或来源不明的数据库。
- 使用真实交易所数据写入数据库。
- 写入真实 API key、secret、passphrase 或 token。
- 把 dirty data 或 fixture 结果标记成 production ready。
- 为了让 E2E 通过而绕过 fail-closed、风险检查或 Feature Intake。

## PR 触发回归任务选择规则

- 文档-only PR：至少运行 `git diff --check`，并检查新增或修改的 Markdown 链接有效。
- Python / backend 变更：运行 backend pytest、compileall、secret scan 和相关 smoke。
- Frontend 变更：运行 frontend build、UI 浏览器检查、fallback/mock 明示检查和相关 API 检查。
- 数据库 / migration / seed 变更：运行 migration、DB/UI 对账、脏数据 fail-closed 和回滚说明检查。
- 安全、运行、部署或交易相关变更：必须经过 Security / Risk 审查；未审批前默认 `BLOCKED`。

## 不授权范围

本任务计划只是治理计划，不实现调度器、队列、worker pool 或生产自动化。它不授权：

- Redis、Celery、Kafka、RabbitMQ 或任何 queue infrastructure。
- production deployment 或 deployment executor。
- real dry-run execution。
- live trading。
- 真实交易所连接。
- 真实 K 线下载。
- live bot start / stop / deploy controls。
