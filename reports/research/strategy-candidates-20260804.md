# Freqtrade Ai 十策略候选研究（2026-08-04）

## 结论

本轮在隔离 worktree 中生成了恰好 10 个候选，并使用真实本地 OKX
`BTC/USDT:USDT` 15m futures 数据完成 1 个主筛选、1 个独立 OOS 和 3 个
互不重叠 walk-forward 窗口的 Freqtrade 回测。10/10 可加载，10/10 通过项目
AST 静态审查，10/10 通过 Freqtrade lookahead-analysis（各抽取 20 个信号，
entry/exit bias 均为 0）。

`phase2-quality-v1` 主窗压力评分均达到 `min_strategy_score=50`，但没有候选
同时满足所有独立验证窗“至少 30 笔且计入费率和滑点后净盈利”的项目晋级
条件。因此本轮可部署候选清单为空；不得激活任何候选。

## 安全与可复现边界

- 范围：`LOCAL_BACKTEST_ONLY`；`allow_real_funds=false`、`real_orders=false`。
- 未连接数据库，未读取凭据，未触碰 canonical runtime/writer、LaunchAgent 或
  canonical 配置，未调用交易接口。
- Freqtrade：`2026.5`，Python `3.11.15`，CCXT `4.5.56`。
- 交易标的：`BTC/USDT:USDT`，15m，futures，isolated，最大同时 1 笔，
  stake 100 USDT，起始余额 1000 USDT。
- 费率：单边 `0.0005`，由 Freqtrade 在开平仓各计一次。
- 滑点压力：单边 `0.0002`，在逐笔结果上开平仓各扣一次。
- 行情文件 SHA-256：
  `14af7d20d71d0eb711e39ecc8b88e6c829e6d706c7e8c2dd8d9e3228b18bbf66`。
- 历史窗口没有匹配的 funding-rate 数据，Freqtrade 将 funding fee 记为 0；
  这是阻止直接晋级的额外保守限制，不应解释为永续资金费已被验证。

复现命令：

```bash
/Users/shenjianpeng/freqtrade_venv/bin/python \
  scripts/run_strategy_candidate_research.py \
  --freqtrade /Users/shenjianpeng/freqtrade_venv/bin/freqtrade \
  --datadir "/Users/shenjianpeng/Developer/Freqtrade Ai/user_data/data/okx"
```

机器可读证据见 `reports/research/strategy-candidates-20260804.json`，其中保存了
完整命令、窗口、逐候选指标、评分拆解、策略 hash 和安全声明。原始 zip、
stdout、stderr 与 lookahead CSV 位于被 Git 忽略的
`reports/backtests/strategy-candidates-20260804/`。

## 历史经验与设计取舍

对 canonical 中既有的只读真实回测产物复核显示：过去候选常出现单月正收益
或高胜率，但在较长窗口中转负。例如 `DeepSeekRegimeCrossoverCandidateB`
在长窗交易稀疏，无法满足每窗 30 笔；多种 RSI、scalper 和快速趋势候选虽有
较高交易数或胜率，却在长期手续费后亏损。由此采用以下约束：

1. 不把单月、单 regime 或高胜率当作稳定性证明。
2. 使用固定、整值、可解释参数，不进行 OOS 反向调参或大规模 hyperopt。
3. 用趋势、突破、均值回归、量价、波动率和 regime hybrid 构造真正不同的
   信号族，而不是只替换 RSI 阈值。
4. 所有 rolling channel 基于当前/历史 candle；突破边界和统计基线使用
   `shift(1)`。禁止负向 `shift`、负 `iloc`、网络、文件、环境变量和动态执行。
5. 评分门槛与晋级门槛分开：`score >= 50` 只是必要条件，不能覆盖独立 OOS/
   walk-forward 失败。

## 策略与参数假设

