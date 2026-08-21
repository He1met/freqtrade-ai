"""Credential-sealed OKX_DEMO port for the canonical Phase 9 canary.

The public object exposes only redacted attestation facts and the two operations
needed by the durable canonical order saga.  Raw credentials, authorization
headers, and the underlying write transport never cross this module boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

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
    contract_value: str
    contract_value_currency: str
    lot_size: str
    min_size: str
    tick_size: str
    mark_price: str
    current_long_leverage: str
    instrument_digest: str
    instrument_observed_at: datetime
    instrument_expires_at: datetime
    mark_price_digest: str
    mark_price_observed_at: datetime
    mark_price_expires_at: datetime
    account_config_digest: str
    account_config_observed_at: datetime
    account_config_expires_at: datetime
    leverage_digest: str
    leverage_observed_at: datetime
    leverage_expires_at: datetime


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


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _utc(value: object, *, code: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CanonicalExecutionChainBlocked(code, "snapshot time is not aware UTC")
    return value.astimezone(timezone.utc)


def _positive_decimal(value: object, *, code: str, field: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal(0)
    if not parsed.is_finite() or parsed <= 0:
        raise CanonicalExecutionChainBlocked(code, f"{field} must be positive")
    return format(parsed, "f")


def _verified_snapshot(
    snapshot: object,
    *,
    resource: str,
    authenticated: bool,
    now: datetime,
) -> tuple[list[Mapping[str, Any]], datetime, datetime]:
    metadata = _field(snapshot, "metadata")
    observed = _utc(
        _field(metadata, "exchange_timestamp") or _field(metadata, "fetched_at"),
        code="BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
    )
    expires = _utc(
        _field(metadata, "expires_at"), code="BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS"
    )
    items = _field(snapshot, "items")
    if (
        _field(snapshot, "status") != "READY"
        or _field(metadata, "execution_target") != "OKX_DEMO"
        or _field(metadata, "source") != "okx_demo_rest"
        or _field(metadata, "resource") != resource
        or _field(metadata, "stale") is not False
        or _field(metadata, "authenticated") is not authenticated
        or observed > now
        or expires <= now
        or not isinstance(items, list)
        or any(not isinstance(item, Mapping) for item in items)
    ):
        raise CanonicalExecutionChainBlocked(
            "BLOCKED_OKX_DEMO_SNAPSHOT_FRESHNESS",
            f"{resource} snapshot is not exact, READY, and fresh",
        )
    return items, observed, expires


def _resource_digest(
    *,
    resource: str,
    observed_at: datetime,
    expires_at: datetime,
    authenticated: bool,
    facts: Mapping[str, object],
) -> str:
    return _digest(
        {
            "execution_target": "OKX_DEMO",
            "resource": resource,
            "source": "okx_demo_rest",
            "authenticated": authenticated,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "facts": dict(facts),
        }
    )


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
        now_provider: Callable[[], datetime] | None = None,
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
        self.__now = now_provider or (lambda: datetime.now(timezone.utc))
        self.__closed = False

    def probe(
        self,
        *,
        instrument: str,
        ttl: timedelta = timedelta(seconds=45),
    ) -> RedactedOkxDemoProbe:
        if not timedelta(0) < ttl <= timedelta(seconds=60):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ATTESTATION_FRESHNESS", "invalid probe time policy"
            )
        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        instrument_snapshot = self.__read.instruments(instrument)
        mark_snapshot = self.__read.mark_price(instrument)
        account_snapshot = self.__read.account_config()
        leverage_snapshot = self.__read.leverage(instrument)
        now = _utc(self.__now(), code="BLOCKED_OKX_DEMO_ATTESTATION_FRESHNESS")
        instrument_items, instrument_observed, instrument_expires = _verified_snapshot(
            instrument_snapshot,
            resource="instruments",
            authenticated=False,
            now=now,
        )
        mark_items, mark_observed, mark_expires = _verified_snapshot(
            mark_snapshot, resource="mark_price", authenticated=False, now=now
        )
        account_items, account_observed, account_expires = _verified_snapshot(
            account_snapshot, resource="account_config", authenticated=True, now=now
        )
        leverage_items, leverage_observed, leverage_expires = _verified_snapshot(
            leverage_snapshot, resource="leverage", authenticated=True, now=now
        )
        if (
            len(instrument_items) != 1
            or instrument_items[0].get("inst_id") != instrument
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_INSTRUMENT_IDENTITY", instrument
            )
        item = instrument_items[0]
        if (
            item.get("inst_type") != "SWAP"
            or item.get("contract_type") != "linear"
            or item.get("state") != "live"
            or item.get("contract_value_ccy") != item.get("base_ccy")
            or item.get("settle_ccy") != item.get("quote_ccy")
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_INSTRUMENT_STATE", instrument
            )
        contract_value = _positive_decimal(
            item.get("contract_value"),
            code="BLOCKED_OKX_DEMO_INSTRUMENT_CONTRACT",
            field="contract_value",
        )
        lot_size = _positive_decimal(
            item.get("lot_size"),
            code="BLOCKED_OKX_DEMO_INSTRUMENT_CONTRACT",
            field="lot_size",
        )
        min_size = _positive_decimal(
            item.get("min_size"),
            code="BLOCKED_OKX_DEMO_INSTRUMENT_CONTRACT",
            field="min_size",
        )
        tick_size = _positive_decimal(
            item.get("tick_size"),
            code="BLOCKED_OKX_DEMO_INSTRUMENT_CONTRACT",
            field="tick_size",
        )
        safe_instrument = {
            "inst_id": instrument,
            "inst_type": "SWAP",
            "contract_type": "linear",
            "base_ccy": item.get("base_ccy"),
            "quote_ccy": item.get("quote_ccy"),
            "settle_ccy": item.get("settle_ccy"),
            "contract_value": contract_value,
            "contract_value_ccy": item.get("contract_value_ccy"),
            "lot_size": lot_size,
            "min_size": min_size,
            "tick_size": tick_size,
            "state": "live",
        }

        if len(mark_items) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_MARK_IDENTITY", instrument
            )
        mark = mark_items[0]
        mark_timestamp = _utc(
            mark.get("timestamp"), code="BLOCKED_OKX_DEMO_MARK_FRESHNESS"
        )
        if (
            mark.get("inst_id") != instrument
            or mark.get("price_kind") != "mark"
            or mark_timestamp != mark_observed
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_MARK_IDENTITY", instrument
            )
        mark_price = _positive_decimal(
            mark.get("price"), code="BLOCKED_OKX_DEMO_MARK_PRICE", field="mark_price"
        )
        safe_mark = {
            "inst_id": instrument,
            "price_kind": "mark",
            "price": mark_price,
            "timestamp": mark_timestamp.isoformat(),
        }

        if len(account_items) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ACCOUNT_CONFIG", "account config must be singular"
            )
        account = account_items[0]
        if (
            account.get("account_level") != "2"
            or account.get("position_mode") != "long_short_mode"
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ACCOUNT_CONFIG",
                "Futures long_short_mode is required",
            )
        safe_account = {
            "account_level": "2",
            "position_mode": "long_short_mode",
            "account_fingerprint_digest": self.__account_fingerprint_digest,
            "credential_generation_digest": self.__credential_generation_digest,
            "permissions": {"read": True, "trade": True, "withdraw": False},
            "simulated_trading": True,
        }

        leverage_by_side: dict[str, str] = {}
        for leverage in leverage_items:
            side = leverage.get("position_side")
            if (
                leverage.get("inst_id") != instrument
                or leverage.get("margin_mode") != "isolated"
                or side not in {"long", "short"}
                or side in leverage_by_side
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_OKX_DEMO_LEVERAGE_IDENTITY", instrument
                )
            leverage_by_side[str(side)] = _positive_decimal(
                leverage.get("leverage"),
                code="BLOCKED_OKX_DEMO_LEVERAGE_VALUE",
                field=f"{side}_leverage",
            )
        if set(leverage_by_side) != {"long", "short"}:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_LEVERAGE_IDENTITY",
                "exact long and short leverage rows are required",
            )

        resolved = max(
            instrument_observed, mark_observed, account_observed, leverage_observed
        )
        expires = min(
            instrument_expires,
            mark_expires,
            account_expires,
            leverage_expires,
            resolved + ttl,
        )
        if expires <= now:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_ATTESTATION_FRESHNESS",
                "combined probe freshness window is empty",
            )
        return RedactedOkxDemoProbe(
            execution_target="OKX_DEMO",
            instrument=instrument,
            account_fingerprint_digest=self.__account_fingerprint_digest,
            credential_generation_digest=self.__credential_generation_digest,
            permissions={"read": True, "trade": True, "withdraw": False},
            simulated_trading=True,
            allow_real_funds=False,
            observed_at=resolved,
            expires_at=expires,
            contract_value=contract_value,
            contract_value_currency=str(item["contract_value_ccy"]),
            lot_size=lot_size,
            min_size=min_size,
            tick_size=tick_size,
            mark_price=mark_price,
            current_long_leverage=leverage_by_side["long"],
            instrument_digest=_resource_digest(
                resource="instruments",
                observed_at=instrument_observed,
                expires_at=instrument_expires,
                authenticated=False,
                facts=safe_instrument,
            ),
            instrument_observed_at=instrument_observed,
            instrument_expires_at=instrument_expires,
            mark_price_digest=_resource_digest(
                resource="mark_price",
                observed_at=mark_observed,
                expires_at=mark_expires,
                authenticated=False,
                facts=safe_mark,
            ),
            mark_price_observed_at=mark_observed,
            mark_price_expires_at=mark_expires,
            account_config_digest=_resource_digest(
                resource="account_config",
                observed_at=account_observed,
                expires_at=account_expires,
                authenticated=True,
                facts=safe_account,
            ),
            account_config_observed_at=account_observed,
            account_config_expires_at=account_expires,
            leverage_digest=_resource_digest(
                resource="leverage",
                observed_at=leverage_observed,
                expires_at=leverage_expires,
                authenticated=True,
                facts={
                    "instrument": instrument,
                    "account_fingerprint_digest": self.__account_fingerprint_digest,
                    **leverage_by_side,
                },
            ),
            leverage_observed_at=leverage_observed,
            leverage_expires_at=leverage_expires,
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
