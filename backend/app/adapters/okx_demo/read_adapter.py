from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from threading import RLock
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Union
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.adapters.okx_demo.attestation_proof import (
    ATTESTATION_PROOF_KEY_ENV,
    AttestationProofKeyUnavailable,
    require_attestation_proof_key,
)
from app.adapters.okx_demo.credentials import (
    OkxDemoCredentialProvider,
    OkxDemoCredentialsUnavailable,
    UnavailableOkxDemoCredentialProvider,
)
from app.adapters.okx_demo.credential_preflight import (
    ALLOW_REAL_FUNDS_ENV,
    EXECUTION_TARGET_ENV,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    OKX_DEMO_CREDENTIAL_ENV_NAMES,
    REST_URL_ENV,
    OkxDemoPreflightBlocked,
    _build_demo_authorization_headers,
    require_pinned_account_fingerprint,
    run_preflight,
    validate_account_config,
)
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.models import (
    AccountConfig,
    Balance,
    Candle,
    ExecutionAttestationBundle,
    FundingRate,
    InstrumentSpec,
    LeverageAdjustmentInfo,
    LeverageInfo,
    MaximumOrderQuantity,
    OkxReadSnapshot,
    OpenInterest,
    OrderBook,
    OrderBookLevel,
    OrderQuery,
    Position,
    ReferencePrice,
    SnapshotMetadata,
    Ticker,
    TradingFee,
    TrustedAccountSnapshotContent,
    TrustedBbo,
    TrustedClosedCandle,
    TrustedInstrumentSnapshotContent,
    TrustedMarketSnapshotContent,
    TrustedMarkPrice,
    TrustedPositionSummary,
    TrustedSignalBundle,
    TrustedSnapshotReference,
)
from app.adapters.okx_demo.transport import OkxReadTransport, UrllibOkxReadTransport


