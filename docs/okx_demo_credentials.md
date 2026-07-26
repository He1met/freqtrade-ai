# OKX Demo Keychain credential boundary

The sole active exchange target is `OKX_DEMO`. Its API key, secret, passphrase,
and expected account fingerprint are stored as four macOS Keychain
generic-password items documented in the README. Environment variables, dotenv
files, PostgreSQL, launchd plist files, logs, UI payloads, issues, and manifests
are not credential stores.

The single local runtime removes all known exchange and provider secrets from
its inherited environment. It then uses explicit per-service allowlists:

- backend: canonical PostgreSQL, operator token when configured, and the
  existing DeepSeek Keychain boundary, with provider and model pinned by the
  runtime;
- DB-backed worker: canonical PostgreSQL and the same pinned DeepSeek boundary;
- frontend and ordinary preflight processes: no provider or exchange secrets;
- project-owned OKX adapter boundary: the complete OKX Demo Keychain bundle and
  fixed non-secret execution-target selectors.

Repository dotenv loading is disabled for every managed child. This prevents a
child from reconstructing a removed credential after process launch. Provider
endpoint and credential selector are fixed in code; shell and dotenv values
cannot redirect DeepSeek to another URL or secret.

## Read-only attestation

`make okx-demo-preflight` reads the four Keychain items only at the runtime
startup boundary and passes them only to the short-lived project adapter
preflight child. That child signs one `GET /api/v5/account/config` request with
the mandatory `x-simulated-trading: 1` header. Its environment does not inherit
proxy variables or custom CA paths.

The attestation is fail closed. It requires:

- the exact `OKX_DEMO` execution-target contract;
- successful authenticated Demo response;
- a non-empty account identity whose canonical SHA-256 matches the pinned
  Keychain fingerprint and is never rendered;
- exactly `read_only,trade` permissions and no withdrawal permission;
- Futures account level and `net_mode`.

Transport errors, invalid JSON, non-zero OKX response codes, missing or multiple
account records, unknown identity, excessive permissions, and account-mode
mismatches all return `BLOCKED`. Remote error messages and response identifiers
are never forwarded.

The preflight has no order-writing code and performs no POST request. It does
not prove order execution, and it must not be used as evidence that a later
write canary succeeded. `remote_account_evidence` covers only the authenticated
Demo response, fingerprint match, permissions, account level, and position
mode. `SWAP`, `isolated`, and `allow_real_funds=false` are reported separately
as `local_target_contract`; `/account/config` does not attest them.

The preflight child is synchronous and short lived. Its in-memory credential
bundle is cleared when the child exits, and no persistent credential holder is
registered, so current Keychain rotation does not require a LaunchAgent
restart. If #449 introduces a long-lived adapter, rotation must use a controlled
restart so the old process releases its previous credential values.
