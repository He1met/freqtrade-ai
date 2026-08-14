# Freqtrade AI V1.3 canonical-only 设计权威

- 权威状态：`FROZEN_FOR_IMPLEMENTATION`
- 设计版本：`canonical-v13-phase0-20260814`
- 协调入口：GitHub #714；阶段入口：#715 → #724
- 生产起点：空数据库；旧数据库只允许作为外部只读档案
- 历史轨道：v46/v47、#705、#708、#710 只作 design-lab/legacy evidence

本文是 canonical V1.3 production 的唯一设计权威。实现与本文冲突时必须停止并返回
`BLOCKED_DESIGN_DRIFT`；不得从旧 ORM、旧 migration、旧 API、旧 UI、本地常量、历史
receipt 或 v47 design-lab 状态寻找 fallback。

## 1. 不可协商边界

1. canonical production 从空库建立，数据库 purpose 必须是
   `FREQTRADE_AI_V13_CANONICAL`，业务对象只存在于 PostgreSQL schema
   `strategy_platform_v13`。
2. `freqtrade_ai`、`freqtrade_ai_design_lab`、schema marker `20260813_47` 和 v47
   migration registry 都不是 canonical production 的前置条件或数据源。
3. legacy 只通过 Phase 2 的 latest-only 外部读取端口提供受控输入。canonical API、UI、
   runtime、projection、qualifier、scorer 和 writer 均不得查询 legacy schema。
4. 每个旧 source entry 创建独立的新 `strategy` 和 `strategy_version=1`。相同规范化内容
   只共享 immutable artifact，不合并策略身份。
5. 初始 intake 只执行 envelope、大小、编码、digest、secret 与 path traversal 检查。
   `INTAKE_ACCEPTED` 只意味着安全入库，策略仍为 `DRAFT/UNVALIDATED`。
6. 七类 research P0 配置是 target、window、generation、diversity、
   quality/qualification、scoring、research aggregate。market-data profile 是与七类配置
   独立的控制面；不得被 window 或 research aggregate 冒充。
7. target set、逐 target allocation 和 cap 初始均为 `UNSET/BLOCKED`。总数只能由已冻结
   target members 与逐 target allocation 派生；不存在 60/6、7、10 或其他默认值。
8. activation 只冻结 immutable snapshots/bundle 并切换控制面指针。它不得创建 job、
   worker、backtest、score、qualification、deployment、signal、intent 或 order。
9. scorer 只产生 target-level `overall_score`；qualifier 独占最终
   `QUALIFIED/REJECTED/BLOCKED` 决定。高分不能覆盖任一 required-window 硬门失败。
10. ephemeral research executor 与 long-lived trading runtime 使用不同身份、镜像合同、
    网络、写入权限和生命周期。二者都不得持有 central order-writer 身份。
11. Demo-only、`allow_real_funds=false` 与唯一 central order writer 是代码和数据库权限
    不变量，不是可关闭的业务配置。
12. 缺失、歧义、冲突、过期或不可证明的证据统一返回 `UNKNOWN/BLOCKED/NO_OP`。

## 2. canonical 数据库身份与 genesis

### 2.1 固定身份

| 项目 | 唯一值 |
| --- | --- |
| database purpose | `FREQTRADE_AI_V13_CANONICAL` |
| business schema | `strategy_platform_v13` |
| genesis version | `20260814_01` |
| manifest key | `canonical-v13-table-manifest-v1` |
| legacy import mode | `EXTERNAL_LATEST_ONLY` |
| production default target/count/cap | `UNSET` |
| trading capability at genesis | `TRADING_DISABLED` |

`schema_metadata` 保存上述身份、manifest digest、安装时间和 installer identity。任何
database/schema/version/manifest 不匹配均为 `BLOCKED_WRONG_CANONICAL_DATABASE`。genesis
installer 只引用 canonical metadata；不得 import legacy `Base.metadata`，不得要求旧表、旧
FK、旧 ID、旧状态或默认 profile。

