# Phase 7 Secret Scanning

## Scope

Phase 7 adds a repo-local secret scanning gate:

```bash
python scripts/scan_secrets.py
```

The default scan is limited to git-tracked engineering paths:

- `.env.example`
- `.github/workflows`
- `backend/app`
- `backend/tests/fixtures`
- `config`
- `docs`
- `frontend/src`
- `reports`
- `scripts`
- `README.md`

It intentionally ignores local untracked directories such as market data,
router backups, firmware downloads, virtual environments, `node_modules`, and
cache directories. CI runs the same tracked-file gate.

## Detection Rules

The scanner looks for secret-shaped assignments such as API keys, API secrets,
passwords, passphrases, tokens, private keys, and authorization values. When a
finding is detected, output is limited to:

- path
- line number
- key name
- rule id

The matched value is never printed in text or JSON output.

Allowed references include placeholders and ENV references:

```dotenv
OKX_DEMO_API_KEY=change_me
OKX_DEMO_API_SECRET=${OKX_DEMO_API_SECRET}
```

```yaml
exchange:
  api_key_env: OKX_DEMO_API_KEY
  api_secret_env: OKX_DEMO_API_SECRET
```

Tests and fixtures should use clearly non-real values such as
`fixture-api-key-that-must-not-render` when a redaction path needs sample input.
Do not use real credentials or real-looking credential samples.

## Blocked Result Handling

A `BLOCKED` result means a secret-shaped value may be committed or archived.
Do not bypass the gate. Remove the value, replace it with an ENV reference or
placeholder, and rerun:

```bash
python scripts/scan_secrets.py
```

If the finding is a legitimate false positive, rewrite the example so it uses a
placeholder or an explicit ENV reference. The scanner should not be weakened to
allow real-looking values.

## Safety Boundary

The scanner does not read real environment values, connect to exchanges, start
Freqtrade, download K-lines, deploy services, or inspect ignored local data. It
only reads repo-local text files selected by the scan scope.
