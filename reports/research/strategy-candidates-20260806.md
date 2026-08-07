# Freqtrade Ai 每小时策略进化研究（2026-08-06）

## 结论

本轮在唯一正式研究 worktree 中生成并验证了恰好 10 个新的
`BTC/USDT:USDT` 15m 策略候选。10/10 可加载，10/10 通过 AST 静态检查，
10/10 通过 Freqtrade `lookahead-analysis`（每个候选抽取 20 个信号，entry/exit
bias 均为 0）。所有候选的主窗 `phase2-quality-v1` 分数都达到
`min_strategy_score=50`，但没有候选同时通过独立 OOS、bull、range、bear 窗口的
最低 30 笔、费用和滑点后正收益、最大回撤不超过 10% 的联合门槛。

可部署候选清单：`[]`。本轮不得激活任何候选，也没有改动 active 策略、
runtime/writer、下单限制或 Demo/Live 开关。

机器可读证据：`reports/research/strategy-candidates-20260806.json`。

## 自动进化依据

上一轮 10 个候选虽然全部可加载且无 lookahead，但多数窗口成本后亏损；唯一在
局部 bull/bear 为正的 Keltner 候选又因 range 仅 24 笔且略亏而失败。本轮因此：

1. 淘汰原有 EMA/ADX、标准 Donchian、Bollinger/RSI、MACD-volume、Keltner、
   Stochastic、ROC、Ichimoku、OBV 和 ADX hybrid 信号，不做轻微调参复制。
2. 改用 DMI slope、失败突破、rolling VWAP z-score、ATR squeeze、CCI、
   Williams %R、MFI、Aroon、Chaikin 和 Bollinger-width regime switch 十种假设。
3. 保留每个独立窗口至少 30 笔和成本后正收益门槛，并新增逐窗最大回撤 10% 与
   lookahead 必须显式通过的机器晋级条件。

## 安全与复现边界

- `execution_scope=LOCAL_BACKTEST_ONLY`
- `allow_real_funds=false`、`real_orders=false`、`database_used=false`
- 未启动、停止或修改 canonical runtime/writer，未读取凭据，未调用下单接口
- Freqtrade `2026.5`，Python `3.11.15`，CCXT `4.5.56`
- 本地 OKX futures 数据 SHA-256：
  `14af7d20d71d0eb711e39ecc8b88e6c829e6d706c7e8c2dd8d9e3228b18bbf66`
- 单边费率 `0.0005`，单边滑点压力 `0.0002`；所有收益均为双边成本后结果
- 历史 funding-rate 数据不可用，Freqtrade 将 funding fee 记为 0；这是额外限制

## 验证窗口

| 名称 | 类型 | Timerange | Regime | BTC 窗口收益 |
|---|---|---|---|---:|
| primary_bear | PRIMARY | 20230701-20231001 | bear | -11.46% |
| wf_bull | WALK_FORWARD | 20231001-20240301 | bull | +126.76% |
| wf_range | WALK_FORWARD | 20240301-20240629 | range | -1.36% |
| oos | OOS | 20250101-20251001 | bull | +21.72% |
| wf_bear | WALK_FORWARD | 20251001-20260201 | bear | -31.03% |

## 结构化结果

窗口单元格格式为 `交易数 / 成本后收益 / 最大回撤`。

| 候选 | Load/Lookahead | Score | WF bull | WF range | OOS | WF bear | 结果 |
|---|---|---:|---:|---:|---:|---:|---|
| Candidate01DmiSlopePullback | PASS/PASS | 66.54 | 40 / -0.61% / 0.68% | 34 / -0.93% / 1.03% | 81 / -0.30% / 0.91% | 48 / -0.44% / 1.00% | rejected: 全部独立窗亏损 |
| Candidate02FailedBreakoutReversal | PASS/PASS | 75.07 | 69 / -2.36% / 2.94% | 71 / -0.24% / 0.69% | 160 / -2.96% / 2.96% | 67 / -1.37% / 1.37% | rejected: 全部独立窗亏损 |
| Candidate03RollingVwapZscore | PASS/PASS | 69.32 | 195 / -2.77% / 3.23% | 174 / -3.69% / 3.83% | 366 / -7.13% / 7.13% | 148 / -1.16% / 1.68% | rejected: 全部独立窗亏损 |
| Candidate04AtrSqueezeExpansion | PASS/PASS | 69.10 | 101 / -1.65% / 1.97% | 84 / +1.21% / 0.59% | 191 / -2.51% / 3.64% | 88 / +0.82% / 0.90% | rejected: bull 与 OOS 亏损 |
| Candidate05CciTrendPullback | PASS/PASS | 62.34 | 304 / -3.22% / 3.42% | 258 / -2.09% / 2.91% | 558 / -7.73% / 8.30% | 260 / -2.97% / 3.58% | rejected: 全部独立窗亏损 |
| Candidate06WilliamsRangeReversal | PASS/PASS | 62.69 | 340 / -6.55% / 6.60% | 267 / -5.36% / 5.36% | 590 / -8.01% / 8.05% | 251 / -5.04% / 5.05% | rejected: 全部独立窗亏损 |
| Candidate07MfiImpulse | PASS/PASS | 65.69 | 345 / -5.88% / 6.37% | 262 / -3.91% / 4.30% | 553 / -6.90% / 6.90% | 247 / -1.12% / 2.13% | rejected: 全部独立窗亏损 |
| Candidate08AroonPersistence | PASS/PASS | 62.91 | 235 / -2.92% / 3.87% | 196 / -3.67% / 4.46% | 379 / -4.71% / 5.28% | 177 / -2.19% / 3.27% | rejected: 全部独立窗亏损 |
| Candidate09ChaikinBreakout | PASS/PASS | 62.93 | 409 / -4.79% / 6.16% | 344 / -5.74% / 6.06% | 843 / -13.62% / 14.23% | 362 / -3.95% / 4.97% | rejected: 亏损且 OOS 回撤超限 |
| Candidate10BandwidthRegimeSwitch | PASS/PASS | 58.37 | 364 / -2.68% / 3.23% | 297 / -1.85% / 2.82% | 627 / -10.11% / 10.46% | 289 / -2.08% / 3.20% | rejected: 亏损且 OOS 回撤超限 |

## 经验总结与下一轮方向

- 高交易数没有抵消 14 bps 双边费率加滑点压力；过度交易仍是最明显失败模式。
- Candidate04 的 volatility expansion 假设在 range 与 bear 为正，但 bull 与独立
  OOS 为负；可保留“扩张过滤”研究方向，但下一轮应改变退出/趋势确认结构，
  不能围绕现参数做窄幅搜索。
- DMI slope 候选回撤最低、交易数达标，但所有 regime 都小幅负收益，显示优势
  不足而非样本稀疏。
- 反转族（failed breakout、VWAP z-score、Williams %R）在 bull/OOS 中继续亏损，
  下一轮应降低其重复权重，并研究持仓时段、波动聚类或 asymmetry 假设。
- 本轮无部署交接；结构化 `qualified_candidates=[]` 交给自动部署评审即可 no-op。

## 测试证据

- `python -m pytest backend/tests/test_strategy_candidate_research.py -q`：3 passed
- `python -m compileall -q research/strategy_candidates scripts/run_strategy_candidate_research.py`：passed
- Freqtrade lookahead-analysis：10/10 `has_bias=False`
- 五个窗口 Freqtrade backtesting：10/10 均产生完整指标