### 2.2 canonical table manifest

以下是 production 允许的业务表全集。后续新增表必须先修改本设计、manifest key/digest
和独立 genesis 验收，不得由 ORM import 顺带创建。

| domain | tables | authority |
| --- | --- | --- |
| schema/audit | `schema_metadata`, `audit_events`, `idempotency_receipts` | schema identity 与追加式审计 |
| intake/catalog | `strategy_artifacts`, `strategy_submissions`, `strategy_intake_receipts`, `strategies`, `strategy_versions` | controlled submission 是唯一入口 |
| control plane | `configuration_profiles`, `configuration_versions`, `configuration_dependencies`, `configuration_snapshots`, `configuration_snapshot_members`, `configuration_bundles`, `configuration_bundle_members`, `configuration_activations`, `research_targets`, `research_target_allocations` | validated snapshot/bundle 与 active pointer |
| market | `market_profiles`, `market_profile_versions`, `market_artifacts`, `market_inspections`, `market_receipts`, `market_snapshots`, `market_snapshot_members` | accepted receipt + frozen snapshot |
| validation | `validation_plans`, `validation_plan_windows`, `validation_attempts`, `validation_window_results` | exact plan/window/attempt evidence |
| scoring/qualification | `target_scores`, `qualification_decisions`, `qualification_window_evidence` | scorer 与 qualifier 分权 |
| optimization | `optimization_runs`, `optimization_trials` | baseline 后受控试验；结果不能直接替换版本 |
| approval/deployment | `deployment_approvals`, `deployments`, `runtime_instances`, `runtime_receipts` | qualification 后独立人工与 runtime 门 |
| execution | `signals`, `trade_intents`, `risk_decisions`, `orders`, `fills`, `ledger_entries`, `reconciliation_runs`, `reconciliation_items` | signal → risk/writer → exchange evidence → ledger |

可重建 read projection 不是业务事实表。projection 可以是 view、materialized view 或外部
cache，但必须可从上述表完整重建；projection writer 不能写回任何业务状态。

### 2.3 明确排除

canonical manifest 不包含以下任一类对象：

- legacy `strategies/strategy_versions/research_jobs/backtest_*` 或它们的 ID/FK；
- v47 `strategy_platform_migration_*`、旧 schema marker、旧 OKX capability/attestation 表；
- legacy `strategy_scores`、`strategy_validation_plans/windows`、
  `market_data_quality_receipts`；
- design-lab owner activation receipt、历史 60/6/7×4 业务值；
- API/UI cache 产生的伪业务表；
- secret、credential value、任意可执行 payload 或数据库内 Python 代码。

## 3. 领域身份、约束与 snapshot binding

### 3.1 intake 与策略身份

- `strategy_artifacts`：以规范化 UTF-8 内容的 SHA-256 为 immutable identity；可以被多个
  strategy version 引用。
- `strategy_submissions`：以调用方 idempotency key + canonical request digest 唯一；
  `source_archive_digest + source_entry_key` 唯一标识一次外部 source entry。
- `strategy_intake_receipts`：只记录 archive snapshot、source entry、artifact 与 submission
  digest；不复制 legacy ID、status、score、job、approval 或 deployment。
- `strategies`：每个 source entry 一个全新 UUID；相同 artifact 不合并该 UUID。
- `strategy_versions`：intake 只创建 `version_number=1`、`catalog_status=DRAFT`、
  `validation_status=UNVALIDATED`、`execution_authorized=false`。

latest-only selector 必须同时证明：`current_version` 属于该 source strategy、它等于可见
版本最高值、source entry 与 artifact 唯一、内容可稳定读取。任一不满足即
`BLOCKED_AMBIGUOUS_LATEST_SOURCE/NO_OP`。

### 3.2 七类 P0 与 market profile