SWAP_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
INDEX_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
TRANSIENT_OKX_CODES = {"50001", "50004", "50011", "50013", "50026", "50061"}
UNAUTHORIZED_OKX_CODES = {"50110", "50111", "50113", "50114", "50119"}
DEFAULT_TTLS = {
    "instruments": 3600,
    "ticker": 10,
    "candles": 120,
    "orderbook": 5,
    "mark_price": 10,
    "index_price": 10,
    "funding_rate": 300,
    "open_interest": 60,
    "account_config": 60,
    "balance": 30,
    "positions": 15,
    "leverage": 60,
    "exchange_max_leverage": 60,
    "maximum_order_quantity": 15,
    "fees": 3600,
    "order": 15,
    "pending_orders": 15,
    "orders_history": 30,
    "fills_history": 30,
}
ATTESTATION_TTL_SECONDS = 60
ATTESTATION_RENEWAL_LEAD_SECONDS = 10
FUTURE_SKEW_SECONDS = 5
SIGNAL_TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1m": ("1m", 60),
    "3m": ("3m", 3 * 60),
    "5m": ("5m", 5 * 60),
    "15m": ("15m", 15 * 60),
    "30m": ("30m", 30 * 60),
    "1h": ("1H", 60 * 60),
    "2h": ("2H", 2 * 60 * 60),
    "4h": ("4H", 4 * 60 * 60),
    "6h": ("6H", 6 * 60 * 60),
    "12h": ("12H", 12 * 60 * 60),
    "1d": ("1D", 24 * 60 * 60),
    "2d": ("2D", 2 * 24 * 60 * 60),
    "3d": ("3D", 3 * 24 * 60 * 60),
    "1w": ("1W", 7 * 24 * 60 * 60),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _RecordedResponseTransport:
    """In-memory test seam; it cannot wrap or invoke a network transport."""

    def __init__(
        self,
        responses: Sequence[Union[OkxReadHttpResponse, BaseException]],
    ) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        *,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> OkxReadHttpResponse:
        self.calls.append(
            {
                "path": path,
                "query": dict(query),
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class OkxDemoReadClient(Protocol):
    def instruments(self, inst_id: Optional[str] = None) -> OkxReadSnapshot: ...
    def ticker(self, inst_id: str) -> OkxReadSnapshot: ...
    def candles(
        self, inst_id: str, *, bar: str = "1m", limit: int = 100
    ) -> OkxReadSnapshot: ...
    def orderbook(self, inst_id: str, *, depth: int = 20) -> OkxReadSnapshot: ...
    def mark_price(self, inst_id: str) -> OkxReadSnapshot: ...
    def index_price(self, index_id: str) -> OkxReadSnapshot: ...
    def funding_rate(self, inst_id: str) -> OkxReadSnapshot: ...
    def open_interest(self, inst_id: str) -> OkxReadSnapshot: ...
    def account_config(self) -> OkxReadSnapshot: ...
    def balance(self, currency: Optional[str] = None) -> OkxReadSnapshot: ...
    def positions(self, inst_id: Optional[str] = None) -> OkxReadSnapshot: ...
    def leverage(self, inst_id: str) -> OkxReadSnapshot: ...
    def exchange_max_leverage(self, inst_id: str) -> OkxReadSnapshot: ...
    def maximum_order_quantity(
        self,
        inst_id: str,
        *,
        td_mode: str,
        price: Decimal,
        leverage: Decimal,
    ) -> OkxReadSnapshot: ...
    def fees(self, inst_id: str) -> OkxReadSnapshot: ...
    def order(
        self,
        inst_id: str,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> OkxReadSnapshot: ...
    def pending_orders(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot: ...
    def fills_history(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        begin: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot: ...
    def orders_history(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot: ...
    def capture_trusted_signal_bundle(
        self,
        db: Session,
        *,
        inst_id: str,
        timeframe: str,
        candle_limit: int = 100,
    ) -> TrustedSignalBundle: ...
    def capture_execution_attestation(
        self,
        db: Session,
        *,
        inst_id: str,
    ) -> ExecutionAttestationBundle: ...


class OkxDemoReadAdapter:
    """Offline recorded-response normalizer with no injectable network transport."""

    def __init__(
        self,
        *,
        execution_target: str,
        recorded_responses: Sequence[
            Union[OkxReadHttpResponse, BaseException]
        ] = (),
        credential_provider: Optional[OkxDemoCredentialProvider] = None,
        timeout_seconds: float = 10.0,
        now_provider: Optional[Callable[[], datetime]] = None,
        ttl_seconds: Optional[Mapping[str, int]] = None,
    ) -> None:
        if execution_target != "OKX_DEMO":
            raise OkxReadAdapterError(
                kind="UNSAFE_TARGET",
                status="BLOCKED",
                message="OKX read adapter permits only execution_target=OKX_DEMO",
            )
        resolved_transport = _RecordedResponseTransport(recorded_responses)
        resolved_provider = (
            credential_provider or UnavailableOkxDemoCredentialProvider()
        )
        self._transport = resolved_transport
        self._credential_provider = resolved_provider
        self._account_config_validator = None
        self._timeout_seconds = timeout_seconds
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._ttls = {**DEFAULT_TTLS, **dict(ttl_seconds or {})}

    def instruments(self, inst_id: Optional[str] = None) -> OkxReadSnapshot:
        query = {"instType": "SWAP"}
        if inst_id is not None:
            query["instId"] = self._swap_id(inst_id)
        expected_inst_id = query.get("instId")
        return self._request(
            resource="instruments",
            path="/api/v5/public/instruments",
            query=query,
            authenticated=False,
            parser=lambda data: [
                self._instrument(
                    self._require_identity(item, "instId", expected_inst_id)
                )
                for item in data
            ],
        )

    def ticker(self, inst_id: str) -> OkxReadSnapshot:
        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="ticker",
            path="/api/v5/market/ticker",
            query={"instId": inst_id},
            authenticated=False,
            parser=lambda data: [
                self._ticker(self._require_identity(item, "instId", inst_id))
                for item in data
            ],
        )

    def candles(
        self,
        inst_id: str,
        *,
        bar: str = "1m",
        limit: int = 100,
    ) -> OkxReadSnapshot:
        interval = re.fullmatch(
            r"([1-9][0-9]*)(m|H|D|W)(?:utc)?",
            bar,
        )
        if interval is None:
            self._invalid_request("bar is not an allowed OKX candle interval")
        if not 1 <= limit <= 300:
            self._invalid_request("candle limit must be between 1 and 300")
        interval_seconds = int(interval.group(1)) * {
            "m": 60,
            "H": 60 * 60,
            "D": 24 * 60 * 60,
            "W": 7 * 24 * 60 * 60,
        }[interval.group(2)]
        return self._request(
            resource="candles",
            path="/api/v5/market/candles",
            query={"instId": self._swap_id(inst_id), "bar": bar, "limit": str(limit)},
            authenticated=False,
            parser=lambda data: [self._candle(item) for item in data],
            freshness_ttl_seconds=interval_seconds + self._ttls["candles"],
        )

    def orderbook(self, inst_id: str, *, depth: int = 20) -> OkxReadSnapshot:
        if not 1 <= depth <= 400:
            self._invalid_request("orderbook depth must be between 1 and 400")
        normalized_inst_id = self._swap_id(inst_id)
        return self._request(
            resource="orderbook",
            path="/api/v5/market/books",
            query={"instId": normalized_inst_id, "sz": str(depth)},
            authenticated=False,
            parser=lambda data: [
                self._orderbook(item, normalized_inst_id) for item in data
            ],
        )

    def mark_price(self, inst_id: str) -> OkxReadSnapshot:
        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="mark_price",
            path="/api/v5/public/mark-price",
            query={"instType": "SWAP", "instId": inst_id},
            authenticated=False,
            parser=lambda data: [
                self._reference_price(
                    self._require_identity(item, "instId", inst_id), "mark"
                )
                for item in data
            ],
        )

    def index_price(self, index_id: str) -> OkxReadSnapshot:
        if not INDEX_ID_PATTERN.fullmatch(index_id):
            self._invalid_request("index_id must use BASE-QUOTE format")
        return self._request(
            resource="index_price",
            path="/api/v5/market/index-tickers",
            query={"instId": index_id},
            authenticated=False,
            parser=lambda data: [
                self._reference_price(
                    self._require_identity(item, "instId", index_id), "index"
                )
                for item in data
            ],
        )

    def funding_rate(self, inst_id: str) -> OkxReadSnapshot:
        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="funding_rate",
            path="/api/v5/public/funding-rate",
            query={"instId": inst_id},
            authenticated=False,
            parser=lambda data: [
                self._funding_rate(
                    self._require_identity(item, "instId", inst_id)
                )
                for item in data
            ],
        )

    def open_interest(self, inst_id: str) -> OkxReadSnapshot:
        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="open_interest",
            path="/api/v5/public/open-interest",
            query={"instType": "SWAP", "instId": inst_id},
            authenticated=False,
            parser=lambda data: [
                self._open_interest(
                    self._require_identity(item, "instId", inst_id)
                )
                for item in data
            ],
        )

    def account_config(self) -> OkxReadSnapshot:
        return self._request(
            resource="account_config",
            path="/api/v5/account/config",
            query={},
            authenticated=True,
            parser=lambda data: [self._account_config(item) for item in data],
        )

    def balance(self, currency: Optional[str] = None) -> OkxReadSnapshot:
        query: dict[str, str] = {}
        if currency is not None:
            if not re.fullmatch(r"[A-Z0-9]{2,20}", currency):
                self._invalid_request("currency is invalid")
            query["ccy"] = currency
        return self._request(
            resource="balance",
            path="/api/v5/account/balance",
            query=query,
            authenticated=True,
            parser=self._balances,
            allow_empty=True,
        )

    def positions(self, inst_id: Optional[str] = None) -> OkxReadSnapshot:
        query = {"instType": "SWAP"}
        if inst_id is not None:
            query["instId"] = self._swap_id(inst_id)
        expected_inst_id = query.get("instId")
        return self._request(
            resource="positions",
            path="/api/v5/account/positions",
            query=query,
            authenticated=True,
            parser=lambda data: [
                self._position(
                    self._require_identity(item, "instId", expected_inst_id)
                )
                for item in data
            ],
            allow_empty=True,
        )

    def leverage(self, inst_id: str) -> OkxReadSnapshot:
        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="leverage",
            path="/api/v5/account/leverage-info",
            query={"instId": inst_id, "mgnMode": "isolated"},
            authenticated=True,
            parser=lambda data: [
                self._leverage(self._require_identity(item, "instId", inst_id))
                for item in data
            ],
        )

    def exchange_max_leverage(self, inst_id: str) -> OkxReadSnapshot:
        """Read the account-authoritative long-side maximum for a 14x request."""

        inst_id = self._swap_id(inst_id)
        return self._request(
            resource="exchange_max_leverage",
            path="/api/v5/account/adjust-leverage-info",
            query={
                "instType": "SWAP",
                "mgnMode": "isolated",
                "lever": "14",
                "posSide": "long",
                "instId": inst_id,
            },
            authenticated=True,
            parser=lambda data: self._exchange_max_leverage(data, inst_id=inst_id),
        )

    def maximum_order_quantity(
        self,
        inst_id: str,
        *,
        td_mode: str,
        price: Decimal,
        leverage: Decimal,
    ) -> OkxReadSnapshot:
        """Read the exact isolated long-side maximum size for the frozen limit."""

        inst_id = self._swap_id(inst_id)
        if td_mode != "isolated":
            self._invalid_request("td_mode must be isolated")
        for field, value in (("price", price), ("leverage", leverage)):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                self._invalid_request(f"{field} must be a positive Decimal")
        price_text = format(price, "f")
        leverage_text = format(leverage, "f")
        return self._request(
            resource="maximum_order_quantity",
            path="/api/v5/account/max-size",
            query={
                "instId": inst_id,
                "tdMode": td_mode,
                "px": price_text,
                "leverage": leverage_text,
            },
            authenticated=True,
            parser=lambda data: self._maximum_order_quantity(
                data,
                inst_id=inst_id,
                td_mode=td_mode,
                price=price,
                leverage=leverage,
            ),
        )

    def fees(self, inst_id: Optional[str] = None) -> OkxReadSnapshot:
        query = {"instType": "SWAP"}
        if inst_id is not None:
            query["instId"] = self._swap_id(inst_id)
        expected_inst_id = query.get("instId")
        return self._request(
            resource="fees",
            path="/api/v5/account/trade-fee",
            query=query,
            authenticated=True,
            parser=lambda data: [
                self._fee(self._require_identity(item, "instId", expected_inst_id))
                for item in data
            ],
        )

    def order(
        self,
        inst_id: str,
        *,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> OkxReadSnapshot:
        if bool(order_id) == bool(client_order_id):
            self._invalid_request("exactly one order_id or client_order_id is required")
        query = {"instId": self._swap_id(inst_id)}
        if order_id:
            if not order_id.isdigit():
                self._invalid_request("order_id must be numeric")
            query["ordId"] = order_id
        else:
            if not client_order_id or not re.fullmatch(
                r"[A-Za-z0-9]{1,32}", client_order_id
            ):
                self._invalid_request("client_order_id is invalid")
            query["clOrdId"] = client_order_id
        expected_order_id = query.get("ordId")
        expected_client_order_id = query.get("clOrdId")
        return self._request(
            resource="order",
            path="/api/v5/trade/order",
            query=query,
            authenticated=True,
            parser=lambda data: [
                self._order(
                    self._require_order_identity(
                        self._require_identity(item, "instId", query["instId"]),
                        order_id=expected_order_id,
                        client_order_id=expected_client_order_id,
                    )
                )
                for item in data
            ],
        )

    def pending_orders(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot:
        query = self._history_query(
            inst_id=inst_id,
            after=after,
            before=before,
            limit=limit,
        )
        expected_inst_id = query.get("instId")
        return self._request(
            resource="pending_orders",
            path="/api/v5/trade/orders-pending",
            query=query,
            authenticated=True,
            parser=lambda data: [
                self._order(
                    self._require_identity(item, "instId", expected_inst_id)
                )
                for item in data
            ],
            allow_empty=True,
        )

    def fills_history(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        begin: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot:
        query = self._history_query(
            inst_id=inst_id,
            after=after,
            before=before,
            limit=limit,
        )
        for field, value in (("begin", begin), ("end", end)):
            if value is not None and (
                not str(value).isdigit()
                or str(value) != str(int(str(value)))
            ):
                self._invalid_request(
                    "fills history {} must be canonical epoch milliseconds".format(
                        field
                    )
                )
            if value is not None:
                query[field] = str(value)
        if begin is not None and end is not None and int(begin) > int(end):
            self._invalid_request("fills history begin must not exceed end")
        return self._request(
            resource="fills_history",
            path="/api/v5/trade/fills-history",
            query=query,
            authenticated=True,
            parser=lambda data: [self._fill(item) for item in data],
            allow_empty=True,
        )

    def orders_history(
        self,
        inst_id: Optional[str] = None,
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> OkxReadSnapshot:
        query = self._history_query(
            inst_id=inst_id,
            after=after,
            before=before,
            limit=limit,
        )
        return self._request(
            resource="orders_history",
            path="/api/v5/trade/orders-history-archive",
            query=query,
            authenticated=True,
            parser=lambda data: [self._order(item) for item in data],
            allow_empty=True,
        )

    def _request(
        self,
        *,
        resource: str,
        path: str,
        query: Mapping[str, str],
        authenticated: bool,
        parser: Callable[[list[Any]], Sequence[BaseModel]],
        allow_empty: bool = False,
        freshness_ttl_seconds: Optional[int] = None,
    ) -> OkxReadSnapshot:
        headers = {
            "Accept": "application/json",
            "User-Agent": "freqtrade-ai-okx-demo-read/1",
        }
        if authenticated:
            encoded = urlencode(sorted(query.items()))
            request_path = path + (f"?{encoded}" if encoded else "")
            try:
                auth_headers = self._credential_provider.authorization_headers(
                    method="GET",
                    request_path=request_path,
                    body="",
                )
            except OkxDemoCredentialsUnavailable as exc:
                raise OkxReadAdapterError(
                    kind="UNAUTHORIZED",
                    status="BLOCKED",
                    message="OKX_DEMO read requires the #443 credential provider",
                ) from exc
            required = {
                "OK-ACCESS-KEY",
                "OK-ACCESS-SIGN",
                "OK-ACCESS-TIMESTAMP",
                "OK-ACCESS-PASSPHRASE",
            }
            if set(auth_headers) != required or any(
                not isinstance(value, str)
                or not value.strip()
                or re.search(r"[\x00-\x1f\x7f]", value)
                for value in auth_headers.values()
            ):
                raise OkxReadAdapterError(
                    kind="UNAUTHORIZED",
                    status="BLOCKED",
                    message=(
                        "OKX_DEMO credential provider returned invalid authorization headers"
                    ),
                )
            headers.update(auth_headers)
            headers["x-simulated-trading"] = "1"
        try:
            response = self._transport.get(
                path=path,
                query=query,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        except OkxReadAdapterError:
            raise
        except TimeoutError as exc:
            raise OkxReadAdapterError(
                kind="TIMEOUT",
                status="FAILED",
                message="OKX read request timed out",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise OkxReadAdapterError(
                kind="NETWORK",
                status="FAILED",
                message="OKX read request failed at the network boundary",
                retryable=True,
            ) from exc

        self._validate_http_status(response.status_code)
        if resource == "account_config" and self._account_config_validator is not None:
            try:
                self._account_config_validator(response.payload)
            except OkxDemoPreflightBlocked:
                raise OkxReadAdapterError(
                    kind="IDENTITY_DRIFT",
                    status="BLOCKED",
                    message="OKX Demo account identity no longer matches attestation",
                ) from None
        data = self._validate_envelope(response.payload, allow_empty=allow_empty)
        try:
            normalized = list(parser(data))
        except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError) as exc:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="FAILED",
                message=f"OKX {resource} response did not match schema",
            ) from exc
        if not normalized and not allow_empty:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="FAILED",
                message=f"OKX {resource} response contained no records",
            )

        now = self._now()
        received_at = self._aware_utc_timestamp(response.received_at)
        if resource == "funding_rate":
            self._validate_funding_schedule(normalized, now)
        exchange_timestamp = self._latest_timestamp(normalized)
        if (
            received_at > now + timedelta(seconds=FUTURE_SKEW_SECONDS)
            or (
                exchange_timestamp is not None
                and exchange_timestamp > now + timedelta(seconds=FUTURE_SKEW_SECONDS)
            )
        ):
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message=f"OKX {resource} snapshot timestamp is in the future",
            )
        # Historical records retain their exchange timestamps for ordering and
        # reconciliation watermarks, but those timestamps do not describe the
        # freshness of a just-authenticated archive response.  Treat the HTTP
        # receipt as the snapshot freshness evidence for archive streams.
        freshness_anchor = (
            received_at
            if resource in {"orders_history", "fills_history"}
            else exchange_timestamp or received_at
        )
        expires_at = freshness_anchor + timedelta(
            seconds=(
                freshness_ttl_seconds
                if freshness_ttl_seconds is not None
                else self._ttls[resource]
            )
        )
        stale = self._now() > expires_at
        if stale:
            raise OkxReadAdapterError(
                kind="STALE_DATA",
                status="BLOCKED",
                message=f"OKX {resource} snapshot is expired",
            )
        return OkxReadSnapshot(
            metadata=SnapshotMetadata(
                resource=resource,
                fetched_at=received_at,
                exchange_timestamp=exchange_timestamp,
                expires_at=expires_at,
                stale=False,
                authenticated=authenticated,
            ),
            items=[item.model_dump(mode="json") for item in normalized],
        )

    @staticmethod
    def _validate_http_status(status_code: int) -> None:
        if 200 <= status_code <= 299:
            return
        if status_code in {401, 403}:
            raise OkxReadAdapterError(
                kind="UNAUTHORIZED",
                status="BLOCKED",
                message="OKX rejected Demo read authorization",
                http_status=status_code,
            )
        if status_code == 429:
            raise OkxReadAdapterError(
                kind="RATE_LIMITED",
                status="FAILED",
                message="OKX read request was rate limited",
                retryable=True,
                http_status=status_code,
            )
        raise OkxReadAdapterError(
            kind="HTTP_ERROR",
            status="FAILED",
            message=f"OKX read request returned HTTP {status_code}",
            retryable=500 <= status_code <= 599,
            http_status=status_code,
        )

    @staticmethod
    def _validate_envelope(payload: object, *, allow_empty: bool) -> list[Any]:
        if not isinstance(payload, dict):
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="FAILED",
                message="OKX read response must be a JSON object",
            )
        code = str(payload.get("code", ""))
        if code != "0":
            if code in UNAUTHORIZED_OKX_CODES:
                raise OkxReadAdapterError(
                    kind="UNAUTHORIZED",
                    status="BLOCKED",
                    message="OKX rejected Demo read authorization",
                    okx_code=code,
                )
            if code in TRANSIENT_OKX_CODES:
                raise OkxReadAdapterError(
                    kind="RATE_LIMITED",
                    status="FAILED",
                    message="OKX returned a transient read business error",
                    retryable=True,
                    okx_code=code,
                )
            raise OkxReadAdapterError(
                kind="BUSINESS_ERROR",
                status="FAILED",
                message="OKX returned a read business error",
                okx_code=code or None,
            )
        data = payload.get("data")
        if not isinstance(data, list) or (not data and not allow_empty):
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="FAILED",
                message="OKX read response data must be a non-empty array",
            )
        return data

    @staticmethod
    def _instrument(item: Mapping[str, Any]) -> InstrumentSpec:
        if item["instType"] != "SWAP":
            raise ValueError("instrument is not SWAP")
        inst_id = item["instId"]
        if not isinstance(inst_id, str) or not SWAP_ID_PATTERN.fullmatch(inst_id):
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message="OKX instrument identity is invalid",
            )
        base_ccy, quote_ccy, _suffix = inst_id.split("-")
        family = "{}-{}".format(base_ccy, quote_ccy)
        if item.get("uly") != family or item.get("instFamily") != family:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message="OKX instrument family does not match instId",
            )
        reported_base = item.get("baseCcy") or ""
        reported_quote = item.get("quoteCcy") or ""
        if (
            reported_base not in ("", base_ccy)
            or reported_quote not in ("", quote_ccy)
        ):
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message="OKX instrument currencies do not match instId",
            )
        contract_type = item.get("ctType")
        settle_ccy = item["settleCcy"]
        contract_value_ccy = item["ctValCcy"]
        consistent = (
            contract_type == "linear"
            and contract_value_ccy == base_ccy
            and settle_ccy == quote_ccy
        ) or (
            contract_type == "inverse"
            and contract_value_ccy == quote_ccy
            and settle_ccy == base_ccy
        )
        if not consistent:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message="OKX instrument contract metadata is inconsistent",
            )
        return InstrumentSpec(
            inst_id=inst_id,
            inst_type=item["instType"],
            base_ccy=base_ccy,
            quote_ccy=quote_ccy,
            settle_ccy=settle_ccy,
            contract_type=contract_type,
            contract_value=Decimal(item["ctVal"]),
            contract_value_ccy=contract_value_ccy,
            lot_size=Decimal(item["lotSz"]),
            min_size=Decimal(item["minSz"]),
            tick_size=Decimal(item["tickSz"]),
            state=item["state"],
            listed_at=_optional_millis(item.get("listTime")),
        )

    @staticmethod
    def _ticker(item: Mapping[str, Any]) -> Ticker:
        return Ticker(
            inst_id=item["instId"],
            last=Decimal(item["last"]),
            bid=Decimal(item["bidPx"]),
            ask=Decimal(item["askPx"]),
            open_24h=Decimal(item["open24h"]),
            high_24h=Decimal(item["high24h"]),
            low_24h=Decimal(item["low24h"]),
            volume_24h=Decimal(item["vol24h"]),
            volume_ccy_24h=Decimal(item["volCcy24h"]),
            timestamp=_millis(item["ts"]),
        )

    @staticmethod
    def _candle(item: Any) -> Candle:
        if not isinstance(item, list) or len(item) < 9:
            raise ValueError("candle must contain at least 9 fields")
        return Candle(
            timestamp=_millis(item[0]),
            open=Decimal(item[1]),
            high=Decimal(item[2]),
            low=Decimal(item[3]),
            close=Decimal(item[4]),
            volume=Decimal(item[5]),
            volume_ccy=Decimal(item[7]),
            confirmed=str(item[8]) == "1",
        )

    @staticmethod
    def _orderbook(item: Mapping[str, Any], inst_id: str) -> OrderBook:
        def levels(raw_levels: Any) -> list[OrderBookLevel]:
            if not isinstance(raw_levels, list):
                raise ValueError("orderbook side must be an array")
            return [
                OrderBookLevel(
                    price=Decimal(level[0]),
                    size=Decimal(level[1]),
                    orders=int(level[3]),
                )
                for level in raw_levels
            ]

        return OrderBook(
            inst_id=inst_id,
            bids=levels(item["bids"]),
            asks=levels(item["asks"]),
            timestamp=_millis(item["ts"]),
        )

    @staticmethod
    def _reference_price(item: Mapping[str, Any], kind: str) -> ReferencePrice:
        return ReferencePrice(
            inst_id=item["instId"],
            price_kind=kind,
            price=Decimal(item["markPx"] if kind == "mark" else item["idxPx"]),
            timestamp=_millis(item["ts"]),
        )

    @staticmethod
    def _funding_rate(item: Mapping[str, Any]) -> FundingRate:
        return FundingRate(
            inst_id=item["instId"],
            funding_rate=Decimal(item["fundingRate"]),
            next_funding_rate=_optional_decimal(item.get("nextFundingRate")),
            funding_time=_millis(item["fundingTime"]),
            next_funding_time=_optional_millis(item.get("nextFundingTime")),
        )

    @staticmethod
    def _validate_funding_schedule(
        items: Sequence[BaseModel],
        now: datetime,
    ) -> None:
        lower = now - timedelta(hours=24)
        upper = now + timedelta(hours=24)
        for item in items:
            if not isinstance(item, FundingRate):
                continue
            if not lower <= item.funding_time <= upper:
                raise OkxReadAdapterError(
                    kind="INVALID_RESPONSE",
                    status="BLOCKED",
                    message="OKX funding schedule is outside the allowed window",
                )
            if item.next_funding_time is not None and (
                not lower <= item.next_funding_time <= upper
                or item.next_funding_time < item.funding_time
            ):
                raise OkxReadAdapterError(
                    kind="INVALID_RESPONSE",
                    status="BLOCKED",
                    message="OKX next funding schedule is inconsistent",
                )

    @staticmethod
    def _open_interest(item: Mapping[str, Any]) -> OpenInterest:
        return OpenInterest(
            inst_id=item["instId"],
            open_interest_contracts=Decimal(item["oi"]),
            open_interest_ccy=_optional_decimal(item.get("oiCcy")),
            timestamp=_millis(item["ts"]),
        )

    @staticmethod
    def _account_config(item: Mapping[str, Any]) -> AccountConfig:
        return AccountConfig(
            account_level=str(item["acctLv"]),
            position_mode=item["posMode"],
            auto_loan=str(item.get("autoLoan", "false")).lower() == "true",
            greeks_type=item.get("greeksType", ""),
        )

    @staticmethod
    def _balances(data: list[Any]) -> list[Balance]:
        balances: list[Balance] = []
        for account in data:
            timestamp = _millis(account["uTime"])
            total_equity = _optional_decimal(account.get("totalEq"))
            details = account.get("details", [])
            if not isinstance(details, list):
                raise ValueError("balance details must be an array")
            for detail in details:
                balances.append(
                    Balance(
                        currency=detail["ccy"],
                        total_equity=total_equity,
                        available_balance=_optional_decimal(detail.get("availBal")),
                        cash_balance=_optional_decimal(detail.get("cashBal")),
                        frozen_balance=_optional_decimal(detail.get("frozenBal")),
                        equity=_optional_decimal(detail.get("eq")),
                        unrealized_pnl=_optional_decimal(detail.get("upl")),
                        timestamp=timestamp,
                    )
                )
        return balances

    @staticmethod
    def _position(item: Mapping[str, Any]) -> Position:
        position_side = OkxDemoReadAdapter._long_short_position_side(item)
        return Position(
            inst_id=item["instId"],
            margin_mode=item["mgnMode"],
            position_side=position_side,
            contracts=Decimal(item["pos"]),
            available_contracts=Decimal(item.get("availPos") or "0"),
            average_price=_optional_decimal(item.get("avgPx")),
            mark_price=_optional_decimal(item.get("markPx")),
            liquidation_price=_optional_decimal(item.get("liqPx")),
            leverage=_optional_decimal(item.get("lever")),
            margin_ratio=_optional_decimal(item.get("mgnRatio")),
            unrealized_pnl=_optional_decimal(item.get("upl")),
            timestamp=_millis(item["uTime"]),
        )

    @staticmethod
    def _leverage(item: Mapping[str, Any]) -> LeverageInfo:
        position_side = OkxDemoReadAdapter._long_short_position_side(item)
        return LeverageInfo(
            inst_id=item["instId"],
            margin_mode=item["mgnMode"],
            position_side=position_side,
            leverage=Decimal(item["lever"]),
        )

    @staticmethod
    def _exchange_max_leverage(
        data: list[Any], *, inst_id: str
    ) -> list[LeverageAdjustmentInfo]:
        if len(data) != 1 or not isinstance(data[0], Mapping):
            raise ValueError("adjust-leverage-info response must be singular")
        item = data[0]
        expected_identity = {
            "instId": inst_id,
            "instType": "SWAP",
            "mgnMode": "isolated",
            "lever": "14",
            "posSide": "long",
        }
        for field, expected in expected_identity.items():
            if field in item and item[field] != expected:
                raise ValueError(f"adjust-leverage-info {field} identity drifted")
        if not isinstance(item.get("existOrd"), bool):
            raise ValueError("adjust-leverage-info existOrd must be boolean")
        return [
            LeverageAdjustmentInfo(
                inst_id=inst_id,
                inst_type="SWAP",
                margin_mode="isolated",
                position_side="long",
                requested_leverage=Decimal("14"),
                max_leverage=Decimal(item["maxLever"]),
                min_leverage=Decimal(item["minLever"]),
                has_pending_orders=item["existOrd"],
            )
        ]

    @staticmethod
    def _maximum_order_quantity(
        data: list[Any],
        *,
        inst_id: str,
        td_mode: str,
        price: Decimal,
        leverage: Decimal,
    ) -> list[MaximumOrderQuantity]:
        if len(data) != 1 or not isinstance(data[0], Mapping):
            raise ValueError("max-size response must be singular")
        item = data[0]
        if item.get("instId") != inst_id:
            raise ValueError("max-size instId identity drifted")
        max_buy = Decimal(str(item.get("maxBuy")))
        if not max_buy.is_finite() or max_buy < 0:
            raise ValueError("max-size maxBuy must be finite and nonnegative")
        return [
            MaximumOrderQuantity(
                inst_id=inst_id,
                margin_mode=td_mode,
                price=price,
                leverage=leverage,
                max_buy=max_buy,
            )
        ]

    @staticmethod
    def _long_short_position_side(item: Mapping[str, Any]) -> str:
        """Reject net-mode snapshots before they enter the Demo execution path."""

        position_side = item.get("posSide")
        if position_side not in ("long", "short"):
            raise ValueError("OKX long_short_mode requires posSide=long or short")
        return position_side

    @staticmethod
    def _fee(item: Mapping[str, Any]) -> TradingFee:
        return TradingFee(
            inst_type="SWAP",
            inst_id=item.get("instId") or None,
            maker_rate=Decimal(item["maker"]),
            taker_rate=Decimal(item["taker"]),
        )

    @staticmethod
    def _order(item: Mapping[str, Any]) -> OrderQuery:
        position_side = OkxDemoReadAdapter._long_short_position_side(item)
        return OrderQuery(
            inst_id=item["instId"],
            order_id=item["ordId"],
            client_order_id=item.get("clOrdId") or None,
            state=item["state"],
            side=item["side"],
            position_side=position_side,
            margin_mode=item.get("tdMode") or None,
            order_type=item["ordType"],
            reduce_only=_optional_boolean(item.get("reduceOnly")),
            price=_optional_decimal(item.get("px")),
            size=Decimal(item["sz"]),
            accumulated_fill_size=Decimal(item.get("accFillSz") or "0"),
            average_price=_optional_decimal(item.get("avgPx")),
            fee=_optional_decimal(item.get("fee")),
            fee_currency=item.get("feeCcy") or None,
            created_at=_millis(item["cTime"]),
            updated_at=_millis(item["uTime"]),
        )

    @staticmethod
    def _fill(item: Mapping[str, Any]):
        from app.adapters.okx_demo.models import FillQuery

        trade_id = item.get("tradeId")
        legacy_fill_id = item.get("fillId")
        if trade_id and legacy_fill_id and trade_id != legacy_fill_id:
            raise ValueError("fill identity fields conflict")
        fill_id = trade_id or legacy_fill_id
        if not fill_id:
            raise ValueError("fill identity is missing")
        bill_id = item.get("billId")
        if (
            not bill_id
            or not str(bill_id).isdigit()
            or str(bill_id) != str(int(str(bill_id)))
        ):
            raise ValueError("fill billId is not a canonical numeric cursor")
        return FillQuery(
            fill_id=fill_id,
            bill_id=str(bill_id),
            order_id=item["ordId"],
            inst_id=item["instId"],
            price=Decimal(item["fillPx"]),
            size=Decimal(item["fillSz"]),
            fee=_optional_decimal(item.get("fee")),
            timestamp=_millis(item["ts"]),
        )

    @classmethod
    def _history_query(
        cls,
        *,
        inst_id: Optional[str],
        after: Optional[str],
        before: Optional[str],
        limit: int,
    ) -> dict[str, str]:
        if limit < 1 or limit > 100:
            cls._invalid_request("history limit must be between 1 and 100")
        if after is not None and not str(after).isdigit():
            cls._invalid_request("history after cursor must be numeric")
        if before is not None and not str(before).isdigit():
            cls._invalid_request("history before cursor must be numeric")
        query = {"instType": "SWAP", "limit": str(limit)}
        if inst_id is not None:
            query["instId"] = cls._swap_id(inst_id)
        if after is not None:
            query["after"] = str(after)
        if before is not None:
            query["before"] = str(before)
        return query

    @staticmethod
    def _latest_timestamp(items: Sequence[BaseModel]) -> Optional[datetime]:
        timestamps: list[datetime] = []
        for item in items:
            for name in ("timestamp", "updated_at"):
                value = getattr(item, name, None)
                if isinstance(value, datetime):
                    timestamps.append(value)
        return max(timestamps) if timestamps else None

    @staticmethod
    def _require_identity(
        item: Any,
        field: str,
        expected: Optional[str],
    ) -> Mapping[str, Any]:
        if not isinstance(item, Mapping):
            raise ValueError("OKX item must be an object")
        if expected is not None and item.get(field) != expected:
            raise ValueError(f"OKX response {field} does not match the request")
        return item

    @staticmethod
    def _require_order_identity(
        item: Mapping[str, Any],
        *,
        order_id: Optional[str],
        client_order_id: Optional[str],
    ) -> Mapping[str, Any]:
        if order_id is not None and item.get("ordId") != order_id:
            raise ValueError("OKX response ordId does not match the request")
        if client_order_id is not None and item.get("clOrdId") != client_order_id:
            raise ValueError("OKX response clOrdId does not match the request")
        return item

    @staticmethod
    def _swap_id(inst_id: str) -> str:
        if not SWAP_ID_PATTERN.fullmatch(inst_id):
            raise OkxReadAdapterError(
                kind="INVALID_REQUEST",
                status="BLOCKED",
                message="inst_id must be an uppercase OKX SWAP instrument",
            )
        return inst_id

    @staticmethod
    def _invalid_request(message: str) -> None:
        raise OkxReadAdapterError(
            kind="INVALID_REQUEST",
            status="BLOCKED",
            message=message,
        )

    def _now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise OkxReadAdapterError(
                kind="INVALID_CLOCK",
                status="BLOCKED",
                message="OKX read clock must be timezone-aware",
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _aware_utc_timestamp(value: Any) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise OkxReadAdapterError(
                kind="INVALID_RESPONSE",
                status="BLOCKED",
                message="OKX response received_at must be timezone-aware",
            )
        return value.astimezone(timezone.utc)


class _AttestedWriterCredentialHandle:
    """Sealed wrapper emitted only with a successful production attestation."""

    def __init__(self, _provider: object) -> None:
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO writer credential handle requires production attestation"
        )

    @classmethod
    def _from_attested_session(
        cls,
        provider: OkxDemoCredentialProvider,
    ) -> "_AttestedWriterCredentialHandle":
        handle = object.__new__(cls)
        handle.__provider = provider
        handle.__revoked = False
        return handle

    def active(self) -> bool:
        return not self.__revoked

    def bind_database(self, db: Session) -> None:
        bind = db.get_bind()
        if not isinstance(bind, Connection):
            raise OkxDemoCredentialsUnavailable(
                "OKX_DEMO durable revoke requires a pinned database connection"
            )
        independent_binding = Session(bind=bind.engine)
        try:
            self.__provider.bind_database(independent_binding)
        finally:
            independent_binding.close()

    def revoke(self, reason: str) -> None:
        self.__revoked = True
        self.__provider.revoke(reason)

    def authorization_headers(
        self,
        *,
        method: str,
        request_path: str,
        body: str,
    ) -> Mapping[str, str]:
        if self.__revoked:
            raise OkxDemoCredentialsUnavailable(
                "OKX_DEMO attested credential session is revoked"
            )
        return self.__provider.authorization_headers(
            method=method,
            request_path=request_path,
            body=body,
        )


