# Qualified strategy queue architecture

## Boundary and rollout state

This contract separates candidate generation, validation, qualified Demo
continuation, and natural runtime acceptance. Generation never implies that a
backtest ran. Queueing never implies that a strategy was deployed.

The current Draft PR implements the two operation-scoped queue entrypoints,
leases, idempotency, stale retry contract, and Demo-only qualification gate. It
does not yet implement the dedicated validation consumer, the read-only API, or
the frontend workspace. Those remain blocked from merge/deployment until the
evaluator receipt repair is terminal and canonical state is freshly verified.

```text
generated Blueprint
  -> GENERATED_QUEUED
  -> leased validation steps
  -> QUALIFIED | REJECTED | VALIDATION_FAILED
  -> QUALIFIED_PENDING_CANONICAL_VALIDATION (QUALIFIED only)
  -> canonical validation/approval/deployment
  -> natural closed-candle evaluation
  -> NO_ACTION | ACTIONABLE | BLOCKED | FAILED
```

`NO_ACTION` is a successful terminal natural observation. No stage may replay
or manufacture a signal or order.

## Efficiency contract

- Generation, queue claiming, backtesting, terminal projection, and qualified
  continuation are separate stages. Durable identities and indexed queue state
  replace repeated full-batch scans and prevent completed backtests from rerunning.
- A validation consumer claims exactly one executable job per lease. Crash or
  timeout produces a durable `STALE` state; only explicit fenced recovery may
  retry the same idempotent job.
- Expensive Freqtrade validation remains serial. Fast source/Blueprint/data/schema
  prechecks and API contract tests run before it, so deterministic failures do not
  consume a backtest slot.
- The page reads one server-built workspace snapshot containing
  `current/waiting/completed/unknown`; it does not join multiple endpoints or use
  high-frequency polling to synthesize progress.
- Efficiency never changes OOS/quality thresholds, the 15% drawdown contract,
  Demo-only flags, the single writer, evidence freshness, or natural-signal rules.
  Missing input is a recorded fail-closed result, not a reason to skip a gate.

## Persistence contract

The first compatible implementation reuses `research_jobs` and
`research_job_attempts`; it does not add writes to protected trading tables.
Later normalization must preserve the identities and state meanings below.

| Concern | Durable identity / field | Contract |
|---|---|---|
| Generated candidate | operation + run ID + candidate key + source digest | Persisted before backtest; exact source and Blueprint digest bound |
| Validation job | `research_jobs.id` | `job_type=formal_candidate_validation`, `operation=strategy_research.candidate_validation_queue_v1` |
| Queue position | `(created_at, id)` among eligible `PENDING` rows | Server-derived; never stored or guessed by the browser |
| Lease | owner, token, expiry, heartbeat, attempt count | One global active research lease; operation-scoped claims only |
| Step progress | persisted stage/checkpoint | No percentage inferred from wall-clock time |
| Terminal validation | `QUALIFIED`, `REJECTED`, `VALIDATION_FAILED` | Includes structured reason codes and immutable evidence references |
| Qualified continuation | candidate ID + report/event/source digests | Exactly-once `qualified_demo_deployment` job; still requires canonical validation |
| Retry | same job ID, incremented attempt, explicit recovery stage | Lease expiry becomes `STALE`; no implicit retry of an unknown external outcome |
| Versioning | queue contract, quality policy, repository commit, Blueprint renderer | Any mismatch fails closed; idempotency keys never float across versions |

Validation steps are an ordered server enum:

1. `GENERATED_QUEUED`
2. `INPUT_EVIDENCE_CHECK`
3. `LOOKAHEAD`
4. `PRIMARY_BACKTEST`
5. `OOS`
6. `WF_BULL`
7. `WF_RANGE`
8. `WF_BEAR`
9. `SCORING`
10. `TERMINAL`

A consumer may use strictly bounded concurrency in the future, but every job
still requires a distinct lease/fence and deterministic capacity configuration.
No throughput setting may alter the 15% drawdown contract, OOS/window gates,
fees/slippage, evidence requirements, or Demo-only flags.

Each terminal record exposes structured `reason_codes` plus evidence references
for market-data receipt, source digest, Blueprint digest, lookahead artifact,
backtest windows, score policy, and repository commit. It must not embed secrets,
credentials, generated code, lease tokens, or trading-table data.

## Read-only API projection

The planned endpoint is:

`GET /api/formal-strategy-research/queue-workspace`

It is a read-only projection assembled from one database snapshot. It has no
claim, retry, cancel, deployment, signal, or order side effects.

```json
{
  "schema_version": "formal-research-queue-workspace-v1",
  "availability": "AVAILABLE",
  "reason_code": "OK",
  "observed_at": "2026-08-11T00:00:00Z",
  "current": null,
  "waiting": [],
  "completed": [],
  "legacy": [],
  "counts": {
    "running": 0,
    "waiting": 0,
    "completed": 0
  }
}
```