| kind | 固定职责 | 初始状态 |
| --- | --- | --- |
| `TARGET` | target set、instrument/pair/timeframe/data kind | `UNSET/BLOCKED` |
| `WINDOW` | 任意数量 required/optional windows 与 coverage 要求 | `UNSET/BLOCKED` |
| `GENERATION` | 每个 target 的 allocation 与 family/provider 选择 | `UNSET/BLOCKED` |
| `DIVERSITY` | 相似度/相关性算法与阈值 | `DRAFT`，无默认阈值 |
| `QUALITY_QUALIFICATION` | required-window 硬门、target-level minimum score | `DRAFT`，生产值未确认 |
| `SCORING` | target-level score components/normalization/weights | `DRAFT`，无默认权重 |
| `RESEARCH_AGGREGATE` | 精确绑定以上六类 snapshot | `BLOCKED` |
| independent `MARKET_DATA` | downloader capability、artifact inspection、freshness/coverage | `UNSET/BLOCKED` |

七类 payload 采用下列 canonical 形状；字段缺失、空集合、重复 identity、额外隐式 total，或
不满足约束时只允许停在 `DRAFT/BLOCKED`，不得生成 snapshot：

| kind | canonical payload facts |
| --- | --- |
| payload `TARGET` | 非空 `targets[]`；每项精确包含 `target_key/instrument/pair/timeframe/data_kind` |
| payload `WINDOW` | 非空 `windows[]`；每项精确包含唯一 `window_key`、显式 `required`、带时区且有序的 `start_at/end_at`、正整数 `coverage.minimum_closed_candles` |
| payload `GENERATION` | 非空 `allocations[]`；与 frozen target keys 精确等集，每 target 显式正 `allocation_count` 与 `candidate_cap >= allocation_count` |
| payload `DIVERSITY` | 非空 `rules[]`；唯一 `rule_key`、显式 algorithm/metric/operator/threshold |
| payload `QUALITY_QUALIFICATION` | 显式持久化 target-level `minimum_score=50`；非空 `required_window_gates[]`，每项含唯一 key、metric、operator、threshold |
| payload `SCORING` | 显式 `window_aggregation=MEAN/MINIMUM/MAXIMUM`；非空 `components[]`，每项含唯一 key、metric、正 weight、maximize/minimize 和有序 normalization bounds；权重精确合计 1.0，不自动归一化 |
| payload `RESEARCH_AGGREGATE` | 显式 `assembly_key`，并精确依赖前六类各一个 immutable snapshot |

这些是结构合同，不是生产业务默认。特别是测试 fixture 中的 target、window、allocation、
`minimum_score=50` 或 weight 都不能进入 production genesis；空库业务行仍为 0。

`research_targets` 保存冻结 target member；`research_target_allocations` 按 target 保存明确的
allocation 与可选 cap。`total_target_count`、`total_candidate_count` 只在 projection/receipt
中派生并验证，不能作为可独立编辑的事实。

### 3.3 不可变 snapshot 与 bundle

1. `configuration_version` 只允许 `DRAFT → VALIDATED → RETIRED`；VALIDATED 后 payload 与
   digest 不可变。
2. 每个 validated version 生成 immutable `configuration_snapshot`，包含 schema、payload、
   dependency、adapter manifest 和 digest。
3. research bundle 精确包含七类 P0 snapshot、独立 market snapshot、target members、逐
   target allocation/cap 和 window members。
4. bundle map key 固定为 `<configuration_kind>:<snapshot_uuid>`，不得按 type 覆盖同类成员。
5. `configuration_activations` 只保存 scope + workflow → bundle pointer；切换 pointer 产生
   append-only audit event，不改变 bundle 或任何执行记录。
6. runtime 只能读取 frozen runtime DTO；不得 import owner resolver、ORM repository 或
   activation mutation service。

所有 validation、backtest、score、qualification、optimization、deployment 与 execution
记录必须保存以下 lineage；不适用项必须显式为 null 并有 reason，不能隐式补齐：

