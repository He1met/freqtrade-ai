from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.canonical_v13.phase9_keychain import (
    CanonicalPhase9KeychainBlocked,
    OKX_DEMO_GENERATION_SERVICE,
    OKX_DEMO_KEYCHAIN_SERVICES,
    read_canonical_okx_demo_capability,
)


def test_canonical_loader_reads_only_fixed_services_and_returns_sealed_environment(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.canonical_v13.phase9_keychain.sys.platform", "darwin")
    monkeypatch.setattr(
        "app.canonical_v13.phase9_keychain.SECURITY_PATH", Path("/usr/bin/true")
    )
    values = {
        "freqtrade-ai/okx-demo-api-key": "api-key-value",
        "freqtrade-ai/okx-demo-api-secret": "api-secret-value",
        "freqtrade-ai/okx-demo-api-passphrase": "passphrase-value",
        "freqtrade-ai/okx-demo-account-fingerprint": "a" * 64,
        "freqtrade-ai/okx-demo-attestation-proof-key": "b" * 64,
        OKX_DEMO_GENERATION_SERVICE: "generation-7",
    }
    observed = []

    def runner(command, **kwargs):
        observed.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, values[command[-2]] + "\n", "")

    capability = read_canonical_okx_demo_capability(runner=runner)
    assert len(observed) == len(OKX_DEMO_KEYCHAIN_SERVICES) + 1
    assert {entry[0][-2] for entry in observed} == set(values)
    assert all(entry[1]["stdin"] is subprocess.DEVNULL for entry in observed)
    assert all(entry[1]["timeout"] == 5 for entry in observed)
    assert capability.environment["FREQTRADE_AI_EXECUTION_TARGET"] == "OKX_DEMO"
    assert capability.environment["FREQTRADE_AI_ALLOW_REAL_FUNDS"] == "false"
    assert (
        capability.environment["FREQTRADE_AI_OKX_DEMO_REST_URL"]
        == "https://openapi.okx.com"
    )
    assert len(capability.credential_generation_digest) == 64
    assert "api-secret-value" not in repr(capability.credential_generation_digest)


def test_canonical_loader_fails_closed_on_invalid_proof(monkeypatch) -> None:
    monkeypatch.setattr("app.canonical_v13.phase9_keychain.sys.platform", "darwin")
    monkeypatch.setattr(
        "app.canonical_v13.phase9_keychain.SECURITY_PATH", Path("/usr/bin/true")
    )

    def runner(command, **_kwargs):
        service = command[-2]
        value = "bad" if service.endswith("attestation-proof-key") else "a" * 64
        if service == OKX_DEMO_GENERATION_SERVICE:
            value = "generation-7"
        return subprocess.CompletedProcess(command, 0, value, "")

    with pytest.raises(
        CanonicalPhase9KeychainBlocked, match="BLOCKED_PHASE9_KEYCHAIN_CONTRACT"
    ):
        read_canonical_okx_demo_capability(runner=runner)
