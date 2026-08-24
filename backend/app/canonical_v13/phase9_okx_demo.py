"""Credential-sealed OKX_DEMO port for the canonical Phase 9 canary.

The public object exposes only redacted attestation facts and the two operations
needed by the durable canonical order saga.  Raw credentials, authorization
headers, and the underlying write transport never cross this module boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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
    current_short_leverage: str
    exchange_max_leverage: str
    limit_price: str
    maximum_buy_contracts: str
    long_contracts: str
    short_contracts: str
    active_position_count: int
    pending_order_count: int
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
    exchange_max_leverage_digest: str
    exchange_max_leverage_observed_at: datetime
    exchange_max_leverage_expires_at: datetime
    positions_digest: str
    positions_observed_at: datetime
    positions_expires_at: datetime
    pending_orders_digest: str
    pending_orders_observed_at: datetime
    pending_orders_expires_at: datetime
    maximum_order_quantity_digest: str
    maximum_order_quantity_observed_at: datetime
    maximum_order_quantity_expires_at: datetime


@dataclass(frozen=True)
class RedactedOkxDemoDispatchGuard:
    execution_target: str
    instrument: str
    account_fingerprint_digest: str
    credential_generation_digest: str
    limit_price: str
    effective_leverage: str
    current_short_leverage: str
    minimum_size: str
    maximum_buy_contracts: str
    long_contracts: str
    short_contracts: str
    active_position_count: int
    pending_order_count: int
    observed_at: datetime
    expires_at: datetime
    positions_digest: str
    positions_observed_at: datetime
    positions_expires_at: datetime
    pending_orders_digest: str
    pending_orders_observed_at: datetime
    pending_orders_expires_at: datetime
    maximum_order_quantity_digest: str
    maximum_order_quantity_observed_at: datetime
    maximum_order_quantity_expires_at: datetime
    leverage_digest: str
    leverage_observed_at: datetime
    leverage_expires_at: datetime

    @property
    def guard_digest(self) -> str:
        return _digest(redacted_dispatch_guard_payload(self))


def redacted_dispatch_guard_payload(
    guard: RedactedOkxDemoDispatchGuard,
) -> dict[str, object]:
    return {
        "contract": "canonical-v13-okx-demo-dispatch-guard-v1",
        "execution_target": guard.execution_target,
        "instrument": guard.instrument,
        "account_fingerprint_digest": guard.account_fingerprint_digest,
        "credential_generation_digest": guard.credential_generation_digest,
        "limit_price": guard.limit_price,
        "effective_leverage": guard.effective_leverage,
        "current_short_leverage": guard.current_short_leverage,
        "minimum_size": guard.minimum_size,
        "maximum_buy_contracts": guard.maximum_buy_contracts,
        "long_contracts": guard.long_contracts,
        "short_contracts": guard.short_contracts,
        "active_position_count": guard.active_position_count,
        "pending_order_count": guard.pending_order_count,
        "observed_at": guard.observed_at.isoformat(),
        "expires_at": guard.expires_at.isoformat(),
        "positions_digest": guard.positions_digest,
        "positions_observed_at": guard.positions_observed_at.isoformat(),
        "positions_expires_at": guard.positions_expires_at.isoformat(),
        "pending_orders_digest": guard.pending_orders_digest,
        "pending_orders_observed_at": guard.pending_orders_observed_at.isoformat(),
        "pending_orders_expires_at": guard.pending_orders_expires_at.isoformat(),
        "maximum_order_quantity_digest": guard.maximum_order_quantity_digest,
        "maximum_order_quantity_observed_at": (
            guard.maximum_order_quantity_observed_at.isoformat()
        ),
        "maximum_order_quantity_expires_at": (
            guard.maximum_order_quantity_expires_at.isoformat()
        ),
        "leverage_digest": guard.leverage_digest,
        "leverage_observed_at": guard.leverage_observed_at.isoformat(),
        "leverage_expires_at": guard.leverage_expires_at.isoformat(),
    }


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


def _nonnegative_decimal(value: object, *, code: str, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = Decimal("NaN")
    if not parsed.is_finite() or parsed < 0:
        raise CanonicalExecutionChainBlocked(code, f"{field} must be nonnegative")
    return parsed


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
        exchange_max_snapshot = self.__read.exchange_max_leverage(instrument)
        positions_snapshot = self.__read.positions(instrument)
        pending_orders_snapshot = self.__read.pending_orders(instrument, limit=100)
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
        exchange_max_items, exchange_max_observed, exchange_max_expires = (
            _verified_snapshot(
                exchange_max_snapshot,
                resource="exchange_max_leverage",
                authenticated=True,
                now=now,
            )
        )
        positions_items, positions_observed, positions_expires = _verified_snapshot(
            positions_snapshot,
            resource="positions",
            authenticated=True,
            now=now,
        )
        pending_items, pending_observed, pending_expires = _verified_snapshot(
            pending_orders_snapshot,
            resource="pending_orders",
            authenticated=True,
            now=now,
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

        if len(exchange_max_items) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_IDENTITY",
                "exchange maximum leverage must be singular",
            )
        exchange_max = exchange_max_items[0]
        exchange_max_leverage = _positive_decimal(
            exchange_max.get("max_leverage"),
            code="BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_VALUE",
            field="exchange_max_leverage",
        )
        exchange_min_leverage = _positive_decimal(
            exchange_max.get("min_leverage"),
            code="BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_VALUE",
            field="exchange_min_leverage",
        )
        requested_leverage = _positive_decimal(
            exchange_max.get("requested_leverage"),
            code="BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_VALUE",
            field="requested_leverage",
        )
        if (
            exchange_max.get("inst_id") != instrument
            or exchange_max.get("inst_type") != "SWAP"
            or exchange_max.get("margin_mode") != "isolated"
            or exchange_max.get("position_side") != "long"
            or Decimal(exchange_min_leverage) > Decimal(exchange_max_leverage)
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_EXCHANGE_MAX_LEVERAGE_IDENTITY", instrument
            )

        if exchange_max.get("has_pending_orders") is not False:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_PENDING_ORDERS",
                "exchange leverage authority reports an existing order",
            )
        totals = {"long": Decimal(0), "short": Decimal(0)}
        active_position_count = 0
        for position in positions_items:
            side = position.get("position_side")
            contracts = _nonnegative_decimal(
                position.get("contracts"),
                code="BLOCKED_OKX_DEMO_POSITION_VALUE",
                field="contracts",
            )
            if (
                position.get("inst_id") != instrument
                or position.get("margin_mode") != "isolated"
                or side not in totals
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_OKX_DEMO_POSITION_IDENTITY", instrument
                )
            totals[str(side)] += contracts
            active_position_count += int(contracts > 0)
        if (
            totals != {"long": Decimal(0), "short": Decimal(0)}
            or active_position_count != 0
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_POSITION_NOT_FLAT",
                "canary requires zero long and short contracts",
            )
        if pending_items:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_PENDING_ORDERS",
                "canary requires zero pending orders",
            )
        limit_price_decimal = (
            Decimal(mark_price) / Decimal(tick_size)
        ).to_integral_value(rounding=ROUND_DOWN) * Decimal(tick_size)
        if not limit_price_decimal.is_finite() or limit_price_decimal <= 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_LIMIT_PRICE",
                "server-derived tick-aligned limit price must be positive",
            )
        limit_price = format(limit_price_decimal, "f")
        leverage_cap = Decimal(exchange_max_leverage)
        effective_leverage = Decimal(leverage_by_side["long"])
        if effective_leverage > leverage_cap:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_CURRENT_LEVERAGE_EXCEEDS_CAP",
                "current long leverage exceeds authenticated exchange authority",
            )
        maximum_snapshot = self.__read.maximum_order_quantity(
            instrument,
            td_mode="isolated",
            price=limit_price_decimal,
            leverage=effective_leverage,
        )
        maximum_items, maximum_observed, maximum_expires = _verified_snapshot(
            maximum_snapshot,
            resource="maximum_order_quantity",
            authenticated=True,
            now=now,
        )
        if len(maximum_items) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_IDENTITY",
                "maximum order quantity must be singular",
            )
        maximum = maximum_items[0]
        maximum_buy_contracts = format(
            _nonnegative_decimal(
                maximum.get("max_buy"),
                code="BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_VALUE",
                field="max_buy",
            ),
            "f",
        )
        if (
            maximum.get("inst_id") != instrument
            or maximum.get("margin_mode") != "isolated"
            or _positive_decimal(
                maximum.get("price"),
                code="BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_VALUE",
                field="price",
            )
            != limit_price
            or Decimal(
                _positive_decimal(
                    maximum.get("leverage"),
                    code="BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_VALUE",
                    field="leverage",
                )
            )
            != effective_leverage
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_MAXIMUM_ORDER_QUANTITY_IDENTITY", instrument
            )
        if Decimal(maximum_buy_contracts) < Decimal(min_size):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_CAPACITY_SHORTFALL",
                "exchange maximum buy quantity is below minimum size",
            )

        safe_positions = {
            "instrument": instrument,
            "margin_mode": "isolated",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": active_position_count,
        }
        safe_pending = {
            "instrument": instrument,
            "pending_order_count": 0,
            "exchange_reports_pending_orders": False,
        }
        safe_maximum = {
            "instrument": instrument,
            "margin_mode": "isolated",
            "limit_price": limit_price,
            "effective_leverage": format(effective_leverage, "f"),
            "maximum_buy_contracts": maximum_buy_contracts,
        }

        resolved = max(
            instrument_observed,
            mark_observed,
            account_observed,
            leverage_observed,
            exchange_max_observed,
            positions_observed,
            pending_observed,
            maximum_observed,
        )
        expires = min(
            instrument_expires,
            mark_expires,
            account_expires,
            leverage_expires,
            exchange_max_expires,
            positions_expires,
            pending_expires,
            maximum_expires,
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
            current_short_leverage=leverage_by_side["short"],
            exchange_max_leverage=exchange_max_leverage,
            limit_price=limit_price,
            maximum_buy_contracts=maximum_buy_contracts,
            long_contracts="0",
            short_contracts="0",
            active_position_count=active_position_count,
            pending_order_count=0,
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
            exchange_max_leverage_digest=_resource_digest(
                resource="exchange_max_leverage",
                observed_at=exchange_max_observed,
                expires_at=exchange_max_expires,
                authenticated=True,
                facts={
                    "instrument": instrument,
                    "exchange_max_leverage": exchange_max_leverage,
                    "has_pending_orders": False,
                },
            ),
            exchange_max_leverage_observed_at=exchange_max_observed,
            exchange_max_leverage_expires_at=exchange_max_expires,
            positions_digest=_resource_digest(
                resource="positions",
                observed_at=positions_observed,
                expires_at=positions_expires,
                authenticated=True,
                facts=safe_positions,
            ),
            positions_observed_at=positions_observed,
            positions_expires_at=positions_expires,
            pending_orders_digest=_resource_digest(
                resource="pending_orders",
                observed_at=pending_observed,
                expires_at=pending_expires,
                authenticated=True,
                facts=safe_pending,
            ),
            pending_orders_observed_at=pending_observed,
            pending_orders_expires_at=pending_expires,
            maximum_order_quantity_digest=_resource_digest(
                resource="maximum_order_quantity",
                observed_at=maximum_observed,
                expires_at=maximum_expires,
                authenticated=True,
                facts=safe_maximum,
            ),
            maximum_order_quantity_observed_at=maximum_observed,
            maximum_order_quantity_expires_at=maximum_expires,
        )

    def dispatch_guard(
        self,
        *,
        instrument: str,
        limit_price: str,
        effective_leverage: str,
        minimum_size: str,
        ttl: timedelta = timedelta(seconds=10),
    ) -> RedactedOkxDemoDispatchGuard:
        """Re-read authenticated account state immediately before the only POST."""

        if self.__closed:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_SESSION_CLOSED", "session is closed"
            )
        if not timedelta(0) < ttl <= timedelta(seconds=15):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_GUARD_FRESHNESS", "invalid guard TTL"
            )
        price = Decimal(
            _positive_decimal(
                limit_price,
                code="BLOCKED_OKX_DEMO_DISPATCH_GUARD_VALUE",
                field="limit_price",
            )
        )
        leverage = Decimal(
            _positive_decimal(
                effective_leverage,
                code="BLOCKED_OKX_DEMO_DISPATCH_GUARD_VALUE",
                field="effective_leverage",
            )
        )
        required_size = Decimal(
            _positive_decimal(
                minimum_size,
                code="BLOCKED_OKX_DEMO_DISPATCH_GUARD_VALUE",
                field="minimum_size",
            )
        )
        positions_snapshot = self.__read.positions(instrument)
        pending_snapshot = self.__read.pending_orders(instrument, limit=100)
        leverage_snapshot = self.__read.leverage(instrument)
        maximum_snapshot = self.__read.maximum_order_quantity(
            instrument,
            td_mode="isolated",
            price=price,
            leverage=leverage,
        )
        now = _utc(self.__now(), code="BLOCKED_OKX_DEMO_DISPATCH_GUARD_FRESHNESS")
        positions, positions_observed, positions_expires = _verified_snapshot(
            positions_snapshot,
            resource="positions",
            authenticated=True,
            now=now,
        )
        pending, pending_observed, pending_expires = _verified_snapshot(
            pending_snapshot,
            resource="pending_orders",
            authenticated=True,
            now=now,
        )
        leverage_items, leverage_observed, leverage_expires = _verified_snapshot(
            leverage_snapshot,
            resource="leverage",
            authenticated=True,
            now=now,
        )
        maximum, maximum_observed, maximum_expires = _verified_snapshot(
            maximum_snapshot,
            resource="maximum_order_quantity",
            authenticated=True,
            now=now,
        )
        totals = {"long": Decimal(0), "short": Decimal(0)}
        active_count = 0
        for item in positions:
            side = item.get("position_side")
            contracts = _nonnegative_decimal(
                item.get("contracts"),
                code="BLOCKED_OKX_DEMO_DISPATCH_POSITION_VALUE",
                field="contracts",
            )
            if (
                item.get("inst_id") != instrument
                or item.get("margin_mode") != "isolated"
                or side not in totals
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_OKX_DEMO_DISPATCH_POSITION_IDENTITY", instrument
                )
            totals[str(side)] += contracts
            active_count += int(contracts > 0)
        if totals != {"long": Decimal(0), "short": Decimal(0)} or active_count != 0:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_POSITION_NOT_FLAT",
                "dispatch guard requires zero long and short contracts",
            )
        if pending:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_PENDING_ORDERS",
                "dispatch guard requires zero pending orders",
            )
        leverage_by_side: dict[str, str] = {}
        for item in leverage_items:
            side = item.get("position_side")
            if (
                item.get("inst_id") != instrument
                or item.get("margin_mode") != "isolated"
                or side not in {"long", "short"}
                or side in leverage_by_side
            ):
                raise CanonicalExecutionChainBlocked(
                    "BLOCKED_OKX_DEMO_DISPATCH_LEVERAGE_IDENTITY", instrument
                )
            leverage_by_side[str(side)] = _positive_decimal(
                item.get("leverage"),
                code="BLOCKED_OKX_DEMO_DISPATCH_LEVERAGE_VALUE",
                field=f"{side}_leverage",
            )
        if (
            set(leverage_by_side) != {"long", "short"}
            or Decimal(leverage_by_side["long"]) != leverage
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_LEVERAGE_DRIFT",
                "current long leverage must equal the frozen policy leverage",
            )
        if len(maximum) != 1:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_MAXIMUM_IDENTITY",
                "dispatch maximum quantity must be singular",
            )
        maximum_item = maximum[0]
        maximum_buy = _nonnegative_decimal(
            maximum_item.get("max_buy"),
            code="BLOCKED_OKX_DEMO_DISPATCH_MAXIMUM_VALUE",
            field="max_buy",
        )
        if (
            maximum_item.get("inst_id") != instrument
            or maximum_item.get("margin_mode") != "isolated"
            or Decimal(str(maximum_item.get("price"))) != price
            or Decimal(str(maximum_item.get("leverage"))) != leverage
        ):
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_MAXIMUM_IDENTITY", instrument
            )
        if maximum_buy < required_size:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_CAPACITY_SHORTFALL",
                "maximum buy quantity is below the frozen minimum size",
            )
        safe_positions = {
            "instrument": instrument,
            "margin_mode": "isolated",
            "long_contracts": "0",
            "short_contracts": "0",
            "active_position_count": active_count,
        }
        safe_pending = {"instrument": instrument, "pending_order_count": 0}
        safe_maximum = {
            "instrument": instrument,
            "margin_mode": "isolated",
            "limit_price": format(price, "f"),
            "effective_leverage": format(leverage, "f"),
            "maximum_buy_contracts": format(maximum_buy, "f"),
        }
        safe_leverage = {
            "instrument": instrument,
            "account_fingerprint_digest": self.__account_fingerprint_digest,
            **leverage_by_side,
        }
        observed = max(
            positions_observed,
            pending_observed,
            maximum_observed,
            leverage_observed,
        )
        expires = min(
            positions_expires,
            pending_expires,
            maximum_expires,
            leverage_expires,
            observed + ttl,
        )
        if expires <= now:
            raise CanonicalExecutionChainBlocked(
                "BLOCKED_OKX_DEMO_DISPATCH_GUARD_FRESHNESS",
                "dispatch guard freshness window is empty",
            )
        return RedactedOkxDemoDispatchGuard(
            execution_target="OKX_DEMO",
            instrument=instrument,
            account_fingerprint_digest=self.__account_fingerprint_digest,
            credential_generation_digest=self.__credential_generation_digest,
            limit_price=format(price, "f"),
            effective_leverage=format(leverage, "f"),
            current_short_leverage=leverage_by_side["short"],
            minimum_size=format(required_size, "f"),
            maximum_buy_contracts=format(maximum_buy, "f"),
            long_contracts="0",
            short_contracts="0",
            active_position_count=active_count,
            pending_order_count=0,
            observed_at=observed,
            expires_at=expires,
            positions_digest=_resource_digest(
                resource="positions",
                observed_at=positions_observed,
                expires_at=positions_expires,
                authenticated=True,
                facts=safe_positions,
            ),
            positions_observed_at=positions_observed,
            positions_expires_at=positions_expires,
            pending_orders_digest=_resource_digest(
                resource="pending_orders",
                observed_at=pending_observed,
                expires_at=pending_expires,
                authenticated=True,
                facts=safe_pending,
            ),
            pending_orders_observed_at=pending_observed,
            pending_orders_expires_at=pending_expires,
            maximum_order_quantity_digest=_resource_digest(
                resource="maximum_order_quantity",
                observed_at=maximum_observed,
                expires_at=maximum_expires,
                authenticated=True,
                facts=safe_maximum,
            ),
            maximum_order_quantity_observed_at=maximum_observed,
            maximum_order_quantity_expires_at=maximum_expires,
            leverage_digest=_resource_digest(
                resource="leverage",
                observed_at=leverage_observed,
                expires_at=leverage_expires,
                authenticated=True,
                facts=safe_leverage,
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
    "RedactedOkxDemoDispatchGuard",
    "RedactedOkxDemoProbe",
    "create_canonical_okx_demo_session",
    "redacted_dispatch_guard_payload",
]