```text
strategy_version_id
research_target_id
configuration_bundle_id + bundle_digest
market_snapshot_id + market_snapshot_digest
validation_plan_id + validation_plan_digest
actor/adapter identity + request/receipt digest
```

## 4. 状态机与唯一最终权威

### 4.1 状态机

| aggregate | legal transitions | final authority |
| --- | --- | --- |
| submission | `RECEIVED → INTAKE_ACCEPTED | REJECTED | BLOCKED` | intake service receipt |
| strategy catalog | `DRAFT → ACTIVE → ARCHIVED` | catalog service |
| strategy validation | `UNVALIDATED → VALIDATING → VALIDATED | REJECTED | BLOCKED` | validation service；intake 不得写 |
| configuration | `DRAFT → VALIDATED → RETIRED` | control-plane owner |
| readiness | `UNSET/BLOCKED → READY`；证据变化可回到 `BLOCKED` | live evidence projection；非持久业务状态 |
| market inspection | `PENDING → ACCEPTED | REJECTED | BLOCKED` | market inspector receipt |
| validation plan | `DECLARED → READY → RUNNING → COMPLETE | FAILED | BLOCKED` | research orchestrator |
| validation attempt | `PENDING → RUNNING → SUCCEEDED | FAILED | BLOCKED` | ephemeral executor receipt |
| qualification | `PENDING → QUALIFIED | REJECTED | BLOCKED | FAILED` | qualifier only |
| optimization | `NOT_STARTED/PENDING_BASELINE → RUNNING → SUCCEEDED | FAILED | BLOCKED` | optimizer receipt |
| approval | `NOT_REQUESTED → PENDING → APPROVED | REJECTED`; `APPROVED → REVOKED` | human approval service |
| deployment | `NOT_DEPLOYED → PENDING → ACTIVE | FAILED`; `ACTIVE → DISABLED` | deployment controller |
| runtime | `UNKNOWN → STARTING → HEALTHY | DEGRADED | FAILED`; terminal `STOPPED` | long-lived runtime receipt |
| order | `INTENT_ACCEPTED → RISK_ACCEPTED → SUBMITTED → ACCEPTED → PARTIAL/FILLED/CANCELLED/REJECTED` | central writer + exchange receipts |

非法跃迁、重复 terminal 写、证据不完整或 source digest 改变必须 fail closed。

### 4.2 required windows、score、qualification

- required windows 的唯一权威：active bundle 中 `WINDOW` snapshot 的 immutable member 集合。
  plan 创建时把每个 `configuration_snapshot_members.id`、key 与 digest 分别写入
  `validation_plan_windows.window_snapshot_member_id/window_key/window_member_digest`；执行代码
  不得使用固定 key 列表，也不得把 UUID 编码进 key。
- window result 的唯一权威：`validation_window_results`；它只保存原始 metrics 和执行 receipt，
  不写最终 qualification。
- score 的唯一权威：scorer 写入 immutable `target_scores.overall_score`。score 必须绑定同一
  plan、target、bundle、market snapshot 和完整 required-window result set。
- qualification 的唯一权威：`qualification_decisions`。qualifier 先检查所有 required window
  的硬门，再读取同 lineage 的 target score 与配置中的 `minimum_score`。任何硬门失败、
  缺窗口、混合 snapshot、混合 target 或 score 缺失均不能 `QUALIFIED`。
- `minimum_score=50` 只有在被明确持久化进 active `QUALITY_QUALIFICATION` snapshot 时才
  生效；代码不提供 50 的 fallback。

### 4.3 首次回测前后无环边界

首次真实回测前必须完成：schema、intake、domain、API、UI、no-trade runtime code、明确
target/allocation/cap、accepted market snapshot、required-window coverage、七类 P0 validated
snapshot、research bundle preview/activation，以及该次回测单独执行授权。

首次真实回测后才允许产生：window results、target score、qualification decision、baseline
acceptance、optimization run。schema/intake/API/UI 的验收不得反向依赖这些后置结果。

