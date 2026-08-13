# V1.3 no-trade runtime configuration readiness

This slice verifies one already materialized immutable research bundle. It does
not register, validate, activate, or materialize owner configuration and does
not start a worker, backtest, exchange client, signal, deployment, or order
path.

## Explicit inputs

The process must receive both non-secret values:

```text
FREQTRADE_AI_V13_NO_TRADE_MODE=1
FREQTRADE_AI_CONFIGURATION_BUNDLE_SNAPSHOT_ID=<positive owner-issued id>
```

Neither value has a default. The database connection must independently point
to `freqtrade_ai_design_lab` as the least-privilege `freqtrade` role. The
runtime must not call the owner resolver and must not receive owner, schema,
sequence, or write privileges.

## Fail-closed contract

`GET /api/v1/runtime/configuration-readiness` and, only when the explicit mode
is enabled, `GET /readyz` require:

- accepted schema `20260813_47` and an immutable persisted bundle;
- `RESEARCH / WORKFLOW / production-research-v13` identity;
- exact v2 generation, diversity, quality, scoring, and research profiles;
- `diversity-threshold-v2` and `profile-bound-score-v2`;
- exact profile cross-bindings and equal scoring/quality rule contracts;
- Demo-only, `allow_real_funds=false`, and single-writer invariants;
- recomputed version, dependency, bundle, adapter-registry, and installed
  manifest digests.

The readiness response contains only immutable ids, digests, derived counts,
and safety status. Credential/attestation remains `UNKNOWN_OUT_OF_SCOPE`, and
worker, backtest, signal, and order capabilities remain `DISABLED`.

Missing input, wrong role/database/scope/schema, digest drift, a v1 adapter,
profile cardinality mismatch, or any safety mismatch returns HTTP 503. Do not
substitute a legacy 60/6 count or a default scoring formula.
