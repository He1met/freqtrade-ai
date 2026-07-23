# Phase 9 Operational Readiness Plan

## Stage Purpose

Phase 9 exists to prove that the project can run as a real local product, not
only as a set of isolated features, fixtures, or mock screens.

The target is "真实运行打通": database-backed pages, a safely configured real
LLM provider, durable strategy records, generated strategy files, local
backtest records, persisted scores, and QA evidence that reconciles the browser,
API responses, and database rows.

This phase is not live trading, production deployment, profit validation, or a
complex quantitative platform expansion.

## Relationship To Earlier Phases

Phase 8 accepted the Local Strategy Lab foundation:

- source tracing with `database`, `api_aggregate`, `fixture`, `fallback`, and
  `unknown`;
- local test database reset/seed/dirty-data support;
- API/database surfaces for strategy generation, strategy versions, backtest
  tasks, results, and ranking;
- browser/API/database reconciliation smoke coverage;
- local-only dry-run readiness and controlled dry-run boundaries.

Phase 9 builds on that foundation by validating operational readiness:

- real provider integration, with DeepSeek as the first provider candidate;
- one controlled real LLM generation path when needed;
- strategy generation flowing into durable database records and files;
- local backtest preflight, task/result persistence, scoring, and UI display;
- structured Bug issue intake for any real-run mismatch;
- a design for hourly local controlled generation/backtesting, without
  production queue infrastructure.

Phase 9 must not be treated as Phase 10. Production deployment, live trading,
real orders, unattended live bot operation, and production-grade queues remain
out of scope.

## Already-Built Surfaces That Need Real-Run Validation

The following capabilities exist from prior phases but still need Phase 9
evidence against real operational conditions:

| Surface | Existing state | Phase 9 validation question |
| --- | --- | --- |
| Strategy generation service | Fake/mock provider paths and database models exist. | Can a real provider result be parsed, validated, and persisted without leaking secrets? |
| Strategy/version repositories | Durable records and source metadata exist. | Do generated records survive refresh and reconcile across API and DB? |
| Strategy file manager | Safe local file handling exists. | Does a real generated strategy produce a safe, present, backtest-recognizable file? |
| Local backtest trigger | Fail-closed local preflight exists. | Does a generated strategy enter a local backtest only when prerequisites are present? |
| Backtest artifact ingest | Artifact parsing and result persistence exist. | Do real local artifacts write `backtest_task`, `backtest_result`, and traceable refs? |
| Strategy scoring/ranking | Database-backed score and ranking APIs exist. | Are scores tied to real persisted results, not fixture-only data? |
| Frontend core pages | API/fallback flows exist. | Which pages show real DB-backed data, and which still show fallback/fixture/mock? |
| Phase 8 E2E smoke | Local fixture and DB reconciliation exists. | Can QA reconcile one real provider run end-to-end? |

For Phase 9 local QA fixture setup, use
[CI-only Phase 8 integration coverage](../scripts/smoke_phase8.py). It covers reset, seed,
dirty-data, failed, `BLOCKED`, missing artifact, partial completion, and
unknown-source scenarios while keeping every local-test row non-acceptable as
real Provider evidence.

## Priority Pages For Database-Backed Display

Phase 9 should prioritize user-visible pages in this order:

1. `GenerationRuns` and strategy generation surfaces: show provider, model,
   run status, failed reasons, strategy IDs, and version IDs.
2. `Strategies` and strategy detail: show database-backed strategy, version,
   file path status, validation state, and artifact refs.
3. `BacktestTasks` and `BacktestRuns`: show task status, `BLOCKED`/`FAILED`
   reasons, local prerequisite checks, run IDs, and result refs.
4. `Ranking`: show scores tied to `strategy_version_id` and
   `backtest_result_id`.
5. `Dashboard` / operator summaries: aggregate real status only when the
   underlying database/API rows are traceable.

Any page that cannot prove its database/API source must display the exact source
type and a user-facing explanation.

## Data Source Rules