每次 research execution 的唯一授权是 append-only `audit_events` 中的
`RESEARCH_EXECUTION_AUTHORIZED` typed receipt：它必须绑定 exact strategy version、target、bundle
及 digest、market snapshot 及 digest、validation plan 及 digest、actor、purpose、environment、
expiry，并由独立 `RESEARCH_EXECUTION_AUTHORIZATION_CONSUMED` receipt 一次性消费。
授权与消费属于 `canonical_control_writer` 的独立事务；ephemeral executor 只把可重算的 immutable
consumption receipt 交给 `canonical_research_writer`，research connection 不读写 `audit_events`，
control connection 也不写 validation 表。授权根行锁与 authorization 专用 partial UNIQUE index
序列化 consume/revoke，禁止 SQLite 单连接假装跨角色原子事务。catalog 的
`strategy_versions.execution_authorized` 仅是历史兼容布尔列，不能授权任何 canonical attempt，
不能替代 per-run receipt。`PENDING` qualification 只存在于 projection；DB 只插入一次 terminal
decision。isolated optimization run/trial 必须保存 baseline lineage、actor、objective/parameters、
metrics、environment、request/result receipt digest；选中 trial 必须通过 controlled submission
建立一个晚于 trial 且不同于 baseline 的新 `UNVALIDATED/execution_authorized=false` version，并
保存 submission link digest，再走完整链，不能原地提升。

## 5. 所有者、单写者与读者矩阵

| identity | 唯一可写范围 | 明确禁止 |
| --- | --- | --- |
| `canonical_schema_owner` | genesis/schema metadata；受控 DDL | 业务执行、runtime、order |
| `canonical_control_writer` | intake/catalog、配置、market receipt/snapshot、activation、audit | score、qualification、deployment、order |
| `canonical_research_writer` | plan/attempt/window result/score/qualification/optimization | config activation、deployment、exchange |
| `canonical_approval_writer` | deployment approvals | qualification、runtime、order |
| `canonical_deployment_writer` | deployments、runtime identity/receipt | qualification、central order |
| `canonical_signal_writer` | signals only | intent/risk/order/fill/ledger |
| `canonical_risk_writer` | trade intents 与 risk decisions | order exchange write、ledger |
| `canonical_order_writer` | orders only；全系统唯一 central writer | qualification、ledger、reconciliation |
| `canonical_fill_writer` | immutable exchange fill receipts | order decision、ledger rewrite |
| `canonical_ledger_writer` | append-only ledger entries | order、fill、历史 update/delete |
| `canonical_reconciliation_writer` | reconciliation run/items | ledger 静默 adjustment、order |
| `canonical_projection_writer` | 可删除重建的 projection/cache | 任意业务表 |
| `canonical_api_reader` | canonical read projection | legacy、owner mutation |
| `canonical_research_reader` | frozen bundle/artifact/market snapshot/plan | legacy、secret、exchange/order |
| `canonical_runtime_reader` | approved deployment frozen DTO、market read | owner resolver、research attempt、legacy |

每张表在 manifest 代码中必须映射到且只映射到一个 writer identity。读权限使用显式
allowlist；writer 另有只包含 `schema_metadata` 与上游 immutable lineage 的精确
read-dependency allowlist，不能借用全表 API reader。不得 `GRANT ... ON ALL TABLES`，不得通过
owner role 或 `SECURITY DEFINER` 绕过边界。所有非唯一 FK 必须有显式索引，digest 列必须有
64 字符数据库约束；并发 create/activation/attempt transition 由 UNIQUE 与行锁共同 fail closed。

## 6. research executor、trading runtime 与 Docker 合同

### 6.1 ephemeral research executor

- 每个 attempt 短生命周期；输入是 immutable artifact、plan、bundle、market snapshot。
- 无 credential mount、无 exchange client、无 order/risk/ledger 权限、无 production writer
  lease；默认 network disabled。
- 输出只是一份 content-addressed attempt receipt/metrics artifact，由
  `canonical_research_writer` 验证后写入。
