# V1.3 owner research activation

This runbook applies only to the physically isolated owner database
`freqtrade_ai_design_lab`.  It does not write the retired `freqtrade_ai`
database and does not start a worker, backtest, signal, deployment, exchange
client, or order path.

## Required business input

The operator must provide `candidates_per_target`.  The activation reads the
exact enabled target set from the existing validated `production-research`
assembly and derives:

```text
target_count = enabled target rows
candidate_count = candidates_per_target * target_count
```

There is no default.  In particular, legacy `10 x 6 = 60` and the test-only
`7 x 4 = 28` are not production inputs.

## Reviewed sequence

1. Confirm the legacy supervisor receipt is still `LEGACY_RETIRED`, its
   `MIGRATION_SUSPENDED` fence has not been resumed, and ports 8000/5173 and
   the canonical writer lock remain unused.
2. Run the read-only owner schema, migration, row-count, quality-state, ACL,
   active-session, prepared-transaction, and secret-table-count preflight.
3. Apply
   `backend/scripts/strategy_platform_v13_owner_activation_acl_repair.sql` as
   the owner.  It may only revoke the drifted runtime `SELECT` on
   `freqtrade_ai_schema_migrations`; every other ACL must already match the
   accepted 54-object read allowlist.
4. Re-run the owner verifier and ACL evidence.  Any problem is `NO_OP`.
5. Execute a rollback-only activation dry-run:

   ```bash
   cd backend
   python scripts/activate_strategy_platform_v13_owner_research.py \
     --candidates-per-target <explicit-positive-integer>
   ```

   Record the returned `input_digest`, `plan_digest`, counts, version ids,
   bundle id and bundle digest.  The input digest excludes transaction-local
   generated ids and is the apply identity; dry-run ids are evidence only and
   are not durable.
6. Compare the dry-run with the reviewed Issue/PR evidence.  Apply only the
   same plan identity:

   ```bash
   cd backend
   python scripts/activate_strategy_platform_v13_owner_research.py \
     --candidates-per-target <same-value> \
     --apply \
     --expected-input-digest <dry-run-input-digest>
   ```

7. Repeat the identical apply command.  It must return the same version ids,
   bundle id, `input_digest` and `plan_digest` with `repeat_noop=true` and no
   row growth.  A rollback-only dry-run may consume PostgreSQL sequence values,
   so its generated ids and `plan_digest` are evidence rather than the apply
   identity; its semantic `input_digest` must remain exact.
8. As runtime role, use only
   `RuntimeConfigurationBundleReader.read_validated(bundle_id)` to verify the
   immutable DTO.  Do not call the owner resolver and do not grant runtime
   write/sequence/schema privileges.
9. Reconcile schema, constraints, owners, ACL, configuration/audit counts,
   activation scope, bundle digest, adapter manifest digest, legacy
   `BLOCKED/QUALIFIED=0`, and zero historical score/window mutation.
10. Create a new mode-0600 owner backup, restore it into an isolated temporary
    database, run the dedicated owner verifier and repeat all digest/count
    checks.  The pre-activation backup is not post-activation evidence.

## Mandatory rollback / NO_OP conditions

- any other client writer, prepared transaction, write lock, or unfinished
  Task 1 migration exists;
- database/schema/owner/role/ACL identity differs from the reviewed contract;
- either secret-table count is non-zero (values must never be selected);
- an adapter source/schema/capability digest differs from the installed
  manifest;
- any v1 schema cannot be preserved exactly in the versioned registry;
- a required metric, family, target, dependency, scope, profile or bundle is
  missing or ambiguous;
- the dry-run and apply semantic `input_digest` values differ, or the first
  apply and identical repeat `plan_digest` values differ;
- any operation asks for credentials, OKX access, worker/backtest execution,
  signal/order creation, historical revalidation, or real funds.
