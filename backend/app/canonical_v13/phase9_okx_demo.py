"""Credential-sealed OKX_DEMO port for the canonical Phase 9 canary.

The public object exposes only redacted attestation facts and the two operations
needed by the durable canonical order saga.  Raw credentials, authorization
headers, and the underlying write transport never cross this module boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from app.adapters.okx_demo.credential_preflight import (
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    require_pinned_account_fingerprint,
)
from app.adapters.okx_demo.read_adapter import OkxDemoReadClient
from app.adapters.okx_demo.server_factory import (
    OkxDemoServerSession,
    create_okx_demo_server_session,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked


class _WriterPort(Protocol):
    def post(
        self, *, path: str, body: Mapping[str, Any], timeout_seconds: float = 10.0
    ) -> Any: ...


@dataclass(frozen=True)
class RedactedOkxDemoProbe:
    execution_target: str
    instrument: str
    account_fingerprint_digest: str
    credential_generation_digest: str
    permissions: Mapping[str, bool]
    simulated_trading: bool
    allow_real_funds: bool
    observed_at: datetime
    expires_at: datetime
    instrument_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class CanonicalOkxDemoSession:
    """Sealed adapter implementing the canonical saga's place/query protocol."""

    def __init__(
        self,
        *,
        read_client: OkxDemoReadClient,
        write_port: _WriterPort,
        account_fingerprint_digest: str,
        credential_generation_digest: str,
        close_callback,
    ) -> None:
        for name, value in (
            ("account_fingerprint_digest", account_fingerprint_digest),
            ("credential_generation_digest", credential_generation_digest),
        ):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_OKX_DEMO_ATTESTATION_DIGEST", name
                )
        self.__read = read_client
        self.__write = write_port
        self.__account_fingerprint_digest = account_fingerprint_digest
        self.__credential_generation_digest = credential_generation_digest
        self.__close = close_callback
        self.__closed = False

    def probe(
        self,
        *,
        instrument: str,
        observed_at: datetime | None = None,
        ttl: timedelta = timedelta(seconds=45),
    ) -> RedactedOkxDemoProbe:
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None or not timedelta(0) < ttl <= timedelta(seconds=60):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ATTESTATION_FRESHNESS", "invalid probe time policy"
            )
        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        snapshot = self.__read.instruments(instrument)
        if len(snapshot.items) != 1 or snapshot.items[0].get("inst_id") != instrument:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_INSTRUMENT_IDENTITY", instrument
            )
        item = snapshot.items[0]
        if item.get("state") != "live":
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_INSTRUMENT_STATE", instrument
            )
        safe_instrument = {
            key: item.get(key)
            for key in (
                "inst_id",
                "inst_type",
                "base_ccy",
                "quote_ccy",
                "settle_ccy",
                "contract_value",
                "contract_value_ccy",
                "lot_size",
                "min_size",
                "tick_size",
                "state",
            )
        }
        resolved = now.astimezone(timezone.utc)
        return RedactedOkxDemoProbe(
            execution_target="OKX_DEMO",
            instrument=instrument,
            account_fingerprint_digest=self.__account_fingerprint_digest,
            credential_generation_digest=self.__credential_generation_digest,
            permissions={"read": True, "trade": True, "withdraw": False},
            simulated_trading=True,
            allow_real_funds=False,
            observed_at=resolved,
            expires_at=resolved + ttl,
            instrument_digest=_digest(safe_instrument),
        )

    def place(self, body: Mapping[str, str]) -> Mapping[str, Any]:
        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        payload = self.__write.post(path="/api/v5/trade/order", body=body)
        if not isinstance(payload, Mapping):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_RESPONSE", "write response is not an object"
            )
        return dict(payload)

    def query(self, *, instrument: str, client_order_id: str) -> Mapping[str, Any]:
        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        snapshot = self.__read.order(instrument, client_order_id=client_order_id)
        if len(snapshot.items) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ORDER_IDENTITY", client_order_id
            )
        item = snapshot.items[0]
        if (
            item.get("inst_id") != instrument
            or item.get("client_order_id") != client_order_id
            or not item.get("order_id")
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ORDER_IDENTITY", client_order_id
            )
        return {
            "code": "0",
            "data": [
                {
                    "ordId": str(item["order_id"]),
                    "clOrdId": client_order_id,
                    "sCode": "0",
                }
            ],
        }

    def fills(self, *, instrument: str, order_id: str) -> tuple[Mapping[str, Any], ...]:
        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        snapshot = self.__read.fills_history(instrument, limit=100)
        return tuple(
            dict(item)
            for item in snapshot.items
            if item.get("inst_id") == instrument and item.get("order_id") == order_id
        )

    def close(self) -> None:
        if not self.__closed:
            self.__closed = True
            self.__close()

    def __enter__(self) -> "CanonicalOkxDemoSession":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def create_canonical_okx_demo_session(
    environment: Mapping[str, str],
    *,
    credential_generation_digest: str,
    lock_path: Path,
) -> CanonicalOkxDemoSession:
    """Create a production session without returning credential-bearing objects."""

    pinned = require_pinned_account_fingerprint(environment)
    if environment.get(OKX_DEMO_ACCOUNT_FINGERPRINT_ENV) != pinned:
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_OKX_DEMO_ACCOUNT_PIN", "account pin differs"
        )
    server: OkxDemoServerSession = create_okx_demo_server_session(
        environment, lock_path=lock_path
    )
    try:
        # These sealed factories are intentionally reached only inside this
        # adapter; callers never gain the credential handle or HTTP transport.
        from app.adapters.okx_demo.write_transport import (
            _create_attested_writer_credential_bridge,
            _create_production_write_transport,
        )

        handle = _create_attested_writer_credential_bridge(server.read)
        write_port = _create_production_write_transport(handle)
        return CanonicalOkxDemoSession(
            read_client=server.read,
            write_port=write_port,
            account_fingerprint_digest=pinned,
            credential_generation_digest=credential_generation_digest,
            close_callback=server.close,
        )
    except BaseException:
        server.close()
        raise


__all__ = [
    "CanonicalOkxDemoSession",
    "RedactedOkxDemoProbe",
    "create_canonical_okx_demo_session",
]
