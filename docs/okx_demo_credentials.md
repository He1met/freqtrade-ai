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

## One-time account pin onboarding

After the three signing credentials have been stored interactively, run
`make okx-demo-pin-account` explicitly. It is not invoked by startup or
preflight. The one-shot child reads only those three Keychain items, calls only
the fixed Demo `GET /api/v5/account/config`, validates the response,
`read_only,trade` permissions, Futures account level, and `long_short_mode`, then
computes the canonical account fingerprint in memory.

The fingerprint is written to the fixed fourth Keychain item through controlled
stdin with both macOS confirmation lines, never argv. The command then reads the
fixed account/service back and uses a constant-time comparison. An empty,
mismatched, or unreadable value causes immediate deletion of the newly created
item and returns `BLOCKED`. Credentials, `uid`, `mainUid`, and the fingerprint
are not written to stdout, stderr, or logs. An existing pin is rejected before
any network request and is never overwritten; there is no `--replace` mode.
Switching Demo accounts therefore requires the operator to explicitly delete
the old fingerprint item before running onboarding again.

## Read-only attestation

`make okx-demo-preflight` reads the four account Keychain items and the
attestation HMAC proof key only at the runtime
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
- Futures account level and `long_short_mode`.
- the fixed Keychain service
  `freqtrade-ai/okx-demo-attestation-proof-key`, provisioned only by
  `make db-attestation-harden`.

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

## Long-running runtime and credential rotation

The existing single LaunchAgent supervises one credential-bearing
`okx_runtime` child after backend, worker, and frontend readiness. No second
plist, virtualenv, database, or runtime directory is created. The child owns
the attested read adapter, startup reconciliation, and the sole writer process
lock.

After the Keychain bundle, account pin, and attestation proof key have been
provisioned, run `make okx-rotate-generation` once, followed by
`make autostart-restart`. The command validates that the complete OKX bundle
exists, generates non-secret random generation metadata, and writes only that
metadata to the fixed Keychain service
`freqtrade-ai/okx-demo-credential-generation`. It never prints the generation
or any credential.

Repeat `make okx-rotate-generation` after every replacement of an OKX key,
secret, passphrase, fingerprint, or attestation key, then run
`make autostart-restart`. The supervisor compares only this non-secret
generation. A new generation causes a controlled `down -> up -> verify`, and
the generation is accepted only after the replacement child verifies.
Temporary Keychain unavailability freezes new openings while leaving the
existing child alive for cancellation and reduce-only cleanup. When the same
generation becomes readable again, the supervisor removes that external
freeze; the reconciliation gate remains authoritative.
