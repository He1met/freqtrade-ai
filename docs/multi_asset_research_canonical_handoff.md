# BTC/ETH/SOL dual-timeframe canonical handoff

This branch is intentionally limited to isolated-worktree implementation and
offline tests. It does not inspect or mutate the canonical runtime/database and
does not start research, bridge, deployment, or order execution.

## What becomes available after merge

- Formal research requires six quality-checked, digest-bound OKX futures data
  files: BTC, ETH, and SOL at both `5m` and `15m`.
- The ten-source candidate contract is split evenly between declared `5m` and
  `15m` strategies. Each candidate is evaluated on all three pairs at its own
  timeframe.
- Every target keeps lookahead, fee/slippage, independent OOS, regime,
  trade-count, score, and 15% drawdown evidence. All target rejection reasons
  are persisted. A candidate is `QUALIFIED` only if at least one exact target
  passes every hard gate.
- The canonical bridge binds its equivalence proof and market-data receipt to
  the selected qualified pair/timeframe. Deployment continuation maps the
  selected pair to the exact BTC/ETH/SOL OKX swap instrument and retains the
  existing three-slot capacity gate.

## Steps reserved for the unique canonical owner

1. Re-check complete Codex task ownership before any canonical action.
2. Verify the six local data files and their coverage of every configured
   primary, walk-forward, and OOS window. Missing or partial data is a fail-closed
   `NOT_GENERATED` result.
3. Review and merge this PR, then update the canonical checkout to the exact
   merge SHA.
4. Design and review a separate reversible PostgreSQL/risk-policy migration for
   `ETH-USDT-SWAP` and `SOL-USDT-SWAP`. It must preserve `OKX_DEMO`,
   `allow_real_funds=false`, `real_orders=false`, the owner-mediated v39 write
   function, runtime-role ACL restrictions, writer fencing, total exposure,
   active-slot, frequency, circuit-breaker, idempotency, fresh attestation, and
   reconciliation checks. Do not reuse the controlled-canary permission path.
5. Run targeted PostgreSQL ACL/bypass/rollback suites before applying that
   migration to canonical. Until this succeeds, ETH/SOL signal execution must
   remain blocked even if a research candidate qualifies.
6. Only the canonical owner may record fresh ownership, start formal research,
   bridge `QUALIFIED` candidates, apply migrations, restart services, or observe
   natural Demo signals/orders. Never replay an unknown historical attempt or
   manufacture a signal/order.
