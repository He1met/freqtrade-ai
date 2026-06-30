# Phase 2 Real Freqtrade Backtest Spike

## Run Command

Run from the repository root:

```bash
python3 scripts/spike_real_freqtrade_backtest.py
```

Optional arguments:

```bash
python3 scripts/spike_real_freqtrade_backtest.py \
  --freqtrade-bin /path/to/freqtrade \
  --market-data-dir user_data/data \
  --tmp-dir /tmp/freqtrade-ai-real-backtest
```

The command writes a Markdown report to:

```text
reports/spikes/phase2_real_freqtrade_backtest_latest.md
```

That report path is ignored by git because it is runtime evidence.

## What The Spike Checks

- Whether a real `freqtrade` command is available.
- Whether local market data exists under `user_data/data`.
- Whether a minimal generated Freqtrade strategy file can be written.
- Whether a temporary backtesting config can be generated without credentials.
- Whether real `freqtrade backtesting` exits successfully.
- Whether a result JSON is generated and can be parsed.
- Whether total profit, max drawdown, trade count, and win rate can be extracted.

## Output Status

- `SUCCESS`: real local backtesting ran and required metrics were parsed.
- `BLOCKED`: a required local prerequisite is missing, usually `freqtrade` or local data.
- `FAILED`: the command ran far enough to attempt the spike but Freqtrade execution or result parsing failed.

When `BLOCKED`, read the report's `Blockers` section. The command does not fake success.

## Current Limits

- Does not run dry-run.
- Does not run live trading.
- Does not call `freqtrade download-data`.
- Does not connect to a real exchange.
- Does not read or write real API keys, secrets, or passphrases.
- Does not modify Freqtrade source code.
- Does not run Hyperopt.
- Does not perform multi-window, random-window, or Phase 3 batch backtesting.