| 候选 | 核心参数 | 适用假设 | 主要风险 |
|---|---|---|---|
| Candidate01EmaAdxTrend | EMA 20/50/200, ADX 20, RSI 48/52 | 有方向的持续趋势 | 低 ADX 边界附近反复扫损 |
| Candidate02DonchianAtrBreakout | Donchian 48, ATR 14/96, EMA 100 | 波动扩张突破 | 假突破和快速反转 |
| Candidate03BollingerRsiReversion | BB 24/2.2, RSI 10, ADX < 28 | 震荡均值回归 | 逆向承接真突破 |
| Candidate04MacdVolumeMomentum | MACD 12/26/9, EMA 100, volume 32 | 放量动量延续 | 清算量峰造成假确认 |
| Candidate05KeltnerPullback | EMA 24/80/200, ATR 20 x 1.6 | 趋势中深回撤后恢复 | 信号稀疏、regime 切换滞后 |
| Candidate06StochasticReversal | STOCHF 14/3, BB 20/2 | 短时超买超卖回摆 | 单边行情振荡器钝化 |
| Candidate07RocAdxImpulse | ROC 12, 96-bar 基线 x 1.6, ADX 22 | 大幅动量冲击延续 | 冲击后立即反转 |
| Candidate08IchimokuCloud | 9/26/52 非前移 cloud | 中期结构趋势 | 云层窄时频繁穿越 |
| Candidate09ObvPriceConfirmation | EMA 16/48, OBV EMA 34 | 量价共同确认 | 交易所成交量失真 |
| Candidate10AdxRegimeHybrid | ADX 24, EMA 24/72, BB 24/2 | 趋势/震荡显式切换 | regime 判断滞后 |

## 验证窗口

| 窗口 | 类型 | 市场状态 | BTC 收益 | 用途 |
|---|---|---:|---:|---|
| 20230701-20231001 | PRIMARY | bear | -11.46% | 主评分与初筛 |
| 20231001-20240301 | WALK_FORWARD | bull | +126.76% | 牛市稳定性 |
| 20240301-20240629 | WALK_FORWARD | range | -1.36% | 震荡稳定性 |
| 20250101-20251001 | OOS | bull | +21.72% | 独立样本外 |
| 20251001-20260201 | WALK_FORWARD | bear | -31.03% | 熊市稳定性 |

市场状态使用项目的 `window-close-return-v1` 规则：收益不低于 +5% 为 bull，
不高于 -5% 为 bear，其余为 range。

## 结果表

表中收益和回撤均已计入 Freqtrade 双边费率，并追加双边滑点压力；窗口单元格
格式为“交易数 / 净收益”。“Score”是主窗按 `phase2-quality-v1` 公式计算的
筛选分数，假定本轮静态审查/验证信号通过且无历史 failure record；它不是
数据库中持久化的正式 `StrategyScore`。

