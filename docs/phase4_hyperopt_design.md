# Phase 4 Hyperopt 参数优化设计方案

## 状态

本设计对应 Issue
[#129](https://github.com/He1met/freqtrade-ai/issues/129)。本文档只定义
Phase 4 Hyperopt 参数优化的项目边界、数据契约和后续实现顺序，不写业务代码、
不新增数据库迁移、不执行真实 Hyperopt。

Phase 4 只做本地研究型参数优化。Phase 4 不做 dry-run、live trading、真实交易、
真实交易所连接，不下载真实 K 线，不做部署、生产运行、队列基础设施或 Freqtrade 源码修改。

## 目标

Phase 4 必须复用 Freqtrade Hyperopt，不自研优化引擎。它对已生成并通过前序校验/回测
的候选策略做本地参数优化，并把优化输入、命令、产物、结果、优化后策略版本和优化前后
表现对比纳入项目自己的审计边界。

设计目标：

- 明确 Hyperopt 输入和实验变量锁定规则。
- 明确只允许使用本地已有 `user_data/data` 的执行边界。
- 明确 `HyperoptProfile` schema 草案。
- 明确 Freqtrade `hyperopt` CLI 参数白名单草案。
- 明确 Hyperopt artifact manifest 草案。
- 明确 Hyperopt result parser 草案。
- 明确优化后 `StrategyVersion` 生成规则。
- 明确优化前后对比和是否保留候选的判断规则。
- 明确失败和 `BLOCKED` 分类。
- 明确 Phase 4 smoke 覆盖范围和后续 Issue 推荐顺序。

## 输入

每个 Hyperopt run 必须有完整、可审计、可复现的输入快照。允许输入如下：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `strategy_version_id` | 项目数据库 / API | 待优化的策略版本 ID。 |
| `strategy_file_path` | `StrategyVersion.file_path` | 传给 Freqtrade 的策略文件路径，必须是项目生成或导入的受控策略文件。 |
| `backtest_profile_id` | 现有 BacktestProfile | 用于继承 pair、timeframe、timerange、本地数据目录等回测边界。 |
| `hyperopt_profile` | 新增 HyperoptProfile | Hyperopt 专用配置和优化变量锁定。 |
| `pair` | BacktestProfile / HyperoptProfile | 单个交易对，必须能在本地数据 catalog 中找到。 |
| `timeframe` | BacktestProfile / HyperoptProfile | 例如 `5m`、`15m`、`1h`。必须与本地数据匹配。 |
| `timerange` | BacktestProfile / HyperoptProfile | 可选，但一旦设置必须写入输入快照。 |
| `local_data_source` | MarketDataCatalog | 只能引用本地已有 `user_data/data`，不得下载。 |
| `spaces` | HyperoptProfile | 允许的优化空间，例如 `buy`、`sell`、`roi`、`stoploss`、`trailing`。 |
| `epochs` | HyperoptProfile | Hyperopt 迭代次数，必须有上限。 |
| `hyperopt_loss` | HyperoptProfile | Freqtrade hyperopt loss 类名。 |
| `random_state` | HyperoptProfile | 可选；用于提高可复现性。 |
| `config_snapshot` | ConfigBuilder | 临时 Freqtrade config 的安全快照，不包含密钥。 |

输入规则：

- `strategy_version_id`、`strategy_file_path`、`pair`、`timeframe`、`local_data_source`、
  `spaces`、`epochs` 和 `hyperopt_loss` 必填。
- `BacktestProfile` 可作为基础输入，`HyperoptProfile` 只能在显式字段上覆盖。
- 所有覆盖字段必须进入 `locked_variables`，避免隐式改变实验条件。
- `local_data_source` 必须来自本地 catalog scan，不允许通过 Freqtrade 自动下载补齐。
- 不允许输入 exchange credential、dry-run 配置、live 配置、order 配置或部署配置。

禁止输入：

- 真实 API key、secret、passphrase 或任何交易所 credential。
- dry-run、live trading、真实下单、order execution、FreqUI/webserver 或部署配置。
- 自动下载 K 线、连接真实交易所、修改 Freqtrade 源码或全局配置的开关。
- Redis、Celery、Kafka、RabbitMQ 或其他队列/生产运行基础设施配置。

## 执行边界

Hyperopt 执行只允许在本地研究边界内发生：

- 只允许使用本地已有 `user_data/data`。
- 不下载行情。
- 不连接真实交易所。
- 不做 dry-run。
- 不做 live trading。
- 不真实下单。
- 不读取、写入或提交真实 API key、secret、passphrase。
- 不修改 Freqtrade 源码。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不进入 Phase 5、Phase 6 或 Phase 7。

如果本地数据缺失或 `freqtrade` binary 不可用，正确结果是 `BLOCKED`，不是自动下载数据、
不是连接交易所、不是伪造成功。

## HyperoptProfile Schema 草案

`HyperoptProfile` 是 Phase 4 的核心输入 schema。建议先作为 Pydantic schema 实现，
后续如需持久化再单独设计数据库迁移。

```yaml
schema_version: "1"
name: "phase4-local-hyperopt-15m"
description: "Local-only Hyperopt profile for one candidate strategy."
strategy_version_id: 123
backtest_profile_id: 45
pair: "BTC/USDT:USDT"
timeframe: "15m"
timerange: "20240101-20240201"
local_data_source:
  root: "user_data/data"
  exchange: "okx"
  relative_path: "okx/futures/BTC_USDT_USDT-15m-futures.feather"
spaces:
  - buy
  - sell
  - roi
  - stoploss
epochs: 100
hyperopt_loss: "SharpeHyperOptLoss"
random_state: 42
max_open_trades: 1
stake_currency: "USDT"
dry_run: false
live_trading: false
locked_variables:
  pair: "BTC/USDT:USDT"
  timeframe: "15m"
  timerange: "20240101-20240201"
  local_data_source: "okx/futures/BTC_USDT_USDT-15m-futures.feather"
  strategy_version_id: 123
```

校验规则草案：

- `schema_version` 必须为当前支持版本。
- `strategy_version_id` 必须存在。
- `strategy_file_path` 或关联版本文件必须存在。
- `pair`、`timeframe`、`timerange` 必须与 `local_data_source` 兼容。
- `spaces` 只能来自 allowlist：`buy`、`sell`、`roi`、`stoploss`、`trailing`。
- `epochs` 必须大于 0 且不超过项目设定上限，例如 `500`。
- `hyperopt_loss` 必须来自 allowlist 或项目配置 allowlist。
- `dry_run` 和 `live_trading` 必须为 `false`。
- `locked_variables` 必须包含会影响实验可比性的字段。
- 不允许出现 credential、exchange secret、order execution 或 deployment 字段。

## Freqtrade Hyperopt CLI 参数白名单草案

Phase 4 只允许通过受控 adapter 构造 Freqtrade `hyperopt` 命令。参数白名单草案：

| 参数 | 说明 |
| --- | --- |
| `hyperopt` | 固定命令。 |
| `--config` | 临时、安全、无密钥 config。 |
| `--strategy` | 策略 class name。 |
| `--strategy-path` | 本次运行的策略目录。 |
| `--datadir` | 本地已有数据目录。 |
| `--userdir` | 临时 userdir。 |
| `--spaces` | 来自 HyperoptProfile 的 allowlist spaces。 |
| `--epochs` | 来自 HyperoptProfile 的受控 epochs。 |
| `--hyperopt-loss` | allowlist 中的 loss 类名。 |
| `--timerange` | 可选，来自 profile 并写入快照。 |
| `--timeframe` | 可选，若 Freqtrade 版本需要且与 profile 一致。 |
| `--random-state` | 可选，用于复现。 |
| `--print-json` | 可选，用于稳定结果解析。 |
| `--export` | 可选，仅允许导出本地结果文件。 |
| `--export-filename` 或版本兼容输出目录 | 可选，必须落在临时/报告目录。 |

禁止参数草案：

- 任何 dry-run 或 live 相关参数。
- 任何下单、交易运行、webserver 或 FreqUI 启动参数。
- 任何下载数据参数。
- 任何 credential、API key、secret、passphrase 参数。
- 任何会修改 Freqtrade 源码或全局配置的参数。

## Hyperopt Artifact Manifest 草案

每次 Hyperopt run 必须写 manifest。建议结构：

```json
{
  "manifest_version": 1,
  "status": "SUCCESS",
  "profile_name": "phase4-local-hyperopt-15m",
  "strategy_version_id": 123,
  "strategy_name": "MyStrategy",
  "strategy_file_path": "/tmp/.../strategies/my_strategy.py",
  "config_path": "/tmp/.../config.json",
  "userdir": "/tmp/.../user_data",
  "datadir": "/tmp/.../user_data/data/okx",
  "pair": "BTC/USDT:USDT",
  "timeframe": "15m",
  "timerange": "20240101-20240201",
  "spaces": ["buy", "sell", "roi", "stoploss"],
  "epochs": 100,
  "hyperopt_loss": "SharpeHyperOptLoss",
  "command_args": ["freqtrade", "hyperopt", "..."],
  "return_code": 0,
  "stdout": "tail only",
  "stderr": "tail only",
  "result_path": "/tmp/.../hyperopt-result.json",
  "best_params_path": "/tmp/.../best-params.json",
  "manifest_path": "/tmp/.../hyperopt-artifact.json",
  "blocked_reason": null,
  "failed_reason": null
}
```

状态规则：

- `SUCCESS`: 命令返回 0，结果文件存在且 parser 可读取 best result。
- `FAILED`: 本地前置条件满足，但命令失败、结果缺失或解析失败。
- `BLOCKED`: 缺少本地数据、缺少 `freqtrade` binary、策略文件不存在或环境前置条件不满足。

Manifest 不得包含真实密钥、完整大日志、下载产物或交易运行状态。

## Hyperopt Result Parser 草案

Parser 负责把 Freqtrade Hyperopt 输出转换为项目稳定 DTO。

建议 DTO：

```json
{
  "result_path": "/tmp/.../hyperopt-result.json",
  "strategy_name": "MyStrategy",
  "best_epoch": 87,
  "loss": -1.2345,
  "is_best": true,
  "spaces": ["buy", "sell", "roi", "stoploss"],
  "best_params": {
    "buy": {"rsi_enabled": true, "rsi_value": 32},
    "sell": {"sell_rsi": 70},
    "roi": {"0": 0.05, "60": 0.02},
    "stoploss": -0.12
  },
  "metrics_snapshot": {
    "profit_total": 12.34,
    "profit_pct": 0.043,
    "max_drawdown_pct": 0.025,
    "win_rate": 0.58,
    "total_trades": 42
  },
  "parser_metadata": {
    "source": "freqtrade_hyperopt_result_parser",
    "missing_metrics": []
  }
}
```

Parser 行为：

- 支持 Freqtrade 版本差异下的 JSON/export 形状。
- 优先提取 best result / best epoch。
- 标准化 `best_params`，保留原始 snapshot。
- 缺失 best result、损坏 JSON、缺少必要字段时返回明确失败。
- 不根据解析结果自动写策略文件、启动回测或启动交易。

## 优化后 StrategyVersion 生成规则

Hyperopt best params 不能覆盖父版本。优化结果必须生成子 `StrategyVersion`。

生成规则：

- `parent_version_id`: 原始 `strategy_version_id`。
- `generation_run_id`: 可为空；Hyperopt 不是 LLM generation。
- `hyperopt_run_id`: 指向本次 Hyperopt run。
- `version_number`: 父策略下递增。
- `file_path`: 新生成的优化后策略文件路径。
- `change_summary`: 例如 `Apply Phase 4 Hyperopt best params from run <id>.`
- `diff_snapshot`: 包含参数变化、spaces、loss、best epoch、父版本路径和结果路径。
- `optimized_parameter_metadata`: 保存 best params、HyperoptProfile 摘要、parser metadata 和 manifest path。
- `validation_status`: 初始为 `pending` 或通过静态检查后为 `passed`，不得默认为可交易。

示例 `diff_snapshot`：

```json
{
  "source": "phase4_hyperopt",
  "parent_version_id": 123,
  "hyperopt_run_id": 456,
  "changed_parameters": {
    "buy.rsi_value": {"before": 30, "after": 32},
    "stoploss": {"before": -0.1, "after": -0.12}
  },
  "spaces": ["buy", "sell", "roi", "stoploss"],
  "best_epoch": 87,
  "loss": -1.2345,
  "manifest_path": "/tmp/.../hyperopt-artifact.json"
}
```

优化后版本进入后续回测/审查前，必须仍然通过现有静态审查和本地 backtesting。
不得直接进入 dry-run 或 live。

## 优化前后对比规则

Phase 4 对比不只看 Hyperopt best result，还必须把优化后版本再次回测。

对比输入：

1. 原始回测结果：父 `StrategyVersion` 在同一 BacktestProfile 下的 baseline backtest。
2. Hyperopt best result：Freqtrade Hyperopt 输出的 best epoch / best params / best metrics。
3. 优化后回测结果：使用优化后 `StrategyVersion` 在同一 BacktestProfile 下重新 backtesting。

比较字段：

- `profit_total`
- `profit_pct`
- `max_drawdown_pct`
- `win_rate`
- `total_trades`
- `sharpe`
- `sortino`
- `calmar`
- `timerange`
- `pair`
- `timeframe`

保留候选规则草案：

- 优化后回测必须成功。
- 优化后收益或评分有正向改善。
- 最大回撤不得显著恶化。
- 交易数不得低到失去统计意义。
- 如果 Hyperopt best result 改善但优化后回测未改善，则不自动保留。
- 如果改善不足或风险恶化，记录 `no_useful_improvement` warning / failure reason。

输出建议：

```json
{
  "baseline_result_id": 1,
  "hyperopt_result_id": 2,
  "optimized_backtest_result_id": 3,
  "keep_candidate": true,
  "decision": "keep",
  "delta": {
    "profit_pct": 0.012,
    "max_drawdown_pct": -0.004,
    "win_rate": 0.03
  },
  "warnings": []
}
```

## 失败和 BLOCKED 分类

| 分类 | 状态 | 说明 |
| --- | --- | --- |
| `missing_local_data` | `BLOCKED` | `user_data/data` 下没有 profile 所需 pair/timeframe/timerange。 |
| `missing_freqtrade_binary` | `BLOCKED` | 找不到 `freqtrade` 命令或显式 binary 路径无效。 |
| `invalid_strategy` | `BLOCKED` 或 `FAILED` | 策略文件缺失/语法错误/静态审查失败；命令前发现为 `BLOCKED`，命令后发现为 `FAILED`。 |
| `hyperopt_command_failed` | `FAILED` | 前置条件满足，但 Freqtrade hyperopt 非零退出。 |
| `result_file_missing` | `FAILED` | 命令成功或部分成功，但预期结果文件不存在。 |
| `result_parse_failed` | `FAILED` | 结果 JSON 损坏、无 best result 或缺必需字段。 |
| `no_useful_improvement` | `FAILED` 或 warning | 优化后回测没有有效改善，或风险显著恶化。 |

所有失败都必须写入 artifact manifest、failure reason 或 run summary，供 UI 展示。

## Phase 4 Smoke 覆盖范围

Phase 4 smoke 应保持 offline/local-only，不执行真实 Hyperopt。建议覆盖：

- 创建 fixture `HyperoptProfile`，校验 schema 和 locked variables。
- 使用 fake Hyperopt runner 生成 `SUCCESS` artifact manifest。
- 使用 fake Hyperopt runner 生成 `FAILED` manifest，例如 command failed。
- 使用缺本地数据 fixture 生成 `BLOCKED` manifest。
- 解析 fixture Hyperopt result，提取 best params 和 metrics snapshot。
- 基于 best params 生成优化后 `StrategyVersion` fixture，验证 parent lineage 和 diff snapshot。
- 使用 fixture baseline / optimized backtest result 生成 before/after 对比。
- 验证 `no_useful_improvement` 场景。
- 可选运行前端 build，确保 Hyperopt 展示入口仍可构建。

Smoke 必须输出安全边界：

- 不调用真实 Freqtrade Hyperopt。
- 不连接真实交易所。
- 不下载 K 线。
- 不执行 dry-run/live trading。
- 不读取或写入真实密钥。
- 不修改 Freqtrade 源码。

## 后续 Issue 推荐开发顺序

推荐顺序：

1. #130 `[Backend][Phase 4] HyperoptProfile schema 与优化变量锁定`
2. #131 `[Adapter][Phase 4] 扩展 Freqtrade CLI Runner 支持 hyperopt`
3. #132 `[Backend][Phase 4] Hyperopt artifact manifest 与 fail-closed 结果归档`
4. #133 `[Backend][Phase 4] 解析 Freqtrade Hyperopt 结果`
5. #134 `[Backend][Phase 4] 基于 Hyperopt 结果生成优化后 StrategyVersion`
6. #135 `[Backend][Phase 4] 优化前后策略表现对比`
7. #136 `[Frontend][Phase 4] 展示 Hyperopt runs、best params 和 before/after 对比`
8. #137 `[Test][Phase 4] 增加 Phase 4 offline smoke 验收脚本`
9. #138 `[Review][Phase 4] Phase 4 Hyperopt 参数优化验收`

执行策略：

- 每次只推进一个 Ready Issue。
- Backlog Issue 不自动进入开发；需要人工或规划 PR 明确设为 Ready。
- Epic #128 不直接开发。
- Review #138 只能在 Phase 4 子任务全部完成后领取。

## 验收

本设计 PR 的验收范围：

- 新增 `docs/phase4_hyperopt_design.md`。
- 更新 `docs/testing_plan.md`，预留 Phase 4 smoke 位置。
- 不写业务代码。
- 不新增数据库迁移。
- 不执行真实 Hyperopt。
- `git diff --check` 通过。
