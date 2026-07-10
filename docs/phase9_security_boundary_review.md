# Phase 9 Security Boundary Review

This review decides whether Phase 9 may proceed from planning/preflight into the
real Provider and single-run E2E issues. It covers ENV-only keys, redaction,
fake/real labeling, dry-run/live boundaries, production deployment boundaries,
and GitHub Issue/PR exposure risk.

No DeepSeek call was made for this review. No API key value was written to code,
config, logs, reports, database rows, GitHub Issues, or pull requests.

## Review Verdict

Phase 9 real Provider work is conditionally approved to proceed to `#269` and
later `#277` only under the gates below:

- real Provider secrets must be read from local ENV at call time only;
- Provider config may store provider name, model, base URL, timeout, and ENV
  variable names, but never secret values;
- a missing key must create `BLOCKED` or a failed generation run, not a fake
  success;
- fake, mock, fixture, fallback, and unknown results must never be labeled as
  DeepSeek or database-backed success;
- runtime control must remain local-only, dry-run-only, and disabled by default;
- no live trading, real order, production deployment, or Freqtrade source edit
  is approved by this review;
- every failure path must redact secrets before writing reports, API payloads,
  logs, page text, Issue text, or PR text.

If any gate fails, the dependent issue must stay `BLOCKED` or create one
structured Bug issue.

## Evidence Commands

| Check | Result | Notes |
| --- | --- | --- |
| `python3 scripts/scan_secrets.py` | PASS | Scanned 195 tracked files; no secret-shaped values found. |
| `backend/.venv/bin/python -m pytest backend/tests/test_secret_scanning.py backend/tests/test_dry_run_status.py backend/tests/test_dry_run_readiness.py backend/tests/test_runtime_contract.py backend/tests/test_dry_run_control.py backend/tests/test_freq_ui_link.py backend/tests/test_strategy_static_review.py` | PASS | 38 security-boundary tests passed. |
| GitHub search for `sk-` in Issues and PRs | PASS | Returned no Issue or PR results. |
| GitHub search for `api_secret OR passphrase OR token` in Issues and PRs | PASS | Returned no Issue or PR results. |
| GitHub search for `DEEPSEEK_API_KEY` | PASS with expected refs | Returned only expected environment-variable references in Phase 9 planning/preflight work, not key values. |

## Repo Secret Scan Boundary

The repo-local scanner is active in CI and reports only path, line number, key
name, and rule id. It does not print matched secret values.

| Surface | Current state | Review result |
| --- | --- | --- |
| Scanner default paths | `.env.example`, GitHub workflows, backend app, backend fixtures, config, docs, frontend src, reports, scripts, and README. | Adequate for Phase 9 tracked artifacts. |
| Placeholder handling | ENV references and placeholders are allowed; real-looking secret assignments are blocked. | Adequate. |
| CI gate | `.github/workflows/ci.yml` runs `python scripts/scan_secrets.py` after backend/frontend validation. | Adequate. |
| Untracked local files | Default scan is tracked-only. | Acceptable for PR safety; local manual evidence must not be committed unless scanned. |

## Provider Key Boundary

| Surface | Current state | Required Phase 9 behavior |
| --- | --- | --- |
| Provider config object | `LLMProviderConfig` stores `provider_name`, `model_name`, `base_url`, `api_key_env`, timeout, and token limits. | Keep this ENV-name-only shape for DeepSeek. |
| API key read | `OpenAICompatibleStrategyBlueprintProvider.generate` reads `os.environ[api_key_env]` at call time and sends it only in the Authorization header. | Preserve; never persist or render the value. |
| Missing key | Missing env raises `LLMProviderConfigurationError` naming only the env var. | Convert to durable failed/BLOCKED run, not fake fallback. |
| Failed provider response | Provider raises a generic provider failure message without response body. | Preserve redaction; `#269` should add provider-safe error categories without raw body leakage. |
| Fake default | `STRATEGY_BLUEPRINT_PROVIDER` defaults to `fake`; fake provider is used for local smoke/tests. | Keep default fake to avoid accidental real API calls. |
| Real/fake label risk | `StrategyGenerationService` currently writes `params_snapshot={"mode": "offline"}` for every provider. | `#269` must change this to non-secret real provider metadata when provider is not fake, otherwise DeepSeek runs can be mislabeled. |

## Dry-Run And Live Trading Boundary