| Source | Core success? | Required behavior |
| --- | --- | --- |
| `database` | Yes | Include database IDs and freshness metadata. |
| `api_aggregate` | Yes, only when traceable | Include source rows, database IDs, and artifact refs. |
| `fixture` | No | Mark as test/local fixture and block core acceptance. |
| `fallback` | No | Explain the missing API, DB row, config, or local dependency. |
| `mock` | No | Keep visibly separate from real provider/database data. |
| `unknown` | No | Show warning or `BLOCKED`; never pass as success. |

Page success without durable database records is a Bug. API success without
database rows is a Bug. Database rows that are not shown on the page are a Bug.

## DeepSeek Validation Policy

DeepSeek is allowed as the first real provider candidate, but the default policy
is to avoid real calls unless a specific validation issue requires one.

The single-run entry point for `#277` is documented in
[phase9_deepseek_single_e2e.md](phase9_deepseek_single_e2e.md). It defaults to
fail-closed evidence and only sends a real request when explicitly authorized.

Minimum-call rules:

- Use `DEEPSEEK_API_KEY` or an equivalent local secure environment variable.
- Never write the key to code, config, database, logs, reports, issues, pull
  requests, screenshots, or page payloads.
- Prefer preflight and fake/provider-contract tests before any real call.
- When a real call is required, perform one narrow validation call and record
  only redacted provider metadata.
- A failed provider response must create a failed run or explicit failure record.
- A fake/mock/fixture provider result must never be labeled as DeepSeek.
- A successful DeepSeek call does not by itself complete Phase 9.

## Real Strategy-To-Backtest Chain

The Phase 9 main chain is:

1. Real LLM provider returns candidate strategy content.
2. `StrategyBlueprint` validation passes or records a failed run.
3. `generation_run` is persisted.
4. `strategy` is persisted.
5. `strategy_version` is persisted.
6. Strategy file is written to an approved local path.
7. Strategy file presence, path safety, and format are validated.
8. Local backtest preflight checks Freqtrade binary, local market data, config,
   strategy file, permissions, and timerange.
9. `backtest_task` / `backtest_run` is persisted or marked `BLOCKED`.
10. Backtest artifact is parsed.
11. `backtest_result` is persisted.
12. `strategy_score` is persisted.
13. Pages show generation, strategy, file, task, result, score, status, and
    failure reasons from API/database data.
14. QA reconciles browser, API, and direct DB state.

Every link must be able to explain `SUCCESS`, `FAILED`, and `BLOCKED`.

## No-Real-Data Explanation Standard

When a page or API cannot show real database-backed data, it must explain:

- what source is currently displayed;
- why the source is not real database data;
- which API, database record, local dependency, or config is missing;
- what command, setup step, or prior workflow the user needs to run;
- whether the current state is accepted as core Phase 9 evidence.

Examples of valid blockers:

- missing `DEEPSEEK_API_KEY`;
- DeepSeek provider not configured;
- provider call failed;
- provider response failed schema validation;
- strategy file was not written;
- local market data is missing;
- Freqtrade binary is unavailable;
- local backtest task was not created;
- backtest result was not ingested;
- strategy score was not generated;
- database is empty;
- backend API is not implemented;
- frontend page still uses fallback only.

## Bug Issue Triggers

Create a structured Bug issue when any of these occur:

- page shows success but the database has no matching row;
- database has a row but the page does not display it;
- API returns data with the wrong `data_source`;
- fixture, fallback, mock, or unknown data appears as real success;
- DeepSeek fails without a failed run;
- provider parsing fails without an error record;
- strategy file is missing after successful generation;
- strategy file path is unsafe or not runnable;
- backtest task is not created after valid preflight;
- backtest result is not written;
- score/ranking does not tie to a persisted backtest result;
- page refresh loses data that should be database-backed;
- any key, secret, token, or passphrase appears in logs, pages, reports, issues,
  pull requests, screenshots, or database payloads;
- dry-run/live boundaries are weakened;
- a test report cannot explain data source and evidence.

Each Bug issue must cover one defect only and include reproduction steps,
current behavior, expected behavior, page/API/DB impact, data source, security
impact, evidence, and acceptance criteria.

