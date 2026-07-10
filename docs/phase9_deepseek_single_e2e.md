# Phase 9 Single DeepSeek E2E

This runbook supports issue `#334`: one controlled DeepSeek validation, or a
durable fail-closed acceptance report when prerequisites are missing.

The default path does not call DeepSeek. A real request is sent only when
`--allow-real-call` is passed and `DEEPSEEK_API_KEY` exists in the local
environment.

The formal API entry is `POST /api/strategy-generation-runs/deepseek-single`.
It fixes the request count at one and requires `allow_real_call=true`. Without
that flag it persists a `BLOCKED` operation evidence record and never invokes
the Provider. Fake and fixture providers use the existing generation endpoint
and cannot satisfy this acceptance path.

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
- evidence JSON and Markdown acceptance report under
  `/tmp/freqtrade-ai-phase9-deepseek-e2e/`

This proves the project fails closed when the real call has not been explicitly
authorized, and gives QA a report that can stay `BLOCKED` without inventing a
successful DeepSeek or Freqtrade run.

## One Real Provider Attempt

Set the environment variable named `DEEPSEEK_API_KEY` only in the local shell,
then authorize the single call:

```bash
python3 scripts/phase9_deepseek_single_e2e.py \
  --allow-real-call \
  --json
```

The script requests exactly one strategy blueprint. It records provider/model
metadata, database IDs, missing conditions, reproduction commands, and next
steps, but never records the key value.

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

## Acceptance Report Output

Every run writes two redacted artifacts:

- `phase9-deepseek-single-e2e-evidence.json`
- `phase9-deepseek-single-e2e-acceptance.md`

The Markdown report is intended for QA and review. It includes:

- final verdict (`READY_FOR_REVIEW`, `BLOCKED`, or `FAILED`);
- missing conditions such as missing approval, missing `DEEPSEEK_API_KEY`,
  missing `freqtrade` binary, blocked local data preflight, or missing real
  backtest artifacts;
- exact rerun commands;
- next-step guidance; and
- database/API/UI evidence IDs when available.

No report path or field may contain an API key value.

## Safety Boundary

- No live trading, real orders, `freqtrade trade`, production deploy, or market
  data download.
- No key value in code, config, database, report, Issue, PR, or page payload.
- Fake, fixture, fallback, and unknown data cannot satisfy this issue.
