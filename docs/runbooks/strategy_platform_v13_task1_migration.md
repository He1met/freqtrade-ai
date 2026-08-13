# Strategy Platform V1.3 Task 1 migration runbook

This runbook covers database structure, real-data migration, reconciliation, and
the controlled old-system-to-V1.3 ownership cutover. It never authorizes
credential access, OKX API calls, backtest execution, signal/order creation, or
trading deployment.

## Safety boundary

- `freqtrade_ai` is always inventoried first with a read-only repeatable-read
  transaction. Missing or stale evidence is `UNKNOWN`, never zero.
- The sole V1.3 owner database is the physically isolated database named exactly
  `freqtrade_ai_design_lab`. Runtime, workers, schedulers, and OKX routing must
  not point to it until migration, reconciliation, ACL, backup, and the explicit
  cutover fence have all passed.
- The migration is additive and forward-only. It records zero destructive,
  overwritten, and deleted rows. Historical primary keys and projected row
  digests must reconcile exactly.
- Legacy static/window `PASSED` is execution evidence only. It is never promoted
  to dynamic quality `QUALIFIED`.
- Legacy research/deployment rows without persisted configuration evidence keep
  `configuration_bundle_snapshot_id=NULL` and receive append-only `UNKNOWN`
  mappings. Immutability triggers are never disabled, and a migration-created
  generic bundle must not be presented as the historical configuration.
- The former shared database `freqtrade_ai` is the intended permanently
  read-only historical source after the maintenance-fenced cutover. Until that
  cutover is observed, legacy write capability may still exist and must be
  reported as `CUTOVER_PENDING`; Task 1 tooling nevertheless must never apply
  V1.3 DDL, DML, ACL, or schema-marker writes to it. Credential and execution
  attestation remain `UNKNOWN/OUT_OF_SCOPE` and fail closed.
- The V1.3 owner copy must contain no credential material. The source snapshot
  dump excludes TABLE DATA for `okx_demo_attestation_secrets` and
  `okx_demo_operator_consent_secrets` at `pg_dump` time; the dump process must
  not read those rows. Their schemas remain present and both restored tables
  must have row count zero. Existing attestation/runtime records are retained
  only as legacy audit history, never accepted as current capability evidence.

## Isolated copy

Create a current custom-format dump from an exported
`SERIALIZABLE READ ONLY DEFERRABLE` snapshot. Record the exported snapshot ID,
source start/completion timestamps, WAL LSN, database size, exact exclusion
arguments, dump size, and dump SHA-256. The dump command must include both
`--exclude-table-data=public.okx_demo_attestation_secrets` and
`--exclude-table-data=public.okx_demo_operator_consent_secrets`. Restore it into
the empty `freqtrade_ai_design_lab` database. Preserve roles, owners, and ACLs;
do not point any application process at the copy.

Before migration, compare the source and restored copy for schema marker, table
row counts, ID ranges, constraints, owners, ACLs, and the migration tool's
legacy projected content digest. Secret-table verification is count-only; no
query may select, log, hash, or otherwise expose a secret row or column.

## Market evidence contract

`migrate_strategy_platform_v13_task1.py` accepts only
`strategy-platform-v13-migration-market-evidence-v1`. It requires:

- an append-only corrected aggregate source matrix with status `PASSED` and a
  64-character digest;
- exactly BTC/ETH/SOL by 5m/15m, with repo-relative path plus absolute inspected
  path, full file SHA-256, size, row/time bounds, interval, zero gaps,
  duplicates, and nulls;
- exact five-window classification evidence, including formal boundaries,
  first/last close, calculated return, expected row count, file digest, and
  observed regime;
- `source_receipt_digest` and a non-future observation after the last closed
  candle.

Historical source receipts are never overwritten. The legacy aggregate remains
`BLOCKED`; only the separately hashed, path-only corrected matrix may be
`PASSED`. Its historical freshness is persisted as `UNKNOWN`, and every new
receipt is explicitly `NOT_STRATEGY_QUALIFICATION`.

## Controlled design-lab execution

