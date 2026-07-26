"""Project-owned OKX Demo adapter boundaries."""

from app.adapters.okx_demo.credentials import (
    OkxDemoCredentialProvider,
    OkxDemoCredentialsUnavailable,
    attest_okx_demo_credential_provider,
)
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.models import (
    ContractConversion,
    InstrumentSpec,
    OkxReadSnapshot,
    SnapshotMetadata,
)
from app.adapters.okx_demo.read_adapter import OkxDemoReadAdapter
from app.adapters.okx_demo.transport import (
    OkxReadHttpResponse,
    OkxReadTransport,
    UrllibOkxReadTransport,
)

__all__ = [
    "ContractConversion",
    "InstrumentSpec",
    "OkxDemoCredentialProvider",
    "OkxDemoCredentialsUnavailable",
    "OkxDemoReadAdapter",
    "OkxReadAdapterError",
    "OkxReadHttpResponse",
    "OkxReadSnapshot",
    "OkxReadTransport",
    "SnapshotMetadata",
    "UrllibOkxReadTransport",
    "attest_okx_demo_credential_provider",
]
