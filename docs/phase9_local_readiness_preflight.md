# Phase 9 Local Readiness Preflight Matrix

Phase 9 real-run validation must fail closed. A missing key, database target,
binary, local market data file, generated strategy file, config, permission, or
redaction rule must produce `BLOCKED` or `FAILED` with an actionable reason,
not a fake success.

This document defines the local readiness matrix for Operational Readiness. It
does not call DeepSeek, does not start Freqtrade, does not download market data,
and does not write to production or unknown databases.

## Status Semantics

| Status | Meaning | Acceptance use |
| --- | --- | --- |
| `READY` | The prerequisite is present and safe for the next local-only step. | Can unlock the dependent issue. |
| `BLOCKED` | The prerequisite is missing or intentionally disabled. | Must show the blocker and resolution path. |
| `FAILED` | A present prerequisite is malformed, unsafe, inaccessible, or inconsistent. | Must create or link a Bug if reproducible. |
| `UNKNOWN` | The system cannot determine state without unsafe access. | Not accepted; explain how to make it checkable. |

## Preflight Matrix

| Prerequisite | READY evidence | BLOCKED condition | FAILED condition | Resolution path |
| --- | --- | --- | --- | --- |
| DeepSeek API key | `DEEPSEEK_API_KEY` env var exists; only presence/length class is checked. | Env var missing or provider disabled. | Value appears in logs, DB, report, page, Issue, PR, or config file. | Set env var locally; rotate if exposed; never persist the value. |
| LLM provider config | Provider name, base URL, model name, timeout, and retry limits are configured without secrets. | Provider not selected or model name missing. | Config includes secret value or labels fake provider as real. | Update local config to reference ENV names only. |
| Local database target | Local/dev/test DB URL is present and classified as safe. | DB URL missing. | URL points to production/shared/remote/unknown target for destructive checks. | Use local SQLite/PostgreSQL test target; refuse unsafe targets. |
| Database migrations | Required tables are present or migration command is documented. | Database is empty or migrations not run. | Schema mismatch prevents writes or source tracing. | Run migrations against local target only. |
| Strategy generation tables | `generation_run`, `strategy`, `strategy_version` can be written and read. | Tables missing or repository unavailable. | API success without durable rows. | Fix local DB/migration before provider validation. |
| Strategy directory | Approved generated strategy directory exists and is writable. | Directory missing or not writable. | Path escapes approved root or points into repo source unexpectedly. | Create approved local directory and enforce allowlist. |
| Strategy file | Generated file exists, has expected suffix/name, and links to `strategy_version`. | File not generated yet. | File missing after success, unsafe path, unreadable file, or invalid format. | Re-run generation after fixing file manager/path config. |
| Freqtrade binary | `freqtrade` or configured binary path resolves locally. | Binary missing. | Binary path is unsafe, not executable, or unexpected version output. | Install/configure local Freqtrade binary; do not modify upstream source. |
| Local market data | Required pair/timeframe/timerange files exist under allowed local data root. | Data missing. | Data file unreadable, wrong pair/timeframe, or outside allowed root. | Provide local data manually; Phase 9 does not auto-download. |
| Backtest config | Local backtest profile has safe timerange, pair, stake, and dry/local-only boundaries. | Profile missing. | Config includes live trading, exchange credentials, or production paths. | Use approved local backtest profile. |
| Artifact directory | Reports/artifacts directory exists and is writable outside tracked source paths when needed. | Directory missing or unwritable. | Artifact refs point to unsafe paths or include secrets. | Create local artifact directory; redact outputs. |
| Log redaction | Secret-shaped values are redacted in stdout, stderr, reports, and API payloads. | Redaction check not implemented for the path. | Any secret value is visible. | Block the run, rotate exposed key, add redaction coverage. |
| API availability | Backend API can expose readiness/result state without starting live workflows. | Backend unavailable. | API reports success with missing DB/file evidence. | Start local backend or fix readiness endpoint. |
| UI explanation | Page can show source, blocker, and next action for missing real data. | Page lacks explanation path. | Page presents fallback/fixture/mock as real success. | Fix UI source marker and no-data explanation. |
| QA database access | QA can query local DB safely. | No local DB query method documented. | QA query targets production/shared/unknown DB. | Document local connection and guard destructive commands. |

## DeepSeek-Specific Rules

- Phase 9 should avoid real DeepSeek calls until `#269` or `#277` explicitly
  requires one.
- A preflight check may test only env var presence and provider configuration.
- A real call, when approved, must be a single narrow validation call by
  default.
- The API key value must never be copied into logs, pages, database rows,
  reports, issues, pull requests, screenshots, or artifacts.
- If a real provider call fails, the failure must be durable and visible as a
  failed run, not silently replaced by a fake result.

## QA Negative Tests

QA should verify these blocked paths before accepting provider or backtest work:

| Scenario | Expected result |
| --- | --- |
| Remove `DEEPSEEK_API_KEY`. | Provider readiness is `BLOCKED`; no real call attempted. |
| Configure fake provider while selecting DeepSeek. | `FAILED` or `BLOCKED`; fake result cannot appear as DeepSeek. |
| Use empty local database. | Pages/API explain missing database records. |
| Remove generated strategy file. | Strategy file readiness is `BLOCKED` or `FAILED`; no backtest success. |
| Remove local market data. | Backtest readiness is `BLOCKED`; no download attempted. |
| Remove Freqtrade binary. | Backtest readiness is `BLOCKED`; no process start. |
| Inject secret-shaped fixture output. | Reports/logs redact it or fail closed. |
| Point DB URL at unknown remote target. | Destructive local-test actions are refused. |

## Bug Triggers

Create a Bug issue when:

- any missing prerequisite still allows a success state;
- `BLOCKED` does not explain the missing condition and resolution path;
- provider readiness reads or logs a real key value;
- local backtest starts without strategy file, market data, or binary readiness;
- fallback/fixture/mock output is reported as real Provider or database success;
- QA cannot reproduce readiness state from page, API, and database evidence.

## Downstream Issue Gates

| Downstream issue | Gate from this matrix |
| --- | --- |
| `#269` DeepSeek Provider | Provider config and key-presence preflight defined; key value never persisted. |
| `#270` Strategy DB chain | Local database and generation tables are safe and writable. |
| `#271` Strategy file | Approved strategy directory and file validation rules are defined. |
| `#272` Backtest preflight | Freqtrade binary, market data, profile, and artifact checks are defined. |
| `#277` Single real LLM E2E | All relevant `READY` states are present, or the run records explicit blockers. |
| `#280` Security review | Key, redaction, live boundary, and unsafe target checks are reviewable. |
