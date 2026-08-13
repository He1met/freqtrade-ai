# Retire the legacy local runtime behind a durable supervisor fence

This runbook retires the legacy `freqtrade_ai` runtime. The database remains a
read-only historical source; it is never migrated or resumed. The physically
isolated `freqtrade_ai_design_lab` database is the only V1.3 runtime target.

The fence and commands below never load `runtime.env`, read credentials,
contact OKX, access either database, or create orders/signals. The only process
mutation is the explicitly generation-bound retirement of the exact legacy
LaunchAgent owner and its marker+cwd verified children.

## Stable paths and owner

- Control: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.json`
- Monotonic generation ledger: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.generation.json`
- Observation receipt: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.observed.json`
- Immutable legacy child snapshot: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.legacy-children.json`
- Permanent retirement receipt: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.legacy-retired.json`
- Serialization lock: `/Users/shenjianpeng/Developer/Freqtrade Ai/.freqtrade-ai/runtime/supervisor-control.lock`
- Legacy LaunchAgent label: `com.he1met.freqtrade-ai.runtime`
- Plist: `/Users/shenjianpeng/Library/LaunchAgents/com.he1met.freqtrade-ai.runtime.plist`
- Working directory: `/Users/shenjianpeng/Developer/Freqtrade Ai`

Backend, worker, frontend, and the legacy OKX reconciliation runtime are not
separate LaunchAgents. Their PIDs are transient during recovery and must never
be copied from an earlier operator report. During `reload-owner`, after the
exact legacy supervisor is paused and drained but before `kickstart`, the
command captures live legacy children into an immutable, generation-bound
snapshot. `supervisor-maintenance-stop-legacy` signals only those snapshotted
PID/PGID/start-token/command-digest/cwd identities and blocks before its first
signal if any extra or changed candidate appears. It does not target
PostgreSQL, Redis, Hermes, Codex, PyCharm, Task1/V1.3 processes, or any other
LaunchAgent.

## Stable CLI and function contract

Use the canonical checkout after deploying the reviewed commit. The maintenance
commands skip `load_runtime_environment()`.

1. Create a strictly increasing generation:

```sh
'/Users/shenjianpeng/Developer/Freqtrade Ai/backend/.venv/bin/python' \
  '/Users/shenjianpeng/Developer/Freqtrade Ai/scripts/local_runtime.py' \
  supervisor-maintenance-suspend \
  --request-id task1-retire-legacy-001 \
  --operator-identity task1 \
  --reason retire-legacy-runtime \
  --target-schema-version 20260813_47 \
  --json
```

Keep `control.cutover_generation` from this response. It is a zero-padded,
20-digit monotonic value issued ledger-first. An interrupted ledger-first write
may be repaired only by repeating the exact request metadata; it cannot reuse
the generation for a different request.

2. Reload only the LaunchAgent supervisor process so it loads the deployed
fence code. For the first committed generation only, a genuinely absent
observation is the one-time pre-fence bootstrap case. Any malformed/partial
receipt, or a missing receipt after generation 1, remains fail-closed. If the
currently loaded supervisor already supports this fence, its existing receipt
must instead be fresh and exactly match the control tuple and process identity.
This command does not stop or start child services:

```sh
'/Users/shenjianpeng/Developer/Freqtrade Ai/backend/.venv/bin/python' \
  '/Users/shenjianpeng/Developer/Freqtrade Ai/scripts/local_runtime.py' \
  supervisor-maintenance-reload-owner \
  --cutover-generation '<generation>' \
  --request-id task1-retire-legacy-001 \
  --json
```

The command uses only `launchctl list`, label-bound `launchctl kill SIGSTOP`,
exact process command, process start time, and cwd. It does not call
`launchctl print` because that output can expose job environment. It invokes
`launchctl kickstart -k` only after the exact suspended generation/request has
been validated, the old owner has been paused and drained, the immutable
legacy child snapshot has been written, and the old owner identity has been
re-probed without change. Once the exact pre-fence owner has been paused, any
drain, snapshot, identity, or kickstart failure leaves it paused; the command
never resumes an owner which cannot observe the durable fence.

3. Wait for the durable drain receipt:

```sh
'/Users/shenjianpeng/Developer/Freqtrade Ai/backend/.venv/bin/python' \
  '/Users/shenjianpeng/Developer/Freqtrade Ai/scripts/local_runtime.py' \
  supervisor-maintenance-status --json
```

Proceed only when the replacement supervisor has written a new receipt and:

- `mode=MIGRATION_SUSPENDED`;
- `observed_matches_control=true`;
- receipt generation/request/mode match the suspend response;
- receipt PID/start token/instance/release identify the current canonical
  supervisor;
- `supervisor_launchd_label=com.he1met.freqtrade-ai.runtime`.

The control file landing is not an immediate acknowledgement. A matching
receipt proves that any synchronous supervisor iteration which was already in
flight has drained; the receipt is written at the next iteration gate and that
iteration performs no capability/thaw/verify/down/up action.

The replacement supervisor may still report the pre-target v46 capability and
is intentionally not eligible to resume target v47. This retirement flow never
resumes it. Do not replace the canonical checkout between this observation and
the legacy stop: the stop CAS binds the complete replacement observation and
the immutable child snapshot.

4. Stop only the legacy child services, under the same generation/request and
while holding both the fence lock and existing runtime lock:

```sh
'/Users/shenjianpeng/Developer/Freqtrade Ai/backend/.venv/bin/python' \
  '/Users/shenjianpeng/Developer/Freqtrade Ai/scripts/local_runtime.py' \
  supervisor-maintenance-stop-legacy \
  --cutover-generation '<generation>' \
  --request-id task1-retire-legacy-001 \
  --json
