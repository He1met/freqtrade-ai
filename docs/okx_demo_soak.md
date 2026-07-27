# OKX_DEMO seven-day soak acceptance

Issue #453 adds an auditable acceptance state machine. It does **not** claim
that a seven-day run has happened. A new run is persisted as `NOT_RUN`, and it
can only become `RUNNING` after the normal #452 E2E has persisted `PASSED`
evidence and demonstrated at least one complete order lifecycle, reconciliation
cycle, and cleanup cycle.

## Single-environment boundary

The soak monitor is a component of the existing supervised `okx_runtime`. It is
not a daemon, cron job, second LaunchAgent, second writer, or separate database.
Every start gate and every probe records the observed cardinality for:

- repository;
- LaunchAgent/runtime;
- PostgreSQL database;
- Python virtual environment;
- OKX order writer.

Each cardinality must be exactly one. The active execution target must be
`OKX_DEMO`. The environment identities are reduced to one SHA-256 fingerprint;
paths, URLs, credentials, and raw payloads are never accepted as evidence
references.

## Durable state

The one PostgreSQL schema owns three tables:

- `okx_demo_soak_runs`: current status, gate evidence, cadence, and final
  evidence;
- `okx_demo_soak_probes`: append-only runtime, reconciliation, resource-growth,
  queue, order, and position observations;
- `okx_demo_soak_events`: append-only transition and incident timeline.

The public status vocabulary is exact:

- `NOT_RUN`
- `BLOCKED`
- `RUNNING`
- `FAILED`
- `RECOVERY_REQUIRED`
- `PASSED`

Only one `RUNNING` or `RECOVERY_REQUIRED` run may exist. Probes and events are
insert-only for the runtime PostgreSQL role. The service rejects non-monotonic
probe or event time, unsafe evidence references, a probe gap beyond the
configured maximum, and any target other than `OKX_DEMO`.

## Freeze, reconciliation, and recovery

Single-environment drift, an unhealthy runtime or WebSocket, reconciliation
drift, an unknown position, or a probe gap moves the run to
`RECOVERY_REQUIRED` and freezes new openings. Recovery must follow the ordered
states `FROZEN -> RECONCILING -> RECOVERING -> ACTIVE`; only
cancel/reduce-only recovery is allowed by the normal writer integration.
Recovery completion requires `RECONCILED` or `RECOVERED`, zero unexpected open
orders, and zero unknown positions.

Credential exposure and duplicate orders are terminal `FAILED` evidence. They
cannot be cleared by a later healthy probe.

## Pass predicate

`PASSED` is possible only when all of the following are true at controlled
shutdown:

1. elapsed real time is at least 604800 seconds;
2. the complete probe timeline exists and no probe gap exceeds the configured
   maximum;
3. the run is healthy and has no pending recovery;
4. repository, runtime, PostgreSQL, virtual environment, and writer cardinality
   are still exactly one;
5. controlled cleanup is complete with zero open orders and zero open
   positions;
6. final reconciliation is `RECONCILED` or `RECOVERED`;
7. opaque API, database, artifact, OKX order/fill/position, and runtime-log
   evidence references plus the SHA-256 report digest are present.

Short tests may exercise the predicate with an injected clock, but are not
production acceptance. Only timestamps and evidence written by the supervised
runtime against the real unique PostgreSQL qualify.

## Integration points for #448-#452

- **#448 reconciliation:** maps its authoritative status and recovery-batch ID
  into each probe and recovery transition. `DRIFTED`, `STALE`, and `UNKNOWN`
  freeze the run.
- **#449 runtime:** calls the service from the existing `okx_runtime` heartbeat.
  It owns scheduling and controlled stop; no new process is introduced.
- **#450 full chain:** supplies completed order-lifecycle coverage and durable
  order/fill/position evidence IDs.
- **#451 UI:** reads safe run/probe/event DTOs. It must never infer `PASSED` from
  page availability or an empty result.
- **#452 E2E:** supplies the persisted E2E evidence ID, exact `PASSED` status,
  reconciliation coverage, and cleanup coverage used by `SoakStartGate`.

Until those integrations are merged and a real continuous run completes, the
honest operational result is `NOT_RUN` or `BLOCKED`, never `PASSED`.
