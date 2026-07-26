# Execution target lineage

Issue #442 keeps one PostgreSQL migration chain while preventing research,
local simulation, historical rows, and OKX Demo exchange records from being
treated as interchangeable evidence.

## Scope catalog

`execution_scopes` contains exactly three immutable identities:

- `OKX_DEMO`: the only exchange-write target.
- `LOCAL_DRY_RUN`: executable local research, generation, and backtest work
  that cannot write exchange orders.
- `UNKNOWN_LEGACY`: historical provenance that cannot be executed or accepted
  as executable evidence.

There is no implicit target inference. The migration adds a non-null foreign
key to strategy generation runs, backtest runs, and research jobs. Existing
rows are backfilled only to `UNKNOWN_LEGACY`; they are never relabelled
`OKX_DEMO` or `LOCAL_DRY_RUN`.

## Persistence boundary

The database contract includes target-bound TradeIntent, risk decision, order,
fill, position, reconciliation, and manifest records. This is persistence
only: it does not connect to OKX or submit orders.

Exchange tables require `execution_target_id=OKX_DEMO`. Repositories are bound
to one target and verify parent records before writing children. Both
`trade_intents` and `exchange_orders` enforce
`(execution_target_id, client_order_id)` uniqueness. The identifier is also
validated against ADR-0010: 1-32 case-sensitive alphanumeric characters.

Queries for runs, jobs, orders, and manifests require or carry an explicit
scope. `UNKNOWN_LEGACY` repositories are read-only; workers cannot claim those
records or mutate their state. Research-job completion validates every linked
generation run, backtest run, task, result, strategy version, and score before
writing any link or terminal status. Missing, cross-scope, or inconsistent
chains are rolled back as `BLOCKED`.

## Migration and acceptance

The single schema version advances from `20260723_01` to `20260727_01` in one
PostgreSQL transaction:

1. create and seed the scope catalog;
2. add nullable lineage columns to existing roots;
3. backfill nulls to `UNKNOWN_LEGACY`;
4. make the columns non-null and add foreign keys and indexes;
5. replace research-job idempotency with a scope-aware unique constraint;
6. create the new target-bound persistence tables;
7. verify columns, nullability, foreign keys, indexes, checks, and unique
   constraints before recording the new version.

SQLite tests validate repository and constraint behavior. PostgreSQL acceptance
must run against an isolated temporary database on the same PostgreSQL
instance; it must never rebuild the canonical project database.