- container/process 退出即终止，不允许 heartbeat 变成长驻 trading runtime。

### 6.2 long-lived trading runtime

- 仅消费 `QUALIFIED + APPROVED + ACTIVE deployment` 的 frozen DTO。
- 独立 image identity、service account、network policy 与 persistent heartbeat；不得复用
  research executor 的容器、token、volume 或数据库角色。
- 只可写 signal；signal gateway、risk writer 与唯一 central order writer 分阶段处理。
- runtime 不持有 owner/control/research/order/ledger/reconciliation writer 权限。
- approval 与 deployment 行保存 immutable qualification lineage；撤销/停止/回滚另写 audit/runtime
  receipt，不修改历史 qualification。runtime receipt 必须绑定 launch/capability digest、network
  policy、service account、order-writer=false 与 heartbeat 时效；`TEST_SIMULATED` receipt 永不把
  deployment 提升为 production `ACTIVE/READY`。
- signal、intent、risk decision、order、fill、ledger、reconciliation 各由 manifest 中独立 writer
  及独立 service module 写自己的表，部署镜像不得导入整条 capability facade。order idempotency
  key + request digest 防重复；fill、ledger 与 reconciliation chain 也按外部 identity/chain replay
  no-op；uncertain submit 停在 fail-closed，
  未取得 accepted receipt 时禁止 fill。ledger entry 必须绑定 fill；reconciliation item 必须至少
  绑定 order/fill/ledger 之一，并且 reconciliation 只报告差异，不静默修改 ledger。

短期与长期 Docker 只定义可验证的 launch specification，不把容器存在本身当作 READY。
镜像 digest、capability digest、禁用 credential/exchange/order 的证明和退出/rollback receipt
缺一即 `BLOCKED`。

## 7. market artifact、receipt 与 snapshot 权威

1. `market_artifacts` 是 immutable content-addressed object；路径只是 locator，不是 identity。
2. `market_inspections` 保存 format、编码、时间单调性、duplicate/null/gap、闭合 K 线边界、
   target/pair/timeframe/data-kind、source/acquired-at/provenance 与 acquisition receipt digest；
   `TEST_SIMULATED` fixture 永远不能通过 production readiness。
3. 只有 `market_receipts.status=ACCEPTED` 且 digest 精确匹配的 artifact 才可进入 snapshot。
4. `market_snapshots` 与 members 冻结 exact artifacts/receipts/coverage；窗口和 research 只
   绑定 snapshot，不直接扫描文件系统，也不读取 legacy receipt。
5. freshness 是在当前时刻对 snapshot evidence 的只读判断，不改写历史 receipt。
6. 历史 v47 market artifact/receipt 只能在外部审计展示；没有 Phase 7 新 receipt 时生产
   readiness 为 `MARKET_SNAPSHOT_UNSET/BLOCKED`。
7. Issue 中“激活 MARKET_DATA profile”固定解释为：bundle 显式绑定一个 `VALIDATED`
   immutable market profile version 及其 content-addressed market snapshot；V1.3 不新增第二个
   mutable market-profile active pointer。production readiness 必须联结 snapshot coverage、
   inspection row count、每个 required window 的起止与 `minimum_closed_candles`，以及当前时刻
   freshness；任一缺失即 `BLOCKED`。

## 8. canonical API DTO 分层

固定 API prefix：`/api/canonical-v13`。

| layer | 作用 | 禁止 |
| --- | --- | --- |
| command DTO | submission/config/preview/activation 等明确请求 | ORM object、legacy ID/status、默认业务值 |
| domain result | service 事务结果与 reason codes | UI 文案、隐式 readiness |
| receipt DTO | digest、lineage、actor、时间与不可变证据 | secret、absolute local path 作为 identity |
| projection DTO | catalog/config/market/research/runtime 只读视图 | 写回业务状态、从缺失值推导 READY |
| frozen runtime DTO | runtime 所需最小 bundle/deployment/target capability | owner resolver、mutable config、legacy row |
| display metadata DTO | label/help/sort/capability | qualification/score/readiness 计算 |

