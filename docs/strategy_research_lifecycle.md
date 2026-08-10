# Strategy research lifecycle

The hourly candidate flow is deliberately separate from the canonical
`strategies` catalog and from `StrategyDeployment`:

1. Generate exactly ten differentiated candidates in the single owned research
   worktree: five declare `5m` and five declare `15m`.
2. Evaluate every candidate on its declared timeframe for each of
   `BTC/USDT:USDT`, `ETH/USDT:USDT`, and `SOL/USDT:USDT`. Each target runs
   load/static checks, Freqtrade lookahead analysis, fee and slippage stress,
   and the primary, independent OOS, bull, range, and bear windows. The six
   market-data artifacts are quality-checked and digest-bound before the worker
   starts.
3. Write the JSON report and persist one `strategy_research_batches` row plus
   ten `strategy_research_candidates` rows. Each row preserves all three target
   decisions, including structured rejection reasons, and one deterministic
   deployment target. After the database transaction,
   atomically add a `persistence_receipt` to the report and synchronize its
   digest so the file cannot keep claiming `database_used=false`.
4. Keep rejected candidates with structured reasons. A validated batch with
   zero qualified candidates is successful research, not a generation failure.
5. Mark a candidate `QUALIFIED` only when at least one pair on its declared
   timeframe passes every hard gate. Let canonical bridge/deployment review read
   only `status=QUALIFIED`; rejected or validation-failed candidates cannot enter
   that continuation.
6. The continuation binds `BTC`, `ETH`, and `SOL` pairs to their exact OKX swap
   instrument IDs. It still preserves the existing active-slot limit, unique
   writer, total exposure, order-frequency, circuit-breaker, idempotency, and
   reconciliation gates. ETH/SOL execution remains fail-closed until the unique
   canonical owner applies and verifies the corresponding risk-policy/DB
   allowlist migration; this research branch does not run that migration.

The supported complete command is:

```bash
python scripts/run_strategy_candidate_research.py \
  --freqtrade /absolute/path/to/freqtrade \
  --datadir /absolute/path/to/okx/data \
  --run-id YYYYMMDDHH \
  --output reports/research/strategy-candidates-YYYYMMDDHH.json \
  --persist-database \
  --repository-commit <exact-candidate-commit>
```

If generation or validation fails after ownership has been established, the
runner automatically writes a failure report. Once candidate discovery has
completed, it also persists all ten candidates as `VALIDATION_FAILED` with the
partial evidence collected so far. A pre-generation failure remains a zero
candidate batch and is never described as ten rejected candidates.

If database persistence was unavailable during the run, ingest the durable
failure report later without re-running backtests:

```bash
python scripts/persist_strategy_candidate_research.py \
  --failure-report reports/research/strategy-candidates-YYYYMMDDHH.json \
  --run-id YYYYMMDDHH \
  --repository-commit <exact-candidate-commit>
```

Read-only handoff endpoints:

- `GET /api/strategy-research-batches`
- `GET /api/strategy-research-candidates?status=QUALIFIED`

`FAILED` with `generated_count=0` means research did not generate candidates.
`VALIDATED` with `generated_count=10`, `persisted_count=10`, and
`qualified_count=0` means all ten were evaluated and rejected by the existing
hard gates. Neither state changes active strategies, runtime, grants, or orders.
