# Phase 7 Worker / Queue Architecture Design

Issue `#202` is a design-only Phase 7 artifact. It describes how future worker
and queue infrastructure should be shaped after explicit approval, but it does
not add Redis, Celery, Kafka, RabbitMQ, worker services, runtime config, Docker
services, or executable queue code.

## Design Goals

The future worker / queue layer should make long-running engineering work
auditable and repeatable without turning this project into a trading control
plane.

Goals:

- Run offline engineering tasks with clear ownership, status, and evidence.
- Preserve the read-only runtime and governance boundaries from Phase 6 and
  Phase 7.
- Attach every task attempt to governance events and artifacts.
- Fail closed on missing local prerequisites, unsafe inputs, or secret-shaped
  payloads.
- Keep operator-visible status stable enough for the Phase 7 dashboard and
  smoke checks.

Non-goals:

- No live trading, real order placement, live bot start / stop, or deploy
  control.
- No exchange connectivity or real K-line download orchestration.
- No production deployment executor.
- No Freqtrade source modification.
- No queue implementation in issue `#202`.

## Candidate Task Types

The first implementation issue, if approved later, should start with safe,
offline, repo-local work only:

| Task type | Purpose | Allowed inputs | Explicitly disallowed |
| --- | --- | --- | --- |
| `backend_test` | Run backend pytest in the project venv | Commit/ref, test selector | Secrets, exchange credentials |
| `python_compileall` | Compile backend/tests/scripts | Commit/ref | Runtime credentials |
| `frontend_build` | Run `npm run build` | Commit/ref | Live API endpoints |
| `secret_scan` | Run repo-local secret gate | Commit/ref, tracked-file scope | Printing matched values |
| `offline_smoke` | Run approved smoke scripts | Phase id, tmp dir | Live or dry-run startup |
| `artifact_index` | Collect safe artifact manifests | Relative artifact paths | Raw logs with secret values |
| `governance_report` | Summarize accepted / blocked evidence | Governance event ids | Human approval bypass |

Future phases may add more task types, but each new type should have its own
Issue, acceptance criteria, fixture coverage, and safety review.

## Task State Model

Workers should expose a small, explicit state machine. State transitions should
be append-only audit events rather than mutable log lines.

| State | Meaning | Terminal |
| --- | --- | --- |
| `ACCEPTED` | Request validated and recorded, but not yet queued | No |
| `QUEUED` | Eligible for a worker lease | No |
| `CLAIMED` | A worker has a lease but has not started execution | No |
| `RUNNING` | Attempt is executing | No |
| `RETRY_WAIT` | Attempt failed with a retryable reason and backoff | No |
| `BLOCKED` | Missing prerequisite or policy violation needs human action | Yes |
| `SUCCEEDED` | Task completed and artifacts were recorded | Yes |
| `FAILED` | Task exhausted retries or hit a non-policy runtime failure | Yes |
| `CANCELLED` | Human cancelled before terminal execution | Yes |
| `EXPIRED` | Lease or queue TTL elapsed without a safe retry path | Yes |

Allowed transitions:

- `ACCEPTED -> QUEUED`
- `QUEUED -> CLAIMED -> RUNNING`
- `RUNNING -> SUCCEEDED`
- `RUNNING -> RETRY_WAIT -> QUEUED`
- `RUNNING -> BLOCKED`
- `RUNNING -> FAILED`
- `QUEUED -> CANCELLED`
- `CLAIMED -> EXPIRED`

`BLOCKED` should be used for safety and prerequisite failures such as missing
fixture data, unsafe runtime mode, secret-shaped payloads, unavailable local
tools, or attempts to request live / deploy / order controls.

## Idempotency And Retries

Each task request should include an idempotency key derived from stable,
non-secret inputs:

- task type;
- repo commit SHA or immutable artifact revision;
- normalized command profile;
- safe input manifest hash;
- requested phase and issue number.

Retries should create new attempts under the same task id. Attempt artifacts
must use attempt-scoped directories so partial output cannot overwrite a
previous result.

Retry policy:

- Retry only infrastructure-shaped failures such as transient process exit,
  temporary filesystem access, or worker lease loss.