def create_attested_okx_demo_read_adapter(
    environment: Mapping[str, str],
) -> OkxDemoReadClient:
    """Create the only production adapter after attesting one frozen bundle."""

    names = (
        EXECUTION_TARGET_ENV,
        ALLOW_REAL_FUNDS_ENV,
        REST_URL_ENV,
        *OKX_DEMO_CREDENTIAL_ENV_NAMES,
        OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
        ATTESTATION_PROOF_KEY_ENV,
    )
    snapshot = {name: environment.get(name, "") for name in names}
    try:
        run_preflight(snapshot)
        expected_fingerprint = require_pinned_account_fingerprint(snapshot)
        attested_at = _utc_now()
        if attested_at.tzinfo is None:
            raise OkxDemoPreflightBlocked("attestation clock is not timezone-aware")
        attested_at = attested_at.astimezone(timezone.utc)
        expires_at = attested_at + timedelta(seconds=ATTESTATION_TTL_SECONDS)
        attestation_hmac_key = require_attestation_proof_key(snapshot)
        from app.services.risk_chain import (
            _issue_attested_session_capability,
            _normalize_attested_snapshot,
            _persist_attested_session,
            _revoke_attested_session,
            _revoke_attested_session_capability,
            _write_attested_snapshot,
        )

        risk_capability = _issue_attested_session_capability(
            attestation_hmac_key=attestation_hmac_key,
            pinned_fingerprint_sha256=expected_fingerprint,
            created_at=attested_at,
            expires_at=expires_at,
        )

        class AttestedSession:
            def __init__(self) -> None:
                self.attested_at = attested_at
                self.expires_at = expires_at
                self._environment = {
                    name: value
                    for name, value in snapshot.items()
                    if name != ATTESTATION_PROOF_KEY_ENV
                }
                self._revoked = False
                self._revoke_session_factory = None
                self._durability_failed = False
                self._risk_capability = risk_capability
                self._renewal_lock = RLock()

            def _renew_if_needed(self, now: datetime) -> None:
                if (
                    now
                    < self.expires_at
                    - timedelta(seconds=ATTESTATION_RENEWAL_LEAD_SECONDS)
                ):
                    return
                with self._renewal_lock:
                    if (
                        now
                        < self.expires_at
                        - timedelta(
                            seconds=ATTESTATION_RENEWAL_LEAD_SECONDS
                        )
                    ):
                        return
                    old_capability = self._risk_capability
                    new_expires_at = now + timedelta(
                        seconds=ATTESTATION_TTL_SECONDS
                    )
                    new_capability = _issue_attested_session_capability(
                        attestation_hmac_key=attestation_hmac_key,
                        pinned_fingerprint_sha256=expected_fingerprint,
                        created_at=now,
                        expires_at=new_expires_at,
                    )
                    try:
                        if self._revoke_session_factory is not None:
                            renewal_db = self._revoke_session_factory()
                            try:
                                _persist_attested_session(
                                    renewal_db,
                                    new_capability,
                                    now=now,
                                )
                                _revoke_attested_session(
                                    renewal_db,
                                    old_capability,
                                    reason="EXPIRED",
                                    revoked_at=now,
                                )
                                renewal_db.commit()
                            except BaseException:
                                renewal_db.rollback()
                                raise
                            finally:
                                renewal_db.close()
                    except BaseException:
                        self._durability_failed = True
                        self._environment.clear()
                        _revoke_attested_session_capability(
                            old_capability
                        )
                        _revoke_attested_session_capability(
                            new_capability
                        )
                        raise OkxDemoCredentialsUnavailable(
                            "OKX_DEMO attestation renewal failed"
                        ) from None
                    self._risk_capability = new_capability
                    self.attested_at = now
                    self.expires_at = new_expires_at
                    _revoke_attested_session_capability(old_capability)

            def bind_database(self, db) -> None:
                if self._revoke_session_factory is None:
                    from sqlalchemy.orm import sessionmaker

                    # Startup reconciliation can consume most of the short
                    # attestation window before the sole writer is bound.
                    # Reuse the normal renewal boundary before the first
                    # durable bind so an expired capability is never written.
                    self._renew_if_needed(_utc_now())
                    revoke_session_factory = sessionmaker(
                        bind=db.get_bind(),
                        expire_on_commit=False,
                    )
                    bind_db = revoke_session_factory()
                    try:
                        _persist_attested_session(
                            bind_db,
                            self._risk_capability,
                            now=_utc_now(),
                        )
                        bind_db.commit()
                    except BaseException:
                        bind_db.rollback()
                        raise
                    finally:
                        bind_db.close()
                    self._revoke_session_factory = revoke_session_factory

            def revoke(self, reason: str, *, db=None) -> None:
                if self._durability_failed:
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO attestation durable revoke is unavailable"
                    )
                if self._revoked:
                    return
                try:
                    if db is not None:
                        _revoke_attested_session(
                            db,
                            self._risk_capability,
                            reason=reason,
                            revoked_at=_utc_now(),
                        )
                    elif self._revoke_session_factory is not None:
                        revoke_db = self._revoke_session_factory()
                        try:
                            _revoke_attested_session(
                                revoke_db,
                                self._risk_capability,
                                reason=reason,
                                revoked_at=_utc_now(),
                            )
                            revoke_db.commit()
                        except BaseException:
                            revoke_db.rollback()
                            raise
                        finally:
                            revoke_db.close()
                except BaseException:
                    self._durability_failed = True
                    self._environment.clear()
                    _revoke_attested_session_capability(
                        self._risk_capability
                    )
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO attestation durable revoke failed"
                    ) from None
                self._revoked = True
                self._environment.clear()
                _revoke_attested_session_capability(
                    self._risk_capability
                )

            def authorization_headers(
                self,
                *,
                method: str,
                request_path: str,
                body: str,
            ) -> Mapping[str, str]:
                now = _utc_now()
                if self._revoked or not isinstance(now, datetime):
                    self.revoke("EXPIRED")
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO account attestation expired or revoked"
                    )
                if now.tzinfo is None:
                    self.revoke("EXPIRED")
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO account attestation expired or revoked"
                    )
                self._renew_if_needed(now.astimezone(timezone.utc))
                try:
                    return _build_demo_authorization_headers(
                        self._environment,
                        method=method,
                        request_path=request_path,
                        body=body,
                    )
                except OkxDemoPreflightBlocked:
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO credential provider is unavailable"
                    ) from None

        class ProductionReadClient:
            def __init__(self) -> None:
                session = AttestedSession()

                def validate_current_account(payload: Any) -> None:
                    try:
                        validate_account_config(
                            payload,
                            expected_fingerprint=expected_fingerprint,
                        )
                    except OkxDemoPreflightBlocked:
                        session.revoke("IDENTITY_DRIFT")
                        raise

                engine = object.__new__(OkxDemoReadAdapter)
                engine._transport = UrllibOkxReadTransport()
                engine._credential_provider = session
                engine._timeout_seconds = 10.0
                engine._now_provider = _utc_now
                engine._ttls = dict(DEFAULT_TTLS)
                engine._account_config_validator = validate_current_account
                self._engine = engine
                self._attested_session = session
                self._writer_credential_handle = (
                    _AttestedWriterCredentialHandle._from_attested_session(
                        session
                    )
                )

            def _persist_risk_snapshot(
                self,
                db,
                *,
                kind,
                content,
                observed_at,
                snapshot_expires_at,
            ):
                self._attested_session.bind_database(db)
                snapshot_now = _utc_now()
                if snapshot_now.tzinfo is None:
                    raise OkxDemoCredentialsUnavailable(
                        "OKX_DEMO attestation clock is invalid"
                    )
                self._attested_session._renew_if_needed(
                    snapshot_now.astimezone(timezone.utc)
                )
                try:
                    normalized = _normalize_attested_snapshot(
                        self._attested_session._risk_capability,
                        kind=kind,
                        content=content,
                        observed_at=observed_at,
                        expires_at=snapshot_expires_at,
                    )
                    return _write_attested_snapshot(
                        db,
                        self._attested_session._risk_capability,
                        normalized,
                        now=snapshot_now,
                    )
                except BaseException:
                    try:
                        self._attested_session.revoke("WRITE_FAILURE", db=db)
                    except OkxDemoCredentialsUnavailable:
                        pass
                    raise

            def capture_trusted_signal_bundle(
                self,
                db: Session,
                *,
                inst_id: str,
                timeframe: str,
                candle_limit: int = 100,
            ) -> TrustedSignalBundle:
                """Capture one typed, atomic evidence bundle for signal and risk."""

                from app.core.config import get_settings
                from app.services.risk_chain import canonical_digest

                allowed_instruments = set(
                    get_settings()
                    .demo_automation_policy.demo_risk_policy.allowed_instruments
                )
                if inst_id not in allowed_instruments:
                    self._block_signal_bundle(
                        "instrument is not allowlisted by the OKX Demo risk policy"
                    )
                timeframe_mapping = SIGNAL_TIMEFRAMES.get(timeframe)
                if timeframe_mapping is None:
                    self._block_signal_bundle(
                        "timeframe has no explicit OKX closed-candle mapping"
                    )
                if not 2 <= candle_limit <= 299:
                    self._block_signal_bundle(
                        "signal bundle requires between 2 and 299 closed candles"
                    )
                okx_bar, timeframe_seconds = timeframe_mapping

                instrument_snapshot = self._engine.instruments(inst_id)
                candle_snapshot = self._engine.candles(
                    inst_id,
                    bar=okx_bar,
                    limit=candle_limit + 1,
                )
                book_snapshot = self._engine.orderbook(inst_id, depth=1)
                mark_snapshot = self._engine.mark_price(inst_id)
                config_snapshot = self._engine.account_config()
                positions_snapshot = self._engine.positions(inst_id)
                leverage_snapshot = self._engine.leverage(inst_id)

                if len(instrument_snapshot.items) != 1:
                    self._block_signal_bundle(
                        "signal bundle requires exactly one instrument"
                    )
                instrument = InstrumentSpec.model_validate(
                    instrument_snapshot.items[0]
                )
                if instrument.inst_id != inst_id or instrument.state != "live":
                    self._block_signal_bundle(
                        "instrument is not an active OKX Demo SWAP"
                    )

                received_candles = [
                    Candle.model_validate(item)
                    for item in candle_snapshot.items
                ]
                if len(received_candles) != candle_limit + 1:
                    self._block_signal_bundle(
                        "closed-candle response is incomplete"
                    )
                expected_delta = timedelta(seconds=timeframe_seconds)
                raw_timestamps = [item.timestamp for item in received_candles]
                if any(
                    newer <= older
                    for newer, older in zip(
                        raw_timestamps,
                        raw_timestamps[1:],
                    )
                ):
                    self._block_signal_bundle(
                        "closed candles are duplicated or out of newest-first order"
                    )
                if any(
                    newer.timestamp - older.timestamp != expected_delta
                    for newer, older in zip(
                        received_candles,
                        received_candles[1:],
                    )
                ):
                    self._block_signal_bundle(
                        "received candles are not continuous for the timeframe"
                    )
                unconfirmed_indexes = [
                    index
                    for index, candle in enumerate(received_candles)
                    if not candle.confirmed
                ]
                if unconfirmed_indexes not in ([], [0]):
                    self._block_signal_bundle(
                        "only the newest candle may be unconfirmed"
                    )
                raw_candles = (
                    received_candles[1:]
                    if unconfirmed_indexes == [0]
                    else received_candles[:candle_limit]
                )
                if (
                    len(raw_candles) != candle_limit
                    or any(not candle.confirmed for candle in raw_candles)
                ):
                    self._block_signal_bundle(
                        "closed-candle response does not contain the exact confirmed window"
                    )
                candles = list(reversed(raw_candles))
                if any(
                    current.timestamp - previous.timestamp != expected_delta
                    for previous, current in zip(candles, candles[1:])
                ):
                    self._block_signal_bundle(
                        "closed candles are not continuous for the timeframe"
                    )
                typed_candles = tuple(
                    TrustedClosedCandle.model_validate(
                        candle.model_dump()
                    )
                    for candle in candles
                )
                serialized_candles = [
                    candle.model_dump(mode="json")
                    for candle in typed_candles
                ]
                candle_set_digest = canonical_digest(
                    {
                        "instrument_id": inst_id,
                        "timeframe": timeframe,
                        "candles": serialized_candles,
                    }
                )

                if len(book_snapshot.items) != 1:
                    self._block_signal_bundle(
                        "signal bundle requires exactly one order book"
                    )
                order_book = OrderBook.model_validate(
                    book_snapshot.items[0]
                )
                if (
                    order_book.inst_id != inst_id
                    or not order_book.bids
                    or not order_book.asks
                ):
                    self._block_signal_bundle(
                        "signal bundle order book has no complete BBO"
                    )
                best_bid = max(order_book.bids, key=lambda item: item.price)
                best_ask = min(order_book.asks, key=lambda item: item.price)
                if best_bid.price >= best_ask.price:
                    self._block_signal_bundle(
                        "signal bundle BBO is crossed or locked"
                    )
                bbo = TrustedBbo(
                    bid_price=best_bid.price,
                    bid_size=best_bid.size,
                    ask_price=best_ask.price,
                    ask_size=best_ask.size,
                    timestamp=order_book.timestamp,
                )

                if len(mark_snapshot.items) != 1:
                    self._block_signal_bundle(
                        "signal bundle requires exactly one mark price"
                    )
                mark_price = ReferencePrice.model_validate(
                    mark_snapshot.items[0]
                )
                if (
                    mark_price.inst_id != inst_id
                    or mark_price.price_kind != "mark"
                    or mark_price.price <= 0
                ):
                    self._block_signal_bundle(
                        "signal bundle mark price is invalid"
                    )
                mark = TrustedMarkPrice(
                    price=mark_price.price,
                    timestamp=mark_price.timestamp,
                )

                if len(config_snapshot.items) != 1:
                    self._block_signal_bundle(
                        "signal bundle requires exactly one account config"
                    )
                account_config = AccountConfig.model_validate(
                    config_snapshot.items[0]
                )
                if account_config.position_mode != "long_short_mode":
                    self._block_signal_bundle(
                        "OKX Demo account is not in long_short_mode"
                    )

                positions = [
                    Position.model_validate(item)
                    for item in positions_snapshot.items
                ]
                if any(
                    item.inst_id != inst_id
                    or item.margin_mode != "isolated"
                    or item.position_side not in {"long", "short"}
                    or item.contracts < 0
                    for item in positions
                ):
                    self._block_signal_bundle(
                        "account positions are not isolated long/short evidence"
                    )
                leverages = [
                    LeverageInfo.model_validate(item)
                    for item in leverage_snapshot.items
                ]
                leverage_by_side: dict[str, Decimal] = {}
                for item in leverages:
                    if (
                        item.inst_id != inst_id
                        or item.margin_mode != "isolated"
                        or item.position_side not in {"long", "short"}
                        or item.leverage <= 0
                        or item.position_side in leverage_by_side
                    ):
                        self._block_signal_bundle(
                            "account leverage evidence is invalid or duplicated"
                        )
                    leverage_by_side[item.position_side] = item.leverage
                if set(leverage_by_side) != {"long", "short"}:
                    self._block_signal_bundle(
                        "account leverage evidence must cover long and short"
                    )

                exposure_by_side = {
                    "long": Decimal("0"),
                    "short": Decimal("0"),
                }
                positions_by_side = {"long": 0, "short": 0}
                position_summaries = []
                for position in positions:
                    if position.contracts == 0:
                        continue
                    pricing = position.mark_price or mark_price.price
                    exposure = (
                        position.contracts
                        * instrument.contract_value
                        * pricing
                        if instrument.contract_type == "linear"
                        else position.contracts * instrument.contract_value
                    )
                    exposure_by_side[position.position_side] += exposure
                    positions_by_side[position.position_side] += 1
                    position_summaries.append(
                        TrustedPositionSummary(
                            position_side=position.position_side,
                            contracts=position.contracts,
                            mark_price=position.mark_price,
                            leverage=position.leverage,
                            timestamp=position.timestamp,
                        )
                    )

                fetched_snapshots = (
                    instrument_snapshot,
                    candle_snapshot,
                    book_snapshot,
                    mark_snapshot,
                    config_snapshot,
                    positions_snapshot,
                    leverage_snapshot,
                )
                common_expiry = min(
                    *(
                        snapshot.metadata.expires_at
                        for snapshot in fetched_snapshots
                    ),
                    self._attested_session.expires_at,
                )
                observed_at = _utc_now()
                if observed_at.tzinfo is None:
                    self._block_signal_bundle(
                        "signal bundle clock is not timezone-aware"
                    )
                observed_at = observed_at.astimezone(timezone.utc)
                if observed_at >= common_expiry:
                    self._block_signal_bundle(
                        "signal bundle expired before atomic persistence"
                    )
                account_as_of = max(
                    config_snapshot.metadata.fetched_at,
                    positions_snapshot.metadata.fetched_at,
                    leverage_snapshot.metadata.fetched_at,
                )

                instrument_content = TrustedInstrumentSnapshotContent(
                    instId=instrument.inst_id,
                    ctVal=instrument.contract_value,
                    ctValCcy=instrument.contract_value_ccy,
                    lotSz=instrument.lot_size,
                    minSz=instrument.min_size,
                    tickSz=instrument.tick_size,
                    contract_shape=instrument.contract_type,
                    state=instrument.state,
                    expires_at=common_expiry,
                )
                market_content = TrustedMarketSnapshotContent(
                    instrument_id=inst_id,
                    timeframe=timeframe,
                    okx_bar=okx_bar,
                    reference_price=mark_price.price,
                    # ``as_of`` is the locally attested observation time used
                    # by the risk verifier.  Exchange event times remain in
                    # the candle, BBO, and mark evidence for ordering/audit.
                    as_of=observed_at,
                    first_candle_at=candles[0].timestamp,
                    last_candle_at=candles[-1].timestamp,
                    candle_count=len(candles),
                    candle_set_digest=candle_set_digest,
                    confirmed_candles=typed_candles,
                    bbo=bbo,
                    mark=mark,
                    expires_at=common_expiry,
                )
                account_content = TrustedAccountSnapshotContent(
                    current_exposure=sum(
                        exposure_by_side.values(),
                        Decimal("0"),
                    ),
                    open_positions=sum(positions_by_side.values()),
                    exposure_by_position_side=exposure_by_side,
                    open_positions_by_position_side=positions_by_side,
                    leverage_by_position_side=leverage_by_side,
                    positions=tuple(position_summaries),
                    as_of=account_as_of,
                    expires_at=common_expiry,
                )

                self._attested_session.bind_database(db)
                self._attested_session._renew_if_needed(observed_at)
                contents = {
                    "instrument": instrument_content.model_dump(mode="json"),
                    "market": market_content.model_dump(mode="json"),
                    "account": account_content.model_dump(mode="json"),
                }
                rows = {}
                try:
                    with db.begin_nested():
                        for kind, content in contents.items():
                            normalized = _normalize_attested_snapshot(
                                self._attested_session._risk_capability,
                                kind=kind,
                                content=content,
                                observed_at=observed_at,
                                expires_at=common_expiry,
                            )
                            rows[kind] = _write_attested_snapshot(
                                db,
                                self._attested_session._risk_capability,
                                normalized,
                                now=observed_at,
                            )
                except BaseException:
                    try:
                        self._attested_session.revoke("WRITE_FAILURE")
                    except OkxDemoCredentialsUnavailable:
                        pass
                    raise

                def reference(kind: str) -> TrustedSnapshotReference:
                    row = rows[kind]
                    return TrustedSnapshotReference(
                        kind=kind,
                        database_id=row.database_id,
                        snapshot_id=row.snapshot_id,
                        digest=row.digest,
                        expires_at=common_expiry,
                    )

                return TrustedSignalBundle(
                    instrument_id=inst_id,
                    timeframe=timeframe,
                    candle_set_digest=candle_set_digest,
                    observed_at=observed_at,
                    expires_at=common_expiry,
                    instrument=reference("instrument"),
                    market=reference("market"),
                    account=reference("account"),
                )

            @staticmethod
            def _block_signal_bundle(message: str) -> None:
                raise OkxReadAdapterError(
                    kind="INVALID_SIGNAL_BUNDLE",
                    status="BLOCKED",
                    message=message,
                )

            def capture_execution_attestation(
                self,
                db: Session,
                *,
                inst_id: str,
            ) -> ExecutionAttestationBundle:
                """Capture only the evidence needed by the bounded canary.

                This path is intentionally separate from
                ``capture_trusted_signal_bundle``.  It fetches no candles and
                creates no signal evaluation, strategy lineage, or deployment
                evidence.  It still uses the same attested session, read
                adapter, snapshot writer, fingerprint, expiry, and
                reconciliation contracts as the production signal path.
                """

                from app.core.config import get_settings

                allowed_instruments = set(
                    get_settings()
                    .demo_automation_policy.demo_risk_policy.allowed_instruments
                )
                if inst_id not in allowed_instruments:
                    raise OkxReadAdapterError(
                        kind="INVALID_REQUEST",
                        status="BLOCKED",
                        message="instrument is not allowlisted by the OKX Demo risk policy",
                    )
                if not SWAP_ID_PATTERN.fullmatch(inst_id):
                    raise OkxReadAdapterError(
                        kind="INVALID_REQUEST",
                        status="BLOCKED",
                        message="execution attestation instrument id is invalid",
                    )

                instrument_snapshot = self._engine.instruments(inst_id)
                book_snapshot = self._engine.orderbook(inst_id, depth=1)
                mark_snapshot = self._engine.mark_price(inst_id)
                config_snapshot = self._engine.account_config()
                positions_snapshot = self._engine.positions(inst_id)
                leverage_snapshot = self._engine.leverage(inst_id)
                fetched_snapshots = (
                    instrument_snapshot,
                    book_snapshot,
                    mark_snapshot,
                    config_snapshot,
                    positions_snapshot,
                    leverage_snapshot,
                )

                observed_at = _utc_now()
                if observed_at.tzinfo is None:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation clock is not timezone-aware",
                    )
                observed_at = observed_at.astimezone(timezone.utc)
                for snapshot in fetched_snapshots:
                    if (
                        snapshot.metadata.execution_target != "OKX_DEMO"
                        or snapshot.metadata.source != "okx_demo_rest"
                        or snapshot.metadata.stale
                        or snapshot.metadata.expires_at <= observed_at
                        or snapshot.metadata.fetched_at > observed_at
                    ):
                        raise OkxReadAdapterError(
                            kind="STALE_DATA",
                            status="BLOCKED",
                            message="execution attestation contains stale evidence",
                        )
                for snapshot in (config_snapshot, positions_snapshot, leverage_snapshot):
                    if snapshot.metadata.authenticated is not True:
                        raise OkxReadAdapterError(
                            kind="UNAUTHORIZED",
                            status="BLOCKED",
                            message="execution attestation account evidence is unauthenticated",
                        )

                if len(instrument_snapshot.items) != 1:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation requires exactly one instrument",
                    )
                instrument = InstrumentSpec.model_validate(
                    instrument_snapshot.items[0]
                )
                if (
                    instrument.inst_id != inst_id
                    or instrument.state != "live"
                    or instrument.contract_type not in {"linear", "inverse"}
                ):
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation instrument is not active",
                    )

                if len(book_snapshot.items) != 1:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation requires exactly one order book",
                    )
                order_book = OrderBook.model_validate(book_snapshot.items[0])
                if (
                    order_book.inst_id != inst_id
                    or not order_book.bids
                    or not order_book.asks
                ):
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation order book has no complete BBO",
                    )
                best_bid = max(order_book.bids, key=lambda item: item.price)
                best_ask = min(order_book.asks, key=lambda item: item.price)
                if (
                    best_bid.price <= 0
                    or best_ask.price <= 0
                    or best_bid.size <= 0
                    or best_ask.size <= 0
                    or best_bid.price >= best_ask.price
                ):
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation BBO is crossed or locked",
                    )

                if len(mark_snapshot.items) != 1:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation requires exactly one mark price",
                    )
                mark_price = ReferencePrice.model_validate(mark_snapshot.items[0])
                if (
                    mark_price.inst_id != inst_id
                    or mark_price.price_kind != "mark"
                    or mark_price.price <= 0
                ):
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation mark price is invalid",
                    )

                if len(config_snapshot.items) != 1:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation requires exactly one account config",
                    )
                account_config = AccountConfig.model_validate(config_snapshot.items[0])
                if account_config.position_mode != "long_short_mode":
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="OKX Demo account is not in long_short_mode",
                    )

                positions = [
                    Position.model_validate(item)
                    for item in positions_snapshot.items
                ]
                if any(
                    item.inst_id != inst_id
                    or item.margin_mode != "isolated"
                    or item.position_side not in {"long", "short"}
                    or item.contracts < 0
                    or (item.mark_price is not None and item.mark_price <= 0)
                    or (item.leverage is not None and item.leverage <= 0)
                    for item in positions
                ):
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation positions are invalid",
                    )
                leverages = [
                    LeverageInfo.model_validate(item)
                    for item in leverage_snapshot.items
                ]
                leverage_by_side: dict[str, Decimal] = {}
                for item in leverages:
                    if (
                        item.inst_id != inst_id
                        or item.margin_mode != "isolated"
                        or item.position_side not in {"long", "short"}
                        or item.leverage <= 0
                        or item.position_side in leverage_by_side
                    ):
                        raise OkxReadAdapterError(
                            kind="INVALID_RESPONSE",
                            status="BLOCKED",
                            message="execution attestation leverage evidence is invalid",
                        )
                    leverage_by_side[item.position_side] = item.leverage
                if set(leverage_by_side) != {"long", "short"}:
                    raise OkxReadAdapterError(
                        kind="INVALID_RESPONSE",
                        status="BLOCKED",
                        message="execution attestation leverage must cover long and short",
                    )

                exposure_by_side = {"long": Decimal("0"), "short": Decimal("0")}
                positions_by_side = {"long": 0, "short": 0}
                position_summaries = []
                for position in positions:
                    if position.contracts == 0:
                        continue
                    pricing = position.mark_price or mark_price.price
                    exposure = (
                        position.contracts * instrument.contract_value * pricing
                        if instrument.contract_type == "linear"
                        else position.contracts * instrument.contract_value
                    )
                    exposure_by_side[position.position_side] += exposure
                    positions_by_side[position.position_side] += 1
                    position_summaries.append(
                        TrustedPositionSummary(
                            position_side=position.position_side,
                            contracts=position.contracts,
                            mark_price=position.mark_price,
                            leverage=position.leverage,
                            timestamp=position.timestamp,
                        )
                    )

                common_expiry = min(
                    *(snapshot.metadata.expires_at for snapshot in fetched_snapshots),
                    self._attested_session.expires_at,
                )
                exchange_market_as_of = max(
                    order_book.timestamp,
                    mark_price.timestamp,
                )
                account_as_of = max(
                    config_snapshot.metadata.fetched_at,
                    positions_snapshot.metadata.fetched_at,
                    leverage_snapshot.metadata.fetched_at,
                )
                if (
                    exchange_market_as_of
                    > observed_at + timedelta(seconds=FUTURE_SKEW_SECONDS)
                    or observed_at - exchange_market_as_of > timedelta(seconds=30)
                ):
                    raise OkxReadAdapterError(
                        kind="STALE_DATA",
                        status="BLOCKED",
                        message="execution attestation market evidence is stale",
                    )
                if observed_at >= common_expiry:
                    raise OkxReadAdapterError(
                        kind="STALE_DATA",
                        status="BLOCKED",
                        message="execution attestation expired before persistence",
                    )

                instrument_content = TrustedInstrumentSnapshotContent(
                    instId=instrument.inst_id,
                    ctVal=instrument.contract_value,
                    ctValCcy=instrument.contract_value_ccy,
                    lotSz=instrument.lot_size,
                    minSz=instrument.min_size,
                    tickSz=instrument.tick_size,
                    contract_shape=instrument.contract_type,
                    state=instrument.state,
                    expires_at=common_expiry,
                )
                market_content = {
                    "execution_target": "OKX_DEMO",
                    "source": "okx_demo_rest",
                    "resource": "market",
                    "stale": False,
                    "authenticated": False,
                    "execution_only": True,
                    "instrument_id": inst_id,
                    "reference_price": str(mark_price.price),
                    # Keep the verifier-facing timestamp on the attested local
                    # clock; the raw exchange event times remain in BBO/mark.
                    "as_of": observed_at.isoformat(),
                    "bbo": TrustedBbo(
                        bid_price=best_bid.price,
                        bid_size=best_bid.size,
                        ask_price=best_ask.price,
                        ask_size=best_ask.size,
                        timestamp=order_book.timestamp,
                    ).model_dump(mode="json"),
                    "mark": TrustedMarkPrice(
                        price=mark_price.price,
                        timestamp=mark_price.timestamp,
                    ).model_dump(mode="json"),
                    "expires_at": common_expiry.isoformat(),
                }
                account_content = TrustedAccountSnapshotContent(
                    current_exposure=sum(exposure_by_side.values(), Decimal("0")),
                    open_positions=sum(positions_by_side.values()),
                    exposure_by_position_side=exposure_by_side,
                    open_positions_by_position_side=positions_by_side,
                    leverage_by_position_side=leverage_by_side,
                    positions=tuple(position_summaries),
                    as_of=account_as_of,
                    expires_at=common_expiry,
                )

                self._attested_session.bind_database(db)
                self._attested_session._renew_if_needed(observed_at)
                contents = {
                    "instrument": instrument_content.model_dump(mode="json"),
                    "market": market_content,
                    "account": account_content.model_dump(mode="json"),
                }
                rows = {}
                try:
                    with db.begin_nested():
                        for kind, content in contents.items():
                            normalized = _normalize_attested_snapshot(
                                self._attested_session._risk_capability,
                                kind=kind,
                                content=content,
                                observed_at=observed_at,
                                expires_at=common_expiry,
                            )
                            rows[kind] = _write_attested_snapshot(
                                db,
                                self._attested_session._risk_capability,
                                normalized,
                                now=observed_at,
                            )
                except BaseException:
                    try:
                        self._attested_session.revoke("WRITE_FAILURE", db=db)
                    except OkxDemoCredentialsUnavailable:
                        pass
                    raise

                def reference(kind: str) -> TrustedSnapshotReference:
                    row = rows[kind]
                    return TrustedSnapshotReference(
                        kind=kind,
                        database_id=row.database_id,
                        snapshot_id=row.snapshot_id,
                        digest=row.digest,
                        expires_at=common_expiry,
                    )

                return ExecutionAttestationBundle(
                    instrument_id=inst_id,
                    observed_at=observed_at,
                    expires_at=common_expiry,
                    instrument=reference("instrument"),
                    market=reference("market"),
                    account=reference("account"),
                )

            def close(self) -> None:
                self._attested_session.revoke("FACTORY_CLOSE")

            def instruments(self, inst_id=None):
                return self._engine.instruments(inst_id)

            def ticker(self, inst_id):
                return self._engine.ticker(inst_id)

            def candles(self, inst_id, *, bar="1m", limit=100):
                return self._engine.candles(inst_id, bar=bar, limit=limit)

            def orderbook(self, inst_id, *, depth=20):
                return self._engine.orderbook(inst_id, depth=depth)

            def mark_price(self, inst_id):
                return self._engine.mark_price(inst_id)

            def index_price(self, index_id):
                return self._engine.index_price(index_id)

            def funding_rate(self, inst_id):
                return self._engine.funding_rate(inst_id)

            def open_interest(self, inst_id):
                return self._engine.open_interest(inst_id)

            def account_config(self):
                return self._engine.account_config()

            def balance(self, currency=None):
                return self._engine.balance(currency)

            def positions(self, inst_id=None):
                return self._engine.positions(inst_id)

            def leverage(self, inst_id):
                return self._engine.leverage(inst_id)

            def exchange_max_leverage(self, inst_id):
                return self._engine.exchange_max_leverage(inst_id)

            def maximum_order_quantity(
                self, inst_id, *, td_mode, price, leverage
            ):
                return self._engine.maximum_order_quantity(
                    inst_id,
                    td_mode=td_mode,
                    price=price,
                    leverage=leverage,
                )

            def fees(self, inst_id):
                return self._engine.fees(inst_id)

            def order(
                self,
                inst_id,
                *,
                order_id=None,
                client_order_id=None,
            ):
                return self._engine.order(
                    inst_id,
                    order_id=order_id,
                    client_order_id=client_order_id,
                )

            def pending_orders(
                self,
                inst_id=None,
                *,
                after=None,
                before=None,
                limit=100,
            ):
                return self._engine.pending_orders(
                    inst_id,
                    after=after,
                    before=before,
                    limit=limit,
                )

            def fills_history(
                self,
                inst_id=None,
                *,
                after=None,
                before=None,
                begin=None,
                end=None,
                limit=100,
            ):
                return self._engine.fills_history(
                    inst_id,
                    after=after,
                    before=before,
                    begin=begin,
                    end=end,
                    limit=limit,
                )

            def orders_history(
                self,
                inst_id=None,
                *,
                after=None,
                before=None,
                limit=100,
            ):
                return self._engine.orders_history(
                    inst_id,
                    after=after,
                    before=before,
                    limit=limit,
                )

        return ProductionReadClient()
    except (OkxDemoPreflightBlocked, AttestationProofKeyUnavailable):
        raise OkxDemoCredentialsUnavailable(
            "OKX_DEMO credential provider account attestation failed"
        ) from None
    finally:
        snapshot.clear()


def _millis(value: Any) -> datetime:
    milliseconds = int(str(value))
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _optional_millis(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    return _millis(value)


def _optional_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _optional_boolean(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("boolean field is malformed")