| Surface | Current state | Review result |
| --- | --- | --- |
| App config | `config/app.yaml` keeps `allow_live_trading: false` and `allow_dry_run_trading: false`; `allow_controlled_dry_run_process` defaults false in settings. | Adequate default. |
| Readiness endpoint | `DryRunReadinessService` never starts a process and reports `starts_freqtrade=false`, `live_trading=false`, `real_orders=false`, and `stores_sensitive_values=false`. | Adequate. |
| Controlled dry-run start | Blocks without manual approval and when backend process gate is disabled. | Adequate; real dry-run execution remains explicitly gated. |
| Freqtrade CLI runner | Allowlist-based; `trade` requires `--dry-run`; secret-shaped options and values are rejected. | Adequate. |
| Runtime read-only contract | Blocks unsafe settings such as `allow_live_trading=true` or `allow_dry_run_trading=true`. | Adequate. |
| Operator status | Reports ENV presence booleans and diagnostics, not ENV values. | Adequate; add DeepSeek env presence only as a boolean if needed in `#269`. |

## Redaction Boundary

| Surface | Current state | Review result |
| --- | --- | --- |
| Dry-run status payloads | Secret-shaped keys, assignment text, and bearer tokens are redacted recursively. | Adequate. |
| Controlled dry-run artifacts | Tests confirm stdout/stderr with secret-shaped fixture values are redacted before manifest persistence. | Adequate. |
| FreqUI link metadata | Secret-shaped config fields are rejected without rendering values. | Adequate. |
| Generated strategy static review | Generated strategy code is blocked from env, filesystem, network, subprocess, dynamic import, and secret access paths. | Adequate. |
| Provider response body | Current provider code does not persist raw failed HTTP bodies. | Adequate; continue avoiding raw provider payloads in `#269`/`#277`. |

## GitHub Issue And PR Boundary

The GitHub checks in this review intentionally returned only numbers, titles,
states, and URLs. They did not print bodies or candidate values.

| Query class | Result | Follow-up |
| --- | --- | --- |
| `sk-` | No Issue or PR results. | If a future search finds results, stop and rotate the leaked key before continuing. |
| `api_secret OR passphrase OR token` | No Issue or PR results. | Continue using env-var names and fake fixture phrases only. |
| `DEEPSEEK_API_KEY` | Expected environment-variable references only. | Keep values out of Issue/PR bodies and comments. |

## Required Gates For Dependent Issues

| Issue | Gate from this review |
| --- | --- |
| `#269` DeepSeek Provider | Implement DeepSeek as OpenAI-compatible ENV-only provider config. Do not call the API in normal tests. Add missing-key and failed-response tests with no value leakage. Fix `params_snapshot` so real provider metadata is not mislabeled as offline. |
| `#270` Provider result to DB | Persist run/strategy/version records with provider/model metadata only; error messages must be redacted and durable. |
| `#271` Strategy file validation | Generated files must pass static review and must not contain env/file/network/subprocess secret-access code. |
| `#272` Local backtest preflight | Missing binary, data, strategy file, config, or permission must be `BLOCKED`; no fallback success. |
| `#273` Backtest result and score DB | Artifacts and metrics must not include secret values; result success requires DB IDs. |
| `#274` and `#275` Frontend display | Pages must show source type and blockers; never show secret values or fake real success. |
| `#277` Single DeepSeek E2E | At most one narrow real call unless explicitly approved; record redacted provider metadata and DB/API/UI evidence. |
| `#330` Hourly local design | Design only; no production scheduler, live trading, real orders, or queue infrastructure. Require pause/cancel controls, a single-use lease, no concurrent attempts, and fail-closed preflight before any Provider call. |

## Bug Triggers

Create a structured Bug issue immediately if any of these are reproduced:

- any real key, token, secret, password, or passphrase appears in tracked files,
  logs, reports, DB rows, API payloads, UI text, screenshots, Issue text, PR
  text, or CI output;
- fake, fixture, fallback, mock, or unknown data is labeled as DeepSeek, real
  Provider output, or database-backed core success;
- DeepSeek missing-key or failed-response paths do not create a durable failed or
  `BLOCKED` record;
- provider failure includes raw response body, request headers, or Authorization
  content;
- `freqtrade trade` can be built or run without `--dry-run`;
- any path enables live trading, real orders, production deployment, or real
  exchange execution without a later explicit phase approval;
- frontend displays ENV values instead of presence/absence booleans;
- CI secret scan is removed, weakened, or bypassed for tracked artifacts.

## Acceptance For Issue #280

Issue `#280` is complete when this review is merged and the Project row is moved
to `Done`.

This review unlocks `#269` for implementation under the gates above. It does not
approve live trading, real orders, production deployment, unattended scheduling,
or extra DeepSeek calls beyond the later explicit E2E issue.
