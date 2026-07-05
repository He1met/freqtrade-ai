# Phase 9 Single DeepSeek E2E

This runbook is for issue `#277`: one controlled DeepSeek validation, or a
durable fail-closed evidence package when prerequisites are missing.

The default path does not call DeepSeek. A real request is sent only when
`--allow-real-call` is passed and `DEEPSEEK_API_KEY` exists in the local
environment.

## Safe Default

```bash
python3 scripts/phase9_deepseek_single_e2e.py --json
```

Expected default result:

- `status=BLOCKED`
- `can_accept_as_real_run=false`
- one `strategy_generation_runs` row with `provider=deepseek` and
  `status=failed`
- `real_call_attempted=false`
- no strategy, backtest, result, or score rows
- evidence JSON under `/tmp/freqtrade-ai-phase9-deepseek-e2e/`

This proves the project fails closed when the real call has not been explicitly
authorized.

## One Real Provider Attempt

Set the environment variable named `DEEPSEEK_API_KEY` only in the local shell,
then authorize the single call:

```bash
python3 scripts/phase9_deepseek_single_e2e.py \
  --allow-real-call \
  --json
```

The script requests exactly one strategy blueprint. It records provider/model
metadata and database IDs, but never records the key value.

If generation succeeds but local market data, Freqtrade binary, or artifacts
are missing, the report remains `BLOCKED` with durable DB evidence and required
actions.

## Artifact Completion

If local backtest preflight is `READY`, pass an existing real local backtest
artifact:

```bash
python3 scripts/phase9_deepseek_single_e2e.py \
  --allow-real-call \
  --manifest-path /tmp/freqtrade-ai-real-backtest/manifest.json \
  --json
```

`READY_FOR_REVIEW` requires all of these:

- DeepSeek generation run is `succeeded`.
- Strategy and strategy version are persisted.
- Strategy file exists under the approved local generated strategy directory.
- Local backtest task/result are persisted from a supplied artifact.
- Strategy score is persisted.
- Result and score `data_source.core_data=true`.
- `can_accept_as_real_run=true`.

Any missing key, missing explicit authorization, provider failure, local
preflight blocker, missing artifact, parse failure, or scoring failure keeps the
report non-acceptable and explains the next required action.

## Safety Boundary

- No live trading, real orders, `freqtrade trade`, production deploy, or market
  data download.
- No key value in code, config, database, report, Issue, PR, or page payload.
- Fake, fixture, fallback, and unknown data cannot satisfy this issue.
