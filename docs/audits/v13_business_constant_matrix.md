# V1.3 业务语义硬编码审计矩阵

## 审计边界

- Issue：[#704](https://github.com/He1met/freqtrade-ai/issues/704)
- 基线：`main@6d262ac87035810099aeefc598ce1cd7b9d18b64`
- 配置依赖：Task1 Draft PR #705（`freqtrade_ai_design_lab` marker `v47`）
- 范围：正式 runtime/API/worker/frontend 读取路径；migration、seed、历史报告、测试夹具不作为生产语义命中。
- 禁区：数据库模型/迁移/ACL、owner resolver、旧库或新库写入、凭据、OKX/IP 白名单、订单、部署和 runtime service control。

本文件是人工可读分组矩阵。逐节点的 `path + line + symbol + SHA-256 fingerprint + disposition + dependency` 机器矩阵位于 `config/v13_business_constant_audit.json`，由 `scripts/audit_v13_business_constants.py` fail-closed 校验。它不是文件级 allowlist：旧业务规则必须保留为 `BLOCKED_*` 债务，只有精确的协议/完整性/安全不变量可标记为 `TECHNICAL_INVARIANT`。

## 判定状态

| 状态 | 含义 |
| --- | --- |
| `REMOVED_FAIL_CLOSED` | 不需要猜测新 schema 即可删除的默认值或伪造投影；缺失后显示 `UNKNOWN` 或阻止动作。 |
| `DTO_VALIDATOR_READY` | 已能校验外部传入的 frozen bundle DTO；不含数据库读取能力。 |
| `RUNTIME_READER_READY` | 已实现只使用 Task1 既有精确 SELECT ACL 的窄投影 reader；不调用 owner resolver，不含任务注入或 runtime 启动。 |
| `PROFILE_ADAPTER_READY` | v2 消费契约已实现并由 frozen profile 驱动；仍需 owner control-plane 创建、VALIDATE、激活对应 configuration versions。 |
| `BLOCKED_CONTROL_PLANE` | 配置结构或 reader 已足够，但正式 workflow 尚未传递 bundle/target identity；禁止 fallback。 |
| `BLOCKED_CONFIGURATION_CONTRACT` | 当前 active profile、adapter schema 或 dependency graph 没有完整表达旧语义，禁止臆造字段或 fallback。 |
| `PROTECTED_RUNTIME` | 位于 Demo/OKX/order/deployment/runtime-control 路径，本线只登记，不修改。 |
| `TECHNICAL_INVARIANT` | 通用协议、完整性或安全常量，不属于可配置业务规则。 |

## runtime / API / worker

| 类别 | 正式命中位置（基线 symbol） | 应由何种冻结配置表达 | 判定与原因 |
| --- | --- | --- | --- |
| 研究目标、pair、timeframe | `backend/app/core/strategy_research_matrix.py::RESEARCH_PAIRS/RESEARCH_TIMEFRAMES/RESEARCH_TARGETS`；`strategy_candidate_validation_queue.py` pair/timeframe allowlist；`bihourly_strategy_research.py` 固定六目标；`formal_strategy_research.py` 固定矩阵 | `research-target-config-set:<id>` 的 enabled targets；生产行 `strategy_target_id` | `BLOCKED_CONTROL_PLANE`。DTO 可表达，但任务尚无受控 bundle/target 注入；历史 null 必须为 `UNKNOWN`。 |
| 固定窗口集合与用途 | `strategy_research_contract.py::REQUIRED_VALIDATION_WINDOW_KEYS`；`strategy_candidate_validation_queue.py` 的 `len(...)=4`；`bihourly_strategy_research.py`、`run_strategy_candidate_research.py` 固定 OOS/WF 窗口和 SOL override | `validation-window-config-set:<id>`、动态 `windows[]`、purpose、required、expected regime | `BLOCKED_CONTROL_PLANE`。不得按数组下标或固定 key 代表窗口。 |
| 质量门 | `strategy_research_contract.py` 的 score/trades/drawdown/fee/slippage；`strategy_research.py` exact-policy 比较；`strategy_research_bridge.py` 与 `qualified_demo_deployment_queue.py` 重复合同 | `quality-gate-profile:<id>` + rule evaluations + bundle identity/digest | `BLOCKED_CONTROL_PLANE`。`QUALIFIED-only` 仍是安全治理门，不能删除。 |
| 候选数、target 数、family、多样性 | `strategy_research_diversity.py` 的 10/60/6/0.90/0.85；API/worker/read model 的 `requested_count=60`；Task1 `diversity-threshold-v1` adapter schema 的 `candidate_count=60,target_count=6` | `generation-profile:<id>`、多个 family version、`diversity-threshold-v2` profile | `PROFILE_ADAPTER_READY + BLOCKED_CONTROL_PLANE`。新 evaluator 从同一 frozen generation/diversity profile 读取 count/family/threshold；测试证明 60/6 与 28/4 均无需改代码。v1 继续只读历史用途，v2 尚待 owner 端注册并激活。 |
| 验证 classifier 与窗口规则 | `strategy_validation_matrix.py` 固定 OOS/WF、regime、30 笔、正收益、±5% classifier | window config + `quality-gate-profile:<id>` + classifier adapter parameters | `BLOCKED_CONTROL_PLANE`。独立 lineage、digest、non-overlap 属技术完整性，保留。 |
| 矩阵并发上限 | `backtest_matrix.py::MAX_MATRIX_TASKS=8` | `worker-execution-profile:<id>` 的明确 `max_matrix_tasks` | `BLOCKED_CONFIGURATION_CONTRACT`。现有 `batch_size`/concurrency 字段不等价，不能代用。 |
| Demo 策略选择、pair→instrument、slot | `okx_demo_strategy_selection.py`、`strategy_deployment_continuation.py` 固定 map/名称/最低分/slot | `strategy_target_id`、deployment selector、capacity profile、promotion/quality profile | `PROTECTED_RUNTIME + BLOCKED_CONFIGURATION_CONTRACT`。旧 `configure_okx_demo_continuous.py` 已删除；canonical continuous soak 不再调用 legacy `app.db`/`app.services` writer 路径。其余 OKX/deployment/order 安全边界继续保留。 |
| Demo 风控 | `demo_automation.py` 的 instrument/leverage/notional/position/deviation/frequency/score/capacity/cooldown | risk/capacity/monitoring profile + execution target capability | `PROTECTED_RUNTIME`。Demo-only、`allow_real_funds=false`、唯一 writer 始终由代码上限强制。 |
| scoring normalization、quality 子权重、penalty、淘汰/警告 | `strategy_scoring.py` 及 `run_strategy_candidate_research.py` 的 normalization 系数、0.35/0.20/0.15/0.20/0.10、30/4、40/15、0.35/3、0.20/10/0.35 | 完整 `profile-bound-score-v2` payload + handler | `PROFILE_ADAPTER_READY + BLOCKED_CONTROL_PLANE`。v2 parser 要求 component weights、每项 normalization、quality components、denominator/completeness/penalty、elimination 与 warning rules 全部非空持久化；缺一项立即 BLOCKED。active scoring v1 仍不完整，需 owner 端创建并激活 v2。 |
| 晋级与 live review | `strategy_promotion.py` 的正收益/20%/30/3/4；`live_candidate_preflight.py` 的 30/10/50%/10 | promotion profile、window purpose、明确 risk/review rule keys | promotion 为 `BLOCKED_CONTROL_PLANE`；live review 为 `BLOCKED_CONFIGURATION_CONTRACT`。 |
| freshness、lease、历史边界 | `bihourly_strategy_research.py` 的 20m/2h/history start/future skew；`qualified_demo_deployment_queue.py` 的 10m/2h/30m；`market_data_quality.py` 的 7d | freshness profile、target max age、market-data policy、worker lease | `BLOCKED_CONFIGURATION_CONTRACT`。Task1 active freshness 尚缺 terminal/ownership/reconciliation kinds，且多来源优先级未定义。 |
| 行情更新与 timeframe 派生 | `download_okx_research_market_data.py` 固定 instrument、5m→15m、6h overlap/start；`market_data_quality.py` 以 5m/15m 字段分支；`formal_strategy_research.py` 的 5/15 分钟换算 | targets、market-data policy、多个 `timeframe-definition:<id>`、downloader adapter capability | 部分 `BLOCKED_CONTROL_PLANE`，任意 timeframe 扩展为 `BLOCKED_CONFIGURATION_CONTRACT`；当前 adapter 未声明通用 derivation graph。 |
| provider/model/generation 参数 | `strategy_generation.py` 固定 provider/model/temperature/timeout/token 与 fake/deepseek/mimo dispatch；materializer 固定模板/指标/slot | provider-model config、generation profile、family definitions、installed adapter registry | DeepSeek 参数为 `BLOCKED_CONTROL_PLANE`；通用 dispatch 为 `BLOCKED_CONFIGURATION_CONTRACT`。bundle 不得携带可执行策略源码。 |
| source/trigger/UI 文案 | `bihourly_strategy_research_trigger.py` manual/automation；`strategy_research.py` source type；`candidate_validation_queue_read.py` 固定中文步骤/进度 | source/trigger definitions、UI presentation profile | `BLOCKED_CONFIGURATION_CONTRACT`。当前 research aggregate 未依赖这些版本。 |
| soak/monitoring | `okx_demo_soak.py` 的 7d/300s/900s；`okx_demo_runtime_readiness.py` heartbeat | monitoring/freshness profile + runtime adapter capability | `PROTECTED_RUNTIME`。本线不触碰 runtime service control。 |

## frontend

| 类别 | 正式命中位置（基线 symbol） | 判定与处理 |
| --- | --- | --- |
| dry-run target 默认 | `frontend/src/api/dryRunApi.ts` 静默补 `BTC/USDT:USDT + 15m + okx`，调用方省略 target | `REMOVED_FAIL_CLOSED`：不再构造默认 target；缺少显式输入时不得 POST。最终 `strategy_target_id + bundle_id` 接线仍为 `BLOCKED_CONTROL_PLANE`。 |
| score breakdown fallback | `frontend/src/api/normalizers/index.ts` 合成固定四权重并把缺失 score/totalScore 变成 0 | `REMOVED_FAIL_CLOSED`：仅渲染 API 实际 breakdown；缺失值保持 `UNKNOWN`，不伪造贡献或质量证据。 |
| 固定 60 展示/类型 | candidate research queue API/model/pages 的 literal type、fallback 与“下一批 60”文案 | `REMOVED_FAIL_CLOSED`：使用权威 `requested_count`，缺失显示 `UNKNOWN`；动态 generation profile 仍等待 control-plane。 |
| 缺失回测指标 | Candidate Workbench 把缺失 OOS `total_trades` 变成 0 | `REMOVED_FAIL_CLOSED`：显示 `UNKNOWN`，不把未知证据伪造成零交易。 |
| 旧状态投影 | queue model 将所有非 `QUALIFIED/REJECTED` 状态归为 `FAILED` | `REMOVED_FAIL_CLOSED`：保留权威状态或 `UNKNOWN`，不制造终态。 |
| 固定 quality contract 作为 POST 门 | `strategyFactoryModel.ts::hasOfficialAggressiveContract` | `BLOCKED_CONTROL_PLANE`。不能直接删除门禁；需 server `start_available/reasons` 或 frozen bundle identity/capability 后替换。 |
| 本地 BacktestProfile/单候选生成 | `candidateWorkbenchModel.ts` 固定 target/profile；Local Strategy Lab `requested_count=1` | pair/timeframe 默认已 `REMOVED_FAIL_CLOSED`，表单必须显式填写；完整 target/profile/bundle ID 与单候选生成仍为 `BLOCKED_CONTROL_PLANE`，不猜新请求 schema。 |
| OKX target/readiness gates | `okxDemoApi.ts`、`okxDemoDisplay.ts` 固定 product/margin/pair/timeframe 与六 gate | `PROTECTED_RUNTIME + BLOCKED_CONTROL_PLANE`。不能用“所有返回 READY”替换 required gates；writer/Demo/funds 门禁必须保留。 |
| 固定四段证据/四 phase/四步 lifecycle | `sourceState.ts`、`workflowState.ts`、Strategies/Factory 本地流程 | `BLOCKED_CONFIGURATION_CONTRACT`。动态 `windows[]` 不是旧 Lab 全流程的等价物，需要 canonical workflow/stage projection。 |
| status denylist/本地 QUALIFIED 推断 | `strategyDisplay.ts` 与 Strategies 本地 eligibility 条件 | `BLOCKED_CONFIGURATION_CONTRACT`。等待权威 action capability；不得把未知新状态默认判为可用。 |

## 已实现的消费边界

`backend/app/services/frozen_configuration_bundle.py` 是纯 DTO 校验器。它验证：

1. 必须是持久化 snapshot，显式 workflow/scope/aggregate identity；
2. 正式 map key 必须是 `<type_key>:<configuration_version_id>`，允许同 type 多版本；bare key 只留在 owner 端历史兼容读取；
3. 每个 version 的 canonical digest、map/digest 对齐、aggregate membership、dependency type/闭包/无环；
4. bundle digest 使用 `configuration-bundle-digest-v1` canonical JSON；
5. capability 必须保持 `demo_only=true`、`allow_real_funds=false`、`single_writer_required=true`、`resolution_contract=strategy-platform-owner-resolver-v1`，并匹配调用方信任的 adapter registry digest；
6. payload 中出现 secret/executable 字段或尝试弱化安全能力时一律 `BLOCKED`。

`backend/app/services/runtime_configuration_bundle_reader.py::RuntimeConfigurationBundleReader.read_validated(bundle_id)` 是独立最小 reader：先验证 `current_database()=freqtrade_ai_design_lab`，再从 `strategy_platform_migration_runs` 的终态只读 header 验证 marker `20260813_47`，随后只从 `configuration_bundle_snapshots`、`configuration_versions`、`configuration_dependencies`、`adapter_definitions` 读取固定字段 allowlist，重算当前 registry digest 与 bundle/version digest，并校验 installed manifest digest。它不 import ORM/repository/owner resolver，不执行 DML/DDL/GRANT/SET ROLE，也不读取旧业务表。

`backend/app/services/profile_bound_adapters.py` 提供 `generation_profile`、`diversity_profile`、`scoring_profile`、`evaluate_profile_bound_diversity` 与 `score_profile_bound_candidate`。60 candidates/6 targets 只作为某个 generation profile 的持久值进入 evaluator；没有 module default。scoring 的 normalization、quality 子权重、trade denominator、metric completeness keys、signal penalties、elimination/warning rules 同样全部来自 resolved `scoring-profile:<id>` payload。

reader 和 adapters 尚未接入 workflow 创建/worker/runtime 启动入口；历史 null snapshot 不会被构造替代值。Task1 现有 SELECT ACL 已覆盖上述四表，因此本 PR 不修改 ACL。

### owner control-plane 待持久化的精确 v2 payload

| type | 必需字段 | 绑定约束 |
| --- | --- | --- |
| `generation-profile` | `candidates_per_target`、`total_target_count`、`total_candidate_count`、`strategy_family_version_ids[]` | 三个 count 均为正数且 `total_candidate_count = candidates_per_target × total_target_count`；family IDs 必须与 frozen graph 中全部 `strategy-family-definition:<id>` 精确一致。当前 60/6 只能作为该 version 的值。 |
| `diversity-profile` | `evaluation_adapter_key=diversity-threshold-v2`、`generation_profile_version_id`、`required_family_version_ids[]`、`thresholds{metric_key: ratio}` | generation ID 必须是同 bundle 中唯一 generation profile；required families 与 generation profile 一致；每个 evidence count/metric 均与 profile 比较。 |
| `scoring-profile` | `scoring_adapter_key=profile-bound-score-v2`、`component_weights{}`、`normalization_rules{}`、`quality_components[]`、`elimination_rules[]`、`warning_rules[]` | component/normalization key 集合一致且 weights 合计 1；quality weights 合计 1；每条 quality component 显式给出 transform 与所需 denominator/keys/penalty；每条 elimination/warning rule 显式给出唯一 `rule_key/metric_key/operator/threshold`。 |

本 PR 不直接 INSERT/VALIDATE/ACTIVATE 这些 version。#705 主线已收到上述 exact module/function/payload 通知；由 owner control-plane 注册新 adapter definition 并激活闭合依赖图后，workflow 才可切换。

## 保留的技术/安全常量

以下不属于业务语义清除范围：

- SHA-256、canonical JSON、digest/schema/manifest/protocol version 与文件/wire shape；
- 状态机合法迁移、事务锁、幂等键、精确 ACL、pagination/read bounds；
- `demo_only=true`、`allow_real_funds=false`、single writer、fail-closed、`QUALIFIED-only`；
- public-data-only、禁止 credential/account/order，以及 validation lineage/digest/non-overlap；
- 测试夹具、migration/seed 与历史报告中的冻结证据（它们不得被 active runtime import）。

## 剩余总门禁

1. Task1 #705 合并并提供 `v47` composite-key + adapter registry capability 契约；
2. owner control-plane 注册并激活 `diversity-threshold-v2` 与绑定 generation version 的 diversity profile，不能复用固定 60/6 的 v1；
3. owner control-plane 创建并激活完整 `profile-bound-score-v2` scoring version；
4. workflow 创建/worker 消费端传入非空 bundle ID 并调用最小 reader；不得回退 owner resolver 或旧常量；
5. 为 matrix tasks、freshness kinds/precedence、downloader timeframe derivation、source/trigger/UI workflow metadata 补明确配置契约；
6. production workflow 创建时强制非空 `configuration_bundle_snapshot_id` 与 `strategy_target_id`；历史 null 继续为 `UNKNOWN`，不得伪回填；
7. 由受保护的 Demo/runtime 线将 deployment/risk/capacity/monitoring 配置接入，保持 QUALIFIED-only 与唯一 writer。
