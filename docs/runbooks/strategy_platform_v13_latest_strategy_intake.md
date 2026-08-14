# Canonical V1.3 latest-only strategy intake

This runbook imports source code only. It does not migrate a legacy database identity,
version number, lifecycle status, score, qualification, backtest, deployment, runtime,
signal, order, fill, or ledger record.

## Safety boundary

- Use an explicit absolute path to a read-only filesystem source archive. Never provide
  a database URL and never query a legacy database.
- The manifest considers exact `*_run_<positive integer>_1.py` files. Every file must
  contain exactly one top-level class. One highest, unambiguous run is selected per
  class; all visible run identities are included in the archive digest.
- Selected code is decoded as strict UTF-8, normalized, secret-scanned, and parsed as
  AST only. It must define one `IStrategy` subclass, use only the frozen import
  allowlist (`freqtrade.strategy`, `functools`, `pandas`, `talib.abstract`), contain no
  module-level executable statement, decorator, or banned call. The process never
  imports, compiles to bytecode, or executes submitted code.
- The API creates an independent canonical strategy and canonical version `1` in
  `DRAFT` / `UNVALIDATED` state. `execution_authorized=false`. Content digest is the
  only artifact deduplication key; equal code never merges strategy identities.
- The API must report `TRADING_DISABLED`. Do not run validation, lookahead, backtest,
  research, qualification, optimization, deployment, runtime, or trading in this gate.

## Plan

Use the release checkout Python environment. Store evidence outside the repository and
outside the read-only source archive. The evidence file is atomically replaced with
mode `0600` and contains paths relative to the supplied root, never the absolute root or
source code.

```bash
backend/.venv/bin/python scripts/canonical_v13_latest_strategy_intake.py plan \
  --source-root /absolute/read-only/source/user_data/strategies \
  --evidence-output /absolute/private/evidence/latest-strategy-intake-plan.json
```

Accept the plan only when `status=PLANNED`, `visible_file_count` matches the captured
archive, every top-level class occurs once in `entries`, every `safety_result=PASSED`,
and the recorded `archive_snapshot_digest`, selected path, run, class, and code digest
have been independently reviewed. A rejected or ambiguous artifact blocks apply; do not
silently omit it.

## Apply through the canonical control API

The API must be the loopback-only standalone canonical service. It must use its mapped
control writer principal; do not mount this route on the legacy service and do not use a
legacy database URL.

```bash
backend/.venv/bin/python scripts/canonical_v13_latest_strategy_intake.py apply \
  --source-root /absolute/read-only/source/user_data/strategies \
  --api-origin http://127.0.0.1:8011 \
  --expected-archive-digest <reviewed-plan-archive-snapshot-digest> \
  --evidence-output /absolute/private/evidence/latest-strategy-intake-apply.json
```

The adapter preflights every selected artifact before the first request, then submits
one class at a time. The evidence file is updated after each receipt. A network failure
may leave a safe partial batch; rerun the exact same archive because request keys are
deterministic and the canonical endpoint is idempotent. Never change the archive under
an existing digest or reuse a key for different content.

## Acceptance

Require all of the following:

1. The second exact apply reports every row with `idempotent_replay=true` and the same
   submission, strategy, version, artifact, intake receipt, and receipt digests.
2. Strategy count and version count equal the number of accepted classes; every version
   is canonical version `1`, `UNVALIDATED`, and not execution-authorized.
3. `strategy_intake_receipts`, `idempotency_receipts`, and `audit_events` contain one
   exact-lineage accepted record per class. Duplicate source digests may reduce the
   artifact count only; they must not reduce strategy or version counts.
4. Validation plans/attempts/results, scores, qualification decisions, optimization,
   deployment/runtime, signals, orders, fills, ledger, and reconciliation remain zero.
5. Restart the standalone API and confirm the strategy catalog is unchanged. Research
   stays blocked without an active bundle/first backtest, runtime stays blocked with
   `TRADING_DISABLED`, and optimization stays `PENDING_FIRST_BACKTEST`.

Preserve the evidence files and SHA-256 digests without source bytes or secrets. This
gate does not authorize the fresh-market or first-backtest gates.