```

Success is `status=LEGACY_RUNTIME_STOPPED`, every snapshotted legacy process
group terminal, no extra managed child, no legacy PID/group evidence, no
listener on legacy service ports `8000` or `5173`, no writer lock holder, and
`automatic_recovery=FENCED`. The full proof is repeated twice before commit.
Any inaccessible process, changed identity, non-terminal child, stale receipt,
or stale generation is `BLOCKED`; the command never broadens the signal scope.
The same locked transaction atomically writes
`supervisor-control.legacy-retired.json`. This immutable receipt binds the
generation/request to SHA-256 digests of the complete replacement observation
and legacy child snapshot, plus terminal services, absent managed orphans,
unbound legacy ports, and no writer-lock holder. It permanently disables
resume, reload, new legacy generations, `up`, and thaw.

5. Re-run `supervisor-maintenance-status` and require
`status=LEGACY_RETIRED`, `legacy_retired=true`,
`resume_eligible=false`, and a matching `legacy_retirement_receipt`. Only after
that durable proof, permanently disable and unload the exact legacy owner:
Re-check the exact label before execution:

```sh
/bin/launchctl list | /usr/bin/awk '$3 == "com.he1met.freqtrade-ai.runtime" {print}'
/bin/launchctl disable 'gui/501/com.he1met.freqtrade-ai.runtime'
/bin/launchctl bootout 'gui/501/com.he1met.freqtrade-ai.runtime'
```

Do not delete or modify another plist/label. Preserve all five JSON records as
audit evidence. Do not call `supervisor-maintenance-resume` for this generation.
New V1.3 must use a distinct owner/generation and stay fail-closed until its
independent database and safety gates are verified.

Stable Python functions in `scripts/local_runtime.py` are:

- `suspend_supervisor_for_migration(...)`
- `reload_supervisor_for_migration(...)`
- `read_supervisor_maintenance_status(...)`
- `stop_legacy_runtime_for_migration(...)`
- `resume_supervisor_after_migration(...)` (not used for permanent retirement)

## Exact on-disk schemas

All five JSON records and the serialization lock are owner-owned regular files
with mode `0600`. Every repository
path component is opened relative to a trusted root with `O_NOFOLLOW`; the
parent must not be group/world writable. JSON writes use a same-directory 0600
temporary file, `fsync(file)`, atomic `replace`, then `fsync(directory)`.

Control (`supervisor-control-v1`):

```json
{
  "schema_version": "supervisor-control-v1",
  "mode": "MIGRATION_SUSPENDED",
  "cutover_generation": "00000000000000000001",
  "request_id": "task1-retire-legacy-001",
  "operator_identity": "task1",
  "reason": "retire-legacy-runtime",
  "target_schema_version": "20260813_47",
  "requested_at": "<UTC timestamp>",
  "updated_at": "<UTC timestamp>"
}
```

Generation ledger (`supervisor-control-generation-v1`) stores the same request
tuple plus `last_issued_generation` and `commit_state=PENDING_CONTROL|COMMITTED`.
The non-reuse guarantee holds for the durable ledger lifetime; deleting all
control history destroys the only local high-water anchor and is forbidden.

Observation (`supervisor-control-observation-v2`) contains the control tuple,
observed timestamp, PID, PID start token, per-process instance UUID, command
digest, cwd, exact LaunchAgent label, loaded release digest and loaded runtime
schema capability. It contains no raw command, environment, credential or URL.

Legacy child snapshot (`supervisor-control-legacy-children-v1`) contains the
generation/request, exact paused legacy supervisor OS identity, capture code
release and expected replacement schema capability, capture timestamp, and a
canonical list of legacy service/PID/PGID/start-token/command-digest/cwd
identities. It is written while the old owner is paused, before replacement,
and is immutable. The release/capability fields identify the deployed capture
code and expected replacement; they are not an attestation of code loaded by
the paused legacy process.

Retirement (`supervisor-control-legacy-retired-v2`) contains generation,
request, replacement supervisor PID/start/release, SHA-256 digests of the
complete validated replacement observation and legacy child snapshot, four
`true` terminal proofs (services terminal, managed orphans absent, service
ports unbound, writer lock unheld), and the retirement timestamp. Validation
also requires `snapshot.captured_at <= observation.observed_at <= retired_at`
and different old/replacement supervisor start tokens. It is written once
after full cleanup and is never overwritten by status or authorization checks.

Only control+ledger+observation+child snapshot+retirement all absent is the
legacy virgin default `ACTIVE`.
Any partial history, malformed/unknown schema, bad permission, symlink, target
other than `20260813_47`, ledger mismatch, stale receipt, or identity mismatch
fails closed before credential capability or runtime actions. The only missing
receipt exception is the explicit generation-1 bootstrap reload described
above; a missing receipt always blocks stop, resume, and normal automation.

## V1.3 database dependency

This legacy retirement PR deliberately does not change a database selector or
connect to either database. `local_runtime.py` remains legacy-only and must not
be used as the V1.3 service owner. Task1 must bind its distinct V1.3 startup
path fail-closed to local database name `freqtrade_ai_design_lab`, with a new
owner/generation and independent schema/readiness gates. The old
`freqtrade_ai` target remains read-only history after retirement.
