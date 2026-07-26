from __future__ import annotations

import re
from typing import Mapping


ATTESTATION_PROOF_KEY_ENV = "FREQTRADE_AI_OKX_DEMO_ATTESTATION_PROOF_KEY"
ATTESTATION_PROOF_KEYCHAIN_SERVICE = (
    "freqtrade-ai/okx-demo-attestation-proof-key"
)
_KEY_HEX = re.compile(r"^[0-9a-f]{64}$")


class AttestationProofKeyUnavailable(RuntimeError):
    pass


def require_attestation_proof_key(environment: Mapping[str, str]) -> bytes:
    value = environment.get(ATTESTATION_PROOF_KEY_ENV, "")
    if not isinstance(value, str) or _KEY_HEX.fullmatch(value) is None:
        raise AttestationProofKeyUnavailable(
            "OKX Demo attestation proof key is unavailable"
        )
    return bytes.fromhex(value)
