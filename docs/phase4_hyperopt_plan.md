# Phase 4 Hyperopt 参数优化规划

## 状态

Phase 4 处于 planning 阶段。Phase 3 已完成验收，详见
[phase3_acceptance.md](phase3_acceptance.md)。本文档只规划 Hyperopt 参数优化
阶段，不授权实现 dry-run、live trading、交易所连接、队列基础设施、部署或
生产运行。

## 阶段定义

Phase 4 = Hyperopt 参数优化。

Phase 4 的目标是复用 Freqtrade Hyperopt，对已经通过前序阶段校验和回测的候选
策略进行参数优化，并把优化输入、命令、产物、结果、派生策略版本和优化前后
表现对比纳入项目自己的审计边界。

Phase 4 之后的阶段边界保持如下：

- Phase 5 = Dry-run / FreqUI 运行管理。
- Phase 6 = 实盘候选与部署管理。

## 背景

Phase 3 已建立本地数据 catalog、BacktestProfile v2、Freqtrade backtesting
artifact manifest、结果解析、矩阵聚合、baseline 对比和前端展示。Phase 4 应在
这些边界上继续推进 Hyperopt 参数优化，而不是跳到交易运行或生产部署。

`docs/github_project_plan.md` 已定义 Phase 4 为 `Hyperopt 参数优化`。本规划同步
`docs/roadmap.md` 的阶段定义，并把 Phase 4 拆成 Epic 与可执行子 Issue。

## 范围

Phase 4 允许规划并逐步实现：

- Hyperopt 参数优化设计方案。
- `HyperoptProfile` schema 和优化变量锁定。
- Freqtrade CLI Runner 的 `hyperopt` 命令封装。
- Hyperopt artifact manifest 和 fail-closed 结果归档。
- Freqtrade Hyperopt 结果解析。
- 基于 Hyperopt best params 生成优化后 `StrategyVersion`。
- 优化前后策略表现对比。
- 前端展示 Hyperopt runs、best params 和 before/after 对比。
- Phase 4 offline smoke 验收脚本。
- Phase 4 Review 验收文档。

## 不做

Phase 4 明确不做：

- 不做 dry-run。
- 不做 live trading。
- 不连接真实交易所。
- 不下载真实 K 线。
- 不提交真实 API key、secret、passphrase。
- 不修改 Freqtrade 源码。
- 不引入 Redis、Celery、Kafka、RabbitMQ。
- 不进入 Phase 5、Phase 6 或 Phase 7 范围。

## 设计原则

- 复用 Freqtrade Hyperopt，不自研优化引擎。
- 只使用本地已有 market data；缺少数据时必须 `BLOCKED`，不能下载数据或伪造成功。
- 所有命令输入、配置快照、产物路径、stdout、stderr、return code 和错误原因都必须可审计。
- Hyperopt 参数空间必须由 schema 显式描述并锁定，避免隐式改变实验变量。
- 优化后策略版本必须能追溯父版本、Hyperopt run、best params 和对比基线。
- 前端只展示研究结果和审计状态，不提供 dry-run、live 或部署入口。

## Issue 规划

| 顺序 | Issue | 初始状态 | 说明 |
| --- | --- | --- | --- |
| 1 | [#128](https://github.com/He1met/freqtrade-ai/issues/128) `[EPIC][Phase 4] Hyperopt 参数优化` | Backlog | Phase 4 聚合 Epic，不直接开发。 |
| 2 | [#129](https://github.com/He1met/freqtrade-ai/issues/129) `[Design][Phase 4] Hyperopt 参数优化设计方案` | Ready | 第一个可执行任务，先确认 schema、adapter、artifact、结果和 UI 边界。 |
| 3 | [#130](https://github.com/He1met/freqtrade-ai/issues/130) `[Backend][Phase 4] HyperoptProfile schema 与优化变量锁定` | Backlog | 设计通过后实现 schema。 |
| 4 | [#131](https://github.com/He1met/freqtrade-ai/issues/131) `[Adapter][Phase 4] 扩展 Freqtrade CLI Runner 支持 hyperopt` | Backlog | 设计通过后实现 CLI 封装。 |
| 5 | [#132](https://github.com/He1met/freqtrade-ai/issues/132) `[Backend][Phase 4] Hyperopt artifact manifest 与 fail-closed 结果归档` | Backlog | 归档命令和产物。 |
| 6 | [#133](https://github.com/He1met/freqtrade-ai/issues/133) `[Backend][Phase 4] 解析 Freqtrade Hyperopt 结果` | Backlog | 标准化 best params 和指标。 |
| 7 | [#134](https://github.com/He1met/freqtrade-ai/issues/134) `[Backend][Phase 4] 基于 Hyperopt 结果生成优化后 StrategyVersion` | Backlog | 生成可追溯子版本。 |
| 8 | [#135](https://github.com/He1met/freqtrade-ai/issues/135) `[Backend][Phase 4] 优化前后策略表现对比` | Backlog | 复用 Phase 3 回测/指标边界做比较。 |
| 9 | [#136](https://github.com/He1met/freqtrade-ai/issues/136) `[Frontend][Phase 4] 展示 Hyperopt runs、best params 和 before/after 对比` | Backlog | 展示研究结果，不提供运行交易入口。 |
| 10 | [#137](https://github.com/He1met/freqtrade-ai/issues/137) `[Test][Phase 4] 增加 Phase 4 offline smoke 验收脚本` | Backlog | 离线、fixture、本地文件验收。 |
| 11 | [#138](https://github.com/He1met/freqtrade-ai/issues/138) `[Review][Phase 4] Phase 4 Hyperopt 参数优化验收` | Backlog | Phase 4 结束时的验收 Issue。 |

## Project 字段目标

- `Phase`: `Phase 4 - Hyperopt 参数优化`
- `Status`: Design Issue 为 `Ready`，其余 Phase 4 Issue 为 `Backlog`
- `Type`: Epic / Task / Feature / Test / Review 按 Issue 类型设置
- `Area`: docs / backend / freqtrade-adapter / frontend / testing
- `Priority`: Phase 4 必做项为 `P1 - 当前阶段必须完成`
- `Size`: Epic 为 `XL - 需要继续拆分`，设计为 `M - 1天左右`，其他任务按拆分范围设置

如果 GitHub Project 字段写入失败，PR 必须说明哪些字段需要人工设置。

## Phase 4 验收方向

Phase 4 最终验收应至少包含：

- 设计文档明确 HyperoptProfile、CLI、artifact、结果解析、StrategyVersion 派生和 UI 边界。
- Offline smoke 覆盖 Hyperopt fixture / fake runner 成功、失败和 BLOCKED 路径。
- Backend pytest 通过。
- Frontend build 通过。
- `python3 -m compileall backend/app backend/tests scripts` 通过。
- `git diff --check` 通过。

规划 PR 只需要运行 `git diff --check`，并说明未运行 backend pytest / frontend build 的原因。