`availability` is `AVAILABLE`, `UNAVAILABLE`, or `UNKNOWN`:

- `AVAILABLE`: every required query succeeded and at most one current lease is
  internally consistent.
- `UNAVAILABLE`: the schema or capability is deliberately not installed.
- `UNKNOWN`: any required query, relationship, lease, or evidence lookup is
  incomplete or contradictory. Arrays and counts must not be presented as an
  authoritative empty queue.

`current`, `waiting[]`, and `completed[]` use the same item contract:

| Field | Meaning |
|---|---|
| `job_id`, `attempt_number` | Stable database identity |
| `candidate_key`, `pair`, `timeframe` | Digest-bound candidate identity |
| `status`, `stage`, `reason_codes` | Persisted server state only |
| `queue_position` | Server-derived integer for waiting rows; otherwise null |
| `lease_state` | `NONE`, `ACTIVE`, `EXPIRED`, or `UNKNOWN`; never includes token |
| `started_at`, `heartbeat_at`, `completed_at` | Database timestamps |
| `evidence_refs` | IDs/digests and safe artifact references only |
| `qualified`, `deployment_queue_job_id` | Explicit persisted result; never inferred |
| `execution_target_id`, `allow_real_funds`, `real_orders` | Must be `OKX_DEMO`, false, false |

The projection sorts `current` first, waiting by `(created_at, id)`, and completed
by `(completed_at, id)` descending. More than one active lease, a missing
candidate identity, or a failed lifecycle query makes the whole authoritative
section `UNKNOWN`; the API must not silently drop the conflicting row.

## Migration and compatibility

- Existing terminal `strategy_research_batches` remain historical evidence. They
  appear under `legacy` with `queue_state=NOT_RECORDED_LEGACY`; they are never
  backfilled as queued or qualified from counts alone.
- The initial queue uses existing tables, so no migration is required by this
  Draft PR. A later normalized schema must be additive first, dual-read behind a
  schema-version gate, and remove old reads only after parity evidence.
- Migration verification must cover PostgreSQL constraints, indexes, runtime-role
  denial, owner-mediated writes, operation-scoped claim concurrency, rollback,
  and legacy projections.
- The producer and consumer can be disabled independently. Rollback stops new
  claims but preserves all pending/stale/terminal rows for audit; it never deletes
  or rewrites a terminal decision.
- The runtime role cannot enqueue, claim validation work, publish deployments, or
  write protected trading tables. Queue writers remain owner/control-plane paths;
  the unique canonical runtime retains exclusive natural signal/order ownership.
- Missing schema, stale ownership, expired receipts, migration mismatch, or
  rollback ambiguity is fail-closed and creates no deployment capability.

## Frontend workspace compatibility

The UI task can implement three sections without inventing state:

| UI section | API source | Required behavior |
|---|---|---|
| Current backtest, pinned | `current` | Show persisted stage, heartbeat and reasons; no animated fake percentage |
| Waiting list | `waiting[]` | Show server queue position and identity; do not predict start time |
| Completed list | `completed[]` | Show terminal status, rejection reasons and safe evidence references |

For `UNKNOWN` or `UNAVAILABLE`, the page replaces all three authoritative lists
with the matching diagnostic state. Cached browser data may be labeled historical
but cannot be rendered as current. `QUALIFIED` may show “queued for canonical
validation”; it must not show “deployed” until a real `StrategyDeployment` ID is
present. `NO_ACTION` is displayed as a normal natural evaluation outcome, not an
error and not an invitation to retry.

## Acceptance matrix

| Layer | Acceptance |
|---|---|
| Generation | Candidate/source/Blueprint is durably queued before any backtest starts |
| Efficiency | Indexed durable state avoids full rescans; fast prechecks precede serial backtests |
| Claiming | Operation-scoped lease has one owner; stale/missing ownership blocks |
| Validation | Lookahead, primary, OOS, bull/range/bear, costs, score and 15% drawdown remain mandatory |
| Terminalization | Every candidate has explicit terminal status, reason codes and evidence refs |
| Promotion | Non-qualified never creates continuation; qualified replay creates exactly one |
| Demo safety | No live/real override exists; runtime role and protected DML remain denied |
| Natural acceptance | Only closed-candle natural evaluation; `NO_ACTION` is valid |
| API | One-snapshot current/waiting/completed projection; incomplete evidence is `UNKNOWN` |
| UI | Current pinned, ordered waiting, completed history; no client-inferred progress |
| Compatibility | Legacy batches remain visible but never synthesized into queue state |
| Rollback | Stop producer/consumer, preserve durable rows, create no deployment/order side effect |
| Dependency | Evaluator receipt repair terminal and fresh canonical verification before merge/deploy |
