# Phase 3 Backtesting Enhancement Plan

## 状态

Phase 3 进入规划完成后的执行准备阶段。Phase 2 已完成策略研发增强验收，Phase 3 的目标是把回测能力从“单次可运行”推进到“可重复、可审计、可解释”的研究闭环。

Phase 3 不进入交易执行阶段。所有任务继续遵守本地研究边界：不做 dry-run、不做 live trading、不做 Hyperopt、不连接真实交易所、不下载真实 K 线、不提交真实密钥、不修改 Freqtrade 源码、不引入 Redis / Celery / Kafka / RabbitMQ。

## 科学实验原则

Phase 3 的回测增强以实验可复核为核心，而不是扩大功能面。每次回测实验都必须能回答以下问题：

- 使用了哪份本地行情数据，数据是否完整。
- 使用了哪个策略版本、策略蓝图版本和生成批次。
- 使用了哪个 BacktestProfile，实验变量是否被锁定。
- 使用了哪个 Freqtrade 命令、临时配置和受控输出目录。
- Freqtrade 的 stdout、stderr、return code、result JSON 和 artifact 是否被记录。
- 指标是否能被解析，字段缺失时失败原因是否清楚。
- 同一输入重复执行时，结果是否一致或差异是否可解释。

## 实验变量边界

Phase 3 将回测实验拆成三类变量。

固定变量：

- Freqtrade CLI 版本和调用边界。
- 本地 `user_data/data` 行情数据路径。
- 策略文件路径、策略版本、策略蓝图版本。
- BacktestProfile 中定义的 pair、timeframe、timerange、stake、fee、dry-run 禁用状态。
- 临时配置生成路径和 artifact 输出路径。

可控变量：

- 单策略或批量策略集合。
- 单 pair / 单 timeframe 起步，后续由矩阵任务显式扩展。
- 指标解析字段集合。
- baseline 对比对象。

禁止变量：

- 真实交易所连接。
- K 线下载。
- dry-run / live trading。
- Hyperopt。
- 真实 API key、secret、passphrase。
- Freqtrade 源码修改。
- 队列系统或外部 worker。

## 执行包顺序

Phase 3 按依赖顺序推进，避免先做 UI 或批量执行而没有可靠的数据和 artifact 基础。

| 顺序 | Issue | 目标 | Project 初始状态 |
| --- | --- | --- | --- |
| 1 | #103 | 回测实验设计与执行计划 | In Progress |
| 2 | #104 | 本地行情数据可用性与真实回测基线复核 | Ready |
| 3 | #105 | MarketDataCatalog 与数据质量检查 | Backlog |
| 4 | #106 | BacktestProfile Schema v2 与实验变量锁定 | Backlog |
| 5 | #107 | Freqtrade backtesting artifact manifest | Backlog |
| 6 | #108 | 回测结果指标扩展与版本兼容解析 | Backlog |
| 7 | #109 | 批量回测矩阵执行与 fail-closed 聚合 | Backlog |
| 8 | #110 | 回测基线对比与重复性检查 | Backlog |
| 9 | #111 | BacktestRun 展示 artifact 与增强指标 | Backlog |
| 10 | #112 | Backtest Matrix 展示状态与摘要 | Backlog |
| 11 | #113 | 增加 Phase 3 smoke 验收脚本 | Backlog |
| 12 | #114 | Phase 3 回测体系增强验收 | Backlog |

Epic #102 保持 XL 和 Backlog，只用于聚合 Phase 3 范围，不直接进入开发。

## 依赖关系

- #104 是第一项开发任务，用于确认真实 Freqtrade CLI、本地行情数据、临时配置、策略文件和 result JSON 的基线可用性。
- #105 依赖 #104 的数据前置条件，用结构化 catalog 固化本地数据发现和质量检查。
- #106 依赖 #104/#105，用 BacktestProfile 锁定实验变量。
- #107 依赖 #106，用 manifest 记录 Freqtrade 调用和 artifact。
- #108 依赖 #107，用真实 result JSON 样本增强指标解析。
- #109 依赖 #106/#107/#108，开始批量矩阵执行，但仍限于本地数据和受控 profile。
- #110 依赖 #109，用 baseline 和重复性检查筛出不可复核结果。
- #111/#112 依赖后端 artifact、指标和矩阵状态稳定后再做前端展示。
- #113 在核心后端和前端链路完成后建立 Phase 3 smoke。
- #114 只在 #104 到 #113 完成后执行验收收口。

## 验收命令基线

每个 Phase 3 PR 必须执行对应 Issue 的验收命令，并按影响范围补充基线命令。

后端变更：

```bash
cd backend && . .venv/bin/activate && pytest
python3 -m compileall backend/app backend/tests scripts
git diff --check
```

前端变更：

```bash
cd frontend && npm run build
git diff --check
```

Phase 3 smoke 形成后：

```bash
python3 scripts/smoke_phase3.py --offline --tmp-dir /tmp/freqtrade-ai-phase3-smoke
```

真实 Freqtrade CLI 相关任务如果缺少本地行情数据，必须输出 `BLOCKED` 和缺失路径，不得下载数据或伪造成功。

## 自动化选择规则

自动化循环进入 Phase 3 后只处理 milestone 为 `Phase 3 - 回测体系增强` 或 label 含 `phase-3` 的 Issue。

选择顺序：

- 优先选择 Status 为 Ready、Size 不是 XL、无 open PR、验收标准明确的 Phase 3 Issue。
- 当前第一项 Ready 任务是 #104。
- 没有 Ready 时，可以从 Backlog 中选择一个范围清楚、Size 不是 XL、无阻塞的 Phase 3 Issue，先标记 Ready，再领取。
- Epic、Review、XL 或范围不清楚的 Issue 不自动开发。
- #114 只能在 Phase 3 子任务完成后进入验收，不应提前自动领取。

每轮自动化最多推进一个 Issue；每个 PR 只能关闭一个 Issue。合并前必须确认本地验证通过、diff 范围符合 Issue、没有 secrets、没有禁止功能。

## 当前限制

- 真实 Freqtrade spike 依赖本地已有 `user_data/data` 行情数据。
- 缺少行情数据时，Phase 3 真实回测相关任务应 fail-closed。
- Phase 3 不负责生产交易、dry-run、live trading、Hyperopt、真实交易所接入或数据下载。
- Phase 3 不引入异步队列或分布式调度。
- Phase 3 不修改 Freqtrade 或 FreqUI 源码。