首批 exact routes：

```text
POST /api/canonical-v13/submissions
GET  /api/canonical-v13/strategies
GET  /api/canonical-v13/strategies/{strategy_id}
GET  /api/canonical-v13/configurations
POST /api/canonical-v13/configurations/{kind}/drafts
POST /api/canonical-v13/configurations/{kind}/{version_id}/validate
POST /api/canonical-v13/research-bundles/preview
POST /api/canonical-v13/research-bundles/{bundle_id}/activate
GET  /api/canonical-v13/market-data
GET  /api/canonical-v13/market-data/snapshots/{snapshot_id}
GET  /api/canonical-v13/readiness/research
GET  /api/canonical-v13/readiness/runtime
GET  /api/canonical-v13/optimizations
```

所有 route 首先验证 canonical database identity。空状态返回真实的
`DRAFT/UNVALIDATED`、`UNSET/BLOCKED`、`RESEARCH_BUNDLE_UNSET`、
`PENDING_FIRST_BACKTEST` 或 `TRADING_DISABLED`；不得输出 fake `READY`、0 分、0 仓位、
`LEGACY_INCOMPLETE` 或 legacy fallback。

API error envelope 固定为
`{"status":"BLOCKED","error":{"code":"...","detail":"..."}}`；DTO validation 为 422，
resource missing 为 404，canonical identity unavailable 为 503，domain conflict 为 409，未分类
exception 只返回去敏的 500。READY bundle preview 的 digest 唯一派生 prospective UUID；activation
path `{bundle_id}`、expected digest 和全部 preview inputs 必须三者一致，handler 不得忽略 path identity。

## 9. UI 与 URL state

canonical 正式页面固定为：

```text
/v13/submission
/v13/strategies
/v13/configuration
/v13/market-data
/v13/research
/v13/optimization
```

`scope/workflow/profile/version/target/strategy` 选择使用 URL query/path 表达并可刷新、深链、分享。
React local state 只保存未提交表单、展开状态和短暂 loading/error；API projection 是事实唯一
权威。UI 不计算 qualification、activation 或 runtime readiness，不把 missing 转为 0/空/正常。
旧页面保留在历史入口，但不得被 canonical navigation 或 API client 当作 fallback。

query allowlist 固定为：submission 无 query；strategies 为 `strategy`；configuration 为
`scope/workflow/profile/version`；market-data 为 `profile/snapshot/target`；research 为
`scope/workflow/target/strategy`；optimization 为 `strategy/target`。缺 key 表示未选择，绝不自动选择第一项。
重复、空、控制字符、非法 UUID 或 unknown key 均为 `INVALID_URL_STATE` 且不发请求；只有用户显式
导航生成的新 URL 才按本页 allowlist 序列化，不静默删除已有未知 state。token、idempotency key、
receipt/error detail 不得进入 URL 或 storage。所有 2xx DTO 在 client boundary 做运行时结构校验；
unknown enum 显示合同漂移并禁用动作，不能被 TypeScript cast 或默认值提升为成功。

## 10. 分阶段边界、独立验收门与入口条件