## Feature, Test, Config, Docs, And Security Split

Use the following split for Phase 9 issue triage:

| Category | Use when |
| --- | --- |
| Feature | A missing product capability is required for real DB/provider/backtest display. |
| Test Gap | QA cannot reconcile page/API/DB, construct data, or verify failure paths. |
| Config Gap | Local key, binary, database, market data, path, or permission prerequisites are unclear. |
| Docs Gap | The user cannot tell what to run, what is real, or what is blocked. |
| Security Risk | Secrets, live trading, real orders, deployment, or dry-run/live boundary risks appear. |
| Deferred | The request belongs to productionization, Phase 10, live trading, or complex scheduling. |

## Recommended Execution Order

| Order | Issue | Purpose | Initial status |
| --- | --- | --- | --- |
| 1 | `#266` | Stage plan, acceptance language, execution order. | Ready |
| 2 | `#267` | Page data-source audit and DB display priority. | Backlog |
| 3 | `#268` | Local readiness/config/dependency preflight matrix. | Backlog |
| 4 | `#280` | Security review for keys, provider, and live boundary. | Blocked until preflight language exists |
| 5 | `#269` | DeepSeek provider ENV-only success/failure path. | Backlog |
| 6 | `#270` | Real provider result into generation/strategy/version DB records. | Backlog |
| 7 | `#271` | Strategy file write and runnable validation. | Backlog |
| 8 | `#272` | Local backtest preflight and task creation. | Backlog |
| 9 | `#273` | Backtest result and score persistence. | Backlog |
| 10 | `#274` | Frontend display of real backtest/score/ranking data. | Backlog |
| 11 | `#275` | Source markers and no-real-data explanations. | Backlog |
| 12 | `#276` | Local test DB data and failure-state QA support. | Backlog |
| 13 | `#277` | Single controlled DeepSeek real-run E2E. | Backlog |
| 14 | `#330` | Hourly local controlled run design. | Backlog |
| 15 | `#279` | Structured Bug issue flow. | Backlog |
| 16 | `#281` | Deferred Phase 10 / productionization boundary. | Backlog |
| 17 | `#282` | Phase 9 review and Project closeout. | Blocked |

The first executable issue is `#266`. After it is complete, `#267`, `#268`,
`#279`, and `#281` can run in parallel because they are documentation/governance
surfaces with separate files. Provider and backtest implementation should wait
for at least the readiness and security boundaries.

## Hourly Local Run Design Boundary

Phase 9 may design an hourly local controlled run, but should not implement a
production scheduler by default.

The detailed design is
[phase9_hourly_local_controlled_run_design.md](phase9_hourly_local_controlled_run_design.md).
It defines local-only state, pause/disable controls, failure semantics,
single-flight single-use lease expectations, pre-implementation acceptance
gates, and the Phase 10 queue boundary.

Required design properties:

- local-only operation;
- at most one generated strategy per hour;
- explicit pause/disable control;
- fail-closed preflight before lease claim or Provider call;
- one single-use lease per attempt with no concurrent manual/scheduled overlap;
- durable run records;
- failed run records;
- strategy file and backtest task linkage;
- page/API/DB verification;
- no live trading;
- no real orders;
- no production deployment;
- no Redis/Celery/Kafka/RabbitMQ unless separately approved.

## Phase 9 Completion Definition

Final acceptance is recorded in [phase9_acceptance.md](phase9_acceptance.md).

Phase 9 is complete only when the review issue can prove:

- every core page either displays database-backed data or explains why it cannot;
- DeepSeek or the selected real provider is validated through the minimal
  allowed real-call path, or remains explicitly `BLOCKED` with safe reasons;
- generated strategies can be persisted and traced to files;
- local backtest preflight, task, result, and score records reconcile across
  browser, API, and database;
- fallback, fixture, mock, and unknown data are visibly non-core;
- Bug issue intake exists for every real-run mismatch class;
- no secret values leak;
- no live trading, real order, production deployment, Freqtrade source edit, or
  unapproved queue infrastructure was introduced.