| 候选 | 可加载/Lookahead | 主窗交易/收益/回撤 | Score ≥ 50 | WF bull | WF range | OOS | WF bear | 晋级 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Candidate01EmaAdxTrend | PASS / PASS | 87 / -1.77% / 1.81% | 66.11 / 是 | 137 / -2.82% | 109 / -1.94% | 246 / -1.53% | 117 / -2.32% | 否 |
| Candidate02DonchianAtrBreakout | PASS / PASS | 114 / -2.16% / 2.35% | 63.94 / 是 | 205 / -2.76% | 171 / +1.39% | 397 / -6.21% | 178 / -2.41% | 否 |
| Candidate03BollingerRsiReversion | PASS / PASS | 139 / -1.71% / 1.74% | 70.21 / 是 | 257 / -4.36% | 187 / -3.47% | 439 / -5.27% | 193 / -4.25% | 否 |
| Candidate04MacdVolumeMomentum | PASS / PASS | 157 / -1.10% / 1.45% | 66.81 / 是 | 256 / -2.60% | 254 / -4.50% | 512 / -6.52% | 235 / -2.95% | 否 |
| Candidate05KeltnerPullback | PASS / PASS | 26 / -0.63% / 0.84% | 71.11 / 是 | 36 / +1.06% | 24 / -0.01% | 83 / -1.42% | 40 / +0.70% | 否 |
| Candidate06StochasticReversal | PASS / PASS | 235 / -4.46% / 4.54% | 59.83 / 是 | 463 / -7.80% | 364 / -5.17% | 821 / -10.27% | 370 / -7.48% | 否 |
| Candidate07RocAdxImpulse | PASS / PASS | 187 / -2.52% / 2.89% | 63.00 / 是 | 354 / -4.50% | 290 / -2.99% | 599 / -8.48% | 278 / -1.41% | 否 |
| Candidate08IchimokuCloud | PASS / PASS | 258 / -2.43% / 3.07% | 62.78 / 是 | 419 / -5.21% | 335 / -3.57% | 700 / -8.97% | 292 / -2.52% | 否 |
| Candidate09ObvPriceConfirmation | PASS / PASS | 161 / -2.26% / 2.63% | 64.08 / 是 | 300 / -5.64% | 224 / +1.80% | 494 / -5.86% | 220 / -1.35% | 否 |
| Candidate10AdxRegimeHybrid | PASS / PASS | 252 / -4.15% / 4.15% | 59.27 / 是 | 432 / -6.46% | 334 / -2.53% | 750 / -7.09% | 309 / -4.19% | 否 |

Candidate05 在 bull/bear 局部为正但 range 窗只有 24 笔且净收益略负；
Candidate02、Candidate09 只在 range 为正。其余候选在所有验证 regime 中
均未显示可接受的成本后稳定性。上述候选可以继续作为研究输入，但不能进入
OKX_DEMO 部署候选清单。

## 策略 hash

| 候选 | SHA-256 |
|---|---|
| Candidate01EmaAdxTrend | `a512f972b93ca7b0b230830cb3b310ec320d6d86fda2708ff494e8f9c4455289` |
| Candidate02DonchianAtrBreakout | `b7c429b9e1e16004d5bec811b82f613a42ce65a8867a5777ee60045bc50b4c8c` |
| Candidate03BollingerRsiReversion | `355b9646a23f6582b9a520bbe1186df09372563ad29bf4f311404ced78291afa` |
| Candidate04MacdVolumeMomentum | `14c795509502215068319cc080e868b8d8a7437999fc93d47ab5dea67179e50b` |
| Candidate05KeltnerPullback | `39bbaea384b97e950ce2502fb5317da7eaa585ab52f4ea6f5891ea67b6789358` |
| Candidate06StochasticReversal | `aba93f569f02f043715d0caaad65e9fb18471a644f57041decd2b1449fc776d9` |
| Candidate07RocAdxImpulse | `9b75dfbee3d03300ef745868a77fbdd5745346a20dd68af137034017f88eaae8` |
| Candidate08IchimokuCloud | `b3fe6b42d6fefc82f2abfa53cc89742e4ddf92c55c1ef52fdc41fc4b8fea2a3f` |
| Candidate09ObvPriceConfirmation | `e340d4ac98760bf8dd9b7eb6dbc720522db8a255d99dba5a306c2db100638ebf` |
| Candidate10AdxRegimeHybrid | `2a86690e9aca012c3c5c5760f4c57f123fb34b7e12af20930f0edbfc4ed52a3a` |

## 可部署候选与最小交接建议

可部署候选清单：`[]`。

当前唯一主任务应保持现有 OKX_DEMO active 策略不变，不导入、不选择、不激活
本轮任何文件。后续若继续研究，优先对 Candidate05 做仅 IS/主窗的低频参数
研究，然后在完全未用于调参的新 OOS 上重验；任何候选仍必须通过项目正式的
持久化 `BacktestRun` / `BacktestTask` / `BacktestResult`、artifact manifest、
market-data lineage 和 `StrategyValidationPlan`，再在最多 3 个 active 策略及
现有全局限额内由主任务决定是否部署。