- Do not retry policy failures: secret findings, live trading requests,
  missing required local data, missing ENV references, or unsafe config.
- Use bounded exponential backoff and a small max attempt count.
- Preserve the first failure reason and the final terminal reason.

## Failure Recovery

The queue design should use leases and heartbeats so abandoned work can be
recovered safely.

Recovery rules:

- A worker claims one task attempt with a finite lease.
- The worker periodically writes heartbeat time and safe progress metadata.
- If heartbeat is stale, the attempt moves to `EXPIRED` or `RETRY_WAIT`
  depending on retry eligibility.
- Partial artifacts are retained under attempt-scoped paths and marked
  incomplete.
- Recovery must not rerun tasks that reached `BLOCKED`, `FAILED`,
  `CANCELLED`, or `SUCCEEDED`.

No recovery path may start Freqtrade live/dry-run, place orders, deploy to
production, or connect to a real exchange.

## Audit Event Integration

Every state transition should emit or reference a governance event compatible
with the Phase 7 audit event work from issue `#199`.

Minimum event fields:

- task id and attempt id;
- issue number and phase;
- actor type (`operator`, `automation`, or `worker`);
- source component;
- previous state and next state;
- reason code;
- safe artifact links;
- timestamp;
- payload hash;
- redaction status.

The event payload should store only safe metadata. Secret-shaped fields should
be rejected or redacted before persistence. Audit output must never include API
keys, API secrets, passphrases, tokens, private keys, authorization headers, or
real credential values.

## Artifact Relationship

Artifacts should be referenced by manifest, not by raw uncontrolled output.

Recommended manifest fields:

- artifact id;
- task id and attempt id;
- relative path under an approved artifact root;
- content type;
- size and checksum;
- created timestamp;
- producing command profile;
- safety flags;
- redaction summary.

Raw logs should be treated as untrusted until scanned and redacted. Dashboard
links should point to safe summaries or manifests, not directly to arbitrary
local paths.

## Secret Boundary

The worker / queue layer may store ENV variable names, presence checks, and
redaction decisions. It must not store or print ENV values.

Rules:

- Request payloads must reject secret-shaped values.
- Task logs and artifacts must pass the repo-local secret scanner before being
  linked in operator-facing output.
- Queue metadata may reference `*_ENV` names but not dereference them into
  stored payloads.
- Missing secret prerequisites should become `BLOCKED`, not best-effort
  execution.
- Test fixtures must use obviously fake values that cannot be mistaken for
  real credentials.

## Implementation Options

This issue does not choose or implement a queue provider. A future issue should
select one option only after the gates below are met.

| Option | Strengths | Risks / reasons to defer |
| --- | --- | --- |
| Database-backed task table | Simple audit joins, fewer moving parts, good first implementation | Needs careful lease and polling design |
| Local process queue | Easy local development and tests | Not durable enough for multi-process recovery |
| Redis queue / RQ | Common lightweight queue model | Adds infrastructure and operational dependency |
| Celery | Mature retries and worker ecosystem | Broad dependency surface and configuration complexity |
| RabbitMQ | Strong broker semantics | Heavy for current local-first scope |
| Kafka | Durable event stream | Too large for current task orchestration needs |
| Managed cloud queue | Operational reliability | Couples project to a deployment platform |

Recommended future path: start with a database-backed, offline-only task table
and a single local worker process in a later, dedicated implementation issue.
Redis, Celery, RabbitMQ, Kafka, or managed queues should remain deferred until
there is evidence that the database-backed path is insufficient.

## Gating Conditions Before Implementation

No worker / queue code should be added until a new Issue explicitly approves an
implementation scope.

Required gates:

- A dedicated implementation Issue with non-XL size and clear acceptance
  criteria.
- Explicit decision on queue provider and storage schema.
- Tests for state transitions, lease expiry, retry behavior, and blocked
  safety paths.
- Audit event integration tests.
- Secret scanning coverage for task payloads, logs, and artifacts.
- Offline smoke coverage that proves no live trading, exchange connection,
  K-line download, production deploy, or Freqtrade source modification.
- PR body listing skipped live/production capabilities as safety boundaries.

Until those gates are met, Phase 7 should treat worker / queue work as
architecture planning only.
