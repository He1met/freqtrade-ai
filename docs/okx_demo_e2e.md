# OKX Demo E2E acceptance

Issue #452 has two deliberately separate modes:

- `OFFLINE_CI` exercises all declared failure and recovery scenarios with an
  isolated deterministic gateway. It performs no exchange network access and
  does not read credentials.
- `CONTROLLED_REAL` may only use a `NORMAL_PIPELINE` gateway supplied by the
  single supervised runtime and writer from #449/#450. The current independent
  framework returns `NOT_RUN` without explicit authorization and `BLOCKED`
  until the concrete, registered adapter is integrated. A test double cannot
  authorize this mode merely by setting a gateway-kind string or success flag.

`scripts/okx_demo_e2e.py` is an acceptance orchestrator, not an exchange
adapter. In particular, it must never import the direct HTTP canary transport
or construct another order writer.

## Verdicts

- `NOT_RUN`: controlled Demo execution was not explicitly authorized.
- `BLOCKED`: prerequisites or the normal-pipeline gateway are unavailable.
- `FAILED`: execution completed and proved a scenario or projection mismatch.
- `DRIFTED`: the final database identity, order set, or position set differs
  from the captured baseline.
- `RECOVERY_REQUIRED`: cleanup or final state cannot be proved.
- `PASSED`: every scenario, projection, database lineage and final cleanup is
  proved for the report's declared mode.

An offline `PASSED` report proves the CI state machine only. It drives
scenario-specific order, fill, idempotency, timeout, sequence, restart, drift,
staleness and target-guard transitions, then verifies baseline restoration.
Its evidence is explicitly marked `OFFLINE_FIXTURE`, and
`real_demo_executed` is always false.

The CLI exits `0` only for `PASSED`; `NOT_RUN`/`BLOCKED` exit `2`,
`FAILED` exits `1`, and `DRIFTED`/`RECOVERY_REQUIRED` exit `3`. Automation therefore
cannot confuse an honest non-run or blocker with successful acceptance.

## Evidence and cleanup

The gateway captures one safe SHA-256 database identity before execution.
Every scenario references its declared evidence source. Controlled real
evidence must be `DATABASE` evidence from the registered adapter and also
contain at least one OKX Demo `ordId`; #449/#450 must validate the canonical
run/attempt/order/reconciliation joins rather than trust caller-supplied IDs.
The final snapshot must use the same database fingerprint and exactly restore
the baseline orders and positions. A proven mismatch is `DRIFTED`; an
unprovable cleanup is `RECOVERY_REQUIRED`, even when earlier checks passed.
Controlled-real reports are mandatory and remain under the one user-level
runtime root `~/.freqtrade-ai/runtime/okx-demo-e2e`; worktree-local evidence
directories are rejected.

## Dependency integration

1. #449 supplies the gateway from the one supervised runtime and writer.
2. #450 dispatches each scenario through the durable normal pipeline.
3. #448 supplies authoritative reconciliation and cleanup snapshots.
4. #451 supplies safe page/API projections for cross-surface verification.

Until those dependencies are merged, no real Demo result may be reported.