| phase | 自身交付与独立出门条件 | 下一阶段入口 |
| --- | --- | --- |
| #715 Design | 本文、manifest/status/writer/reader/snapshot/API DTO 冻结；旧轨道重分类；无双重权威 | design key 与 manifest 可由静态测试验证；无 unresolved schema identity |
| #716 Schema | 空库只安装 canonical manifest；重复安装 no-op；约束/owner/ACL evidence；业务行 0 | genesis manifest/digest/ACL gate `ACCEPTED` |
| #717 Submission | latest-only selector、intake checks、artifact dedupe/strategy separation、idempotent receipt | isolated intake tests 通过；无 validator/backtest/runtime side effect |
| #718 Domain | 七类 P0 + independent market service；draft/validate/preview/snapshot/readiness | target/market/window 缺失时可证明 `BLOCKED`；activation side effect 0 |
| #719 API | exact canonical routes/DTO/identity guard/read projections | API contract/empty/error/idempotency 通过；legacy query count 0 |
| #720 UI | 六页、URL state、空/错/blocked/deep-link/refresh | UI contract tests/build 通过；无 local qualification/readiness 推导 |
| #721 No-trade | validator/executor/scorer/qualifier/optimizer/runtime reader 代码与 simulator | production execution tables 0；显示 `PENDING_FIRST_BACKTEST` |
| #722 Activation readiness | 明确业务 target/allocation/cap、新 market receipt/window coverage、bundle activation | 在真实值与新行情缺失时保持精确 `BLOCKED`；代码/fixture gate 可通过 |
| #723 First research | 每次另行授权的真实 static/lookahead/backtest/score/qualification/optimization | 当前授权仅代码/隔离验收；真实执行保持 `BLOCKED_EXPLICIT_AUTHORITY_REQUIRED` |
| #724 Demo runtime | approval→deployment→runtime→signal→risk→order→fill→ledger→reconciliation 分门 | 当前授权仅代码/隔离验收；credential/OKX/service/order 保持 `BLOCKED` |

阶段验收不得引用未来阶段的绿色结果替代自身证据。后置失败不得回写前置成功事实。

## 11. 既有工作重分类

| 工作 | canonical 定位 | 可复用 | 不得继承 |
| --- | --- | --- | --- |
| #694/#696/#698 | 历史 foundation | immutable snapshot、DTO 分层、配置生命周期、fail-closed 错误思想 | legacy Base/FK/读模型与三阶段 migration-first 顺序 |
| #705 / v47 | design-lab/legacy migration evidence | reconciliation、digest、ACL、备份证据方法 | v47 schema/marker、旧历史迁入库、production bootstrap 声明 |
| #706 | frozen-reader/profile pure prototype | pure validation、无 fallback、narrow reader 思想 | `freqtrade_ai_design_lab`、v47 table projection、旧 bundle DTO |
| #708 | non-executing control-plane prototype | deterministic preview、explicit scope、receipt | 测试 7×4、v47 profile graph、production activation 声明 |
| #710 | owner executor prototype | idempotency、rollback-only、owner discipline | design-lab apply、ACL repair、v47 registry、production target/count |
| #699 | superseded legacy migration scope | 历史问题与 migration evidence | canonical production 关键路径、共享/设计库 migration gate |
| #707 | superseded v47 activation scope | exact dependency/adapter validation lessons | canonical production activation 或 Phase 7 readiness 证明 |

#699/#707 保持历史不删除；其未完成 runtime/activation 声明不能阻塞 canonical genesis，
也不能被视为 #716-#724 的验收证据。

## 12. 当前已知 BLOCKED

- production target set、逐 target allocation/cap：`UNSET/BLOCKED`；
- production market profile 与全新 canonical market snapshot：`UNSET/BLOCKED`；
- production window coverage 与七类 P0 active snapshots：`UNSET/BLOCKED`；
- 首次真实回测授权：`BLOCKED_EXPLICIT_AUTHORITY_REQUIRED`；
- credential、OKX、真实 Demo runtime、signal/order/fill：`BLOCKED_OUT_OF_SCOPE`；
- 生产/共享数据库 genesis/migration/ACL 实证：本任务禁止连接，完成代码和隔离证据后仍需
  另行受控执行。

生产 genesis、owner/ACL 与独立 API composition root 的唯一操作顺序见
`docs/runbooks/strategy_platform_v13_canonical_genesis.md`。legacy `app.main` 不挂载
canonical；reverse proxy 仅将 `/api/canonical-v13` 导向独立 canonical process。

这些 BLOCKED 不反向否定 Phase 0-6 的设计、代码与隔离验收，也不得被假 fixture 宣称为
production `READY`。