```sh
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/migrate_strategy_platform_v13_task1.py \
  --database-url postgresql+psycopg://localhost/freqtrade_ai_design_lab \
  --evidence-file /absolute/path/task1-market-evidence.json \
  --evidence-file-sha256 4d83e33a8fcecaa8e5a27c919b4e633127c98cefa38c33fc9e4f8668e7eefb76 \
  --report-file /absolute/path/task1-migration-first.json \
  --report-identity reports/migrations/task1-migration-first.json \
  --request-id task1-design-lab-first \
  --actor task1-design-lab-owner
```

Run the same command again with the **same** first-report path and request
identity. The first command atomically writes a mode-0600 immutable artifact and
`<report>.sha256`; the repeat command must not overwrite it and instead writes
`<stem>.repeat<suffix>` plus its own SHA-256 sidecar. Before opening the database
transaction, the CLI proves the report directory supports private atomic
write-and-rename. The second result must return `repeat_noop=true` with the same
source and target snapshot digests and no new target, score, rule, mapping, job,
order, or deployment row. Acceptance records and independently rechecks both
artifact hashes.

`--report-file` is a private filesystem locator used only for local writes. It
is never persisted in the database, artifact payload, or stdout. The immutable
request identity uses the separately supplied, repo-relative
`--report-identity`; absolute paths and traversal are rejected.

Run the read-only reconciliation SQL after each attempt:

```sh
psql -X -d freqtrade_ai_design_lab \
  -f backend/scripts/strategy_platform_v13_task1_reconciliation.sql
```

Acceptance requires no unresolved mappings, conflicts, digest mismatches,
unvalidated constraints, orphaned versions, or migration-created `QUALIFIED`
summary. Existing canonical scores retain their original value, scoring version,
and backtest result. Scores without an exact validation-window result remain in
`strategy_scores` and receive an auditable `NOT_APPLICABLE/UNKNOWN` association;
no filler window score is generated.

## Legacy source and V1.3 owner boundary

`freqtrade_ai` must become a read-only historical source after the controlled
retirement, and must never receive Task 1 DDL, DML, ACL, or schema-marker
writes. Before retirement, report any surviving write capability explicitly as
`CUTOVER_PENDING`. The command rejects every database name except the physically
isolated `freqtrade_ai_design_lab`, which is the sole V1.3 owner DB for this
cutover.

Before the final V1.3 run, identify the exact old launchd labels, PIDs, writer
lease, and supervisor control-state path without reading process environments or
credentials. Persist a new `MIGRATION_SUSPENDED` generation, wait for the same
generation to be observed, then stop only the confirmed old services. The old
generation is never resumed. New services may start only with an explicit new
owner generation, the V1.3 DB URL, `OKX_DEMO-only`, `allow_real_funds=false`, and
execution fail-closed while credential/IP/OKX attestation remains `UNKNOWN`.

## Acceptance states

Task 1 has two explicit gates and must not blur them:

- `TASK1_DATABASE_MIGRATION_SUBGATE_ACCEPTED` means the sanitized owner copy,
  v47 structure, complete real-history mapping, exact-identity no-op replay,
  reconciliation, minimum read ACL, immutable reports, and restore-tested backup
  all passed. It does **not** authorize API/UI work or runtime activation by
  itself.
- `TASK1_ACCEPTED` additionally requires the maintenance generation cutover and
  the §9.1 static/AST production-path contract. Until the formal runtime reads
  active database configuration without the legacy pair/window/timeframe,
  provider/optimizer, count, threshold, capacity, or TTL fallbacks, status is
  `NOT_ACCEPTED_STATIC_RUNTIME_CUTOVER_PENDING`.

Legacy runtime/API/frontend paths may not be broad-allowlisted to manufacture a
pass. Every temporary static-contract exception must identify an exact file and
symbol/node fingerprint, rule ID, reason, owner, expiry, and removal task.
Demo-only, `allow_real_funds=false`, unique-writer fencing, state-machine keys,
digests, FK/idempotency rules, and adapter hard capability limits are safety or
protocol invariants rather than configurable business defaults.
