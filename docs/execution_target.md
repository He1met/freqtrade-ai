# ExecutionTarget contract

The project has one repository, one managed runtime, and one PostgreSQL
instance. `ExecutionTarget` identifies the sole destination that may eventually
receive exchange orders; it does not create a second environment.

## Current contract

- `OKX_DEMO` is the only configured and `ACTIVE` exchange execution target.
- It is an OKX `SWAP`, isolated-margin, simulated-trading account.
- `allow_real_funds` and implicit fallback are always false.
- `order_submission_enabled` is true for the sole `OKX_DEMO` writer after
  #450 activation. It authorizes no order by itself: fresh attestation,
  reconciliation, risk approval, limits, idempotency, and the unique writer
  must all pass. Setting it back to false is the configuration-level stop.
- `LOCAL_DRY_RUN` is a non-exchange local simulation scope. It cannot be used as
  an exchange order destination.

The canonical manifest is the `execution` section of `config/app.yaml`. The
same credential-free manifest is returned by:

- `GET /runtime/execution-target`
- `GET /api/runtime/execution-target`
- `GET /runtime/read-only` as `execution_target_manifest`

Health and operator status expose the same `OKX_DEMO` target ID. Startup logs
include only the target identity and boolean safety flags, never credential
values.

## Fail-closed rules

Application settings refuse to load when the execution section is missing or
when it contains duplicate, unknown, live, fallback, real-funds, mislabeled
Demo, or exchange-enabled `LOCAL_DRY_RUN` configuration. Submission may only
toggle between enabled and stopped inside that otherwise immutable Demo
contract. There is no default routing fallback when reading `config/app.yaml`.

Direct `Settings(...)` construction must also receive an already validated
manifest. Isolated unit tests and smoke runs use an explicit
`okx_demo_execution_target_manifest()` factory; there is no missing-value
default.
