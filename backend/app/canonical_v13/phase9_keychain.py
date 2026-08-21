"""Canonical-only Keychain boundary for the OKX_DEMO Phase 9 worker.

The loader has no environment or dotenv fallback and never logs item values.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
from typing import Callable, Final, Sequence


KEYCHAIN_TIMEOUT_SECONDS: Final = 5
SECURITY_PATH: Final = Path("/usr/bin/security")
OKX_DEMO_KEYCHAIN_SERVICES: Final[dict[str, str]] = dict(
    zip(
        (
            "OKX_DEMO_API_KEY",
            "OKX_DEMO_API_SECRET",
            "OKX_DEMO_API_PASSPHRASE",
            "OKX_DEMO_ACCOUNT_FINGERPRINT",
            "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY",
        ),
        (
            "freqtrade-ai/okx-demo-api-key",
            "freqtrade-ai/okx-demo-api-secret",
            "freqtrade-ai/okx-demo-api-passphrase",
            "freqtrade-ai/okx-demo-account-fingerprint",
            "freqtrade-ai/okx-demo-attestation-proof-key",
        ),
        strict=True,
    )
)
OKX_DEMO_GENERATION_SERVICE: Final = "freqtrade-ai/okx-demo-credential-generation"


class CanonicalPhase9KeychainBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CanonicalOkxDemoCapability:
    environment: dict[str, str]
    credential_generation_digest: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _read(service: str, *, runner: Runner, account: str) -> str:
    try:
        completed = runner(
            (
                str(SECURITY_PATH),
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_READ", "security command unavailable"
        ) from exc
    value = completed.stdout.rstrip("\r\n")
    if (
        completed.returncode != 0
        or not value
        or len(value) > 16384
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_ITEM", "required canonical item is unavailable"
        )
    return value


def read_canonical_okx_demo_capability(
    *, runner: Runner = subprocess.run
) -> CanonicalOkxDemoCapability:
    if sys.platform != "darwin" or not SECURITY_PATH.is_file():
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_PLATFORM", "macOS security is required"
        )
    account = pwd.getpwuid(os.getuid()).pw_name
    values = {
        name: _read(service, runner=runner, account=account)
        for name, service in OKX_DEMO_KEYCHAIN_SERVICES.items()
    }
    fingerprint = values["OKX_DEMO_ACCOUNT_FINGERPRINT"]
    proof = values["FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"]
    generation = _read(OKX_DEMO_GENERATION_SERVICE, runner=runner, account=account)
    if (
        re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or re.fullmatch(r"[0-9a-f]{64}", proof) is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", generation) is None
    ):
        values.clear()
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_CONTRACT",
            "fingerprint, proof, or generation metadata is invalid",
        )
    values.update(
        {
            "FREQTRADE_AI_EXECUTION_TARGET": "OKX_DEMO",
            "FREQTRADE_AI_ALLOW_REAL_FUNDS": "false",
            "FREQTRADE_AI_OKX_DEMO_REST_URL": "https://openapi.okx.com",
        }
    )
    return CanonicalOkxDemoCapability(
        environment=values,
        credential_generation_digest=sha256(
            f"okx-demo-generation:{generation}".encode("utf-8")
        ).hexdigest(),
    )


def read_canonical_service_secret(
    service: str, *, runner: Runner = subprocess.run
) -> str:
    """Read one allowlisted Phase 9 service secret into memory only."""

    allowed = {
        "freqtrade-ai/v13/runtime-signal-receipt-hmac-v1",
    }
    if service not in allowed:
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_SERVICE", "service is not allowlisted"
        )
    if sys.platform != "darwin" or not SECURITY_PATH.is_file():
        raise CanonicalPhase9KeychainBlocked(
            "BLOCKED_PHASE9_KEYCHAIN_PLATFORM", "macOS security is required"
        )
    return _read(
        service,
        runner=runner,
        account=pwd.getpwuid(os.getuid()).pw_name,
    )


__all__ = [
    "CanonicalOkxDemoCapability",
    "CanonicalPhase9KeychainBlocked",
    "OKX_DEMO_GENERATION_SERVICE",
    "OKX_DEMO_KEYCHAIN_SERVICES",
    "read_canonical_okx_demo_capability",
    "read_canonical_service_secret",
]
