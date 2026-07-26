from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

from app.adapters.okx_demo.credentials import (
    OkxDemoCredentialProvider,
    OkxDemoCredentialsUnavailable,
    UnavailableOkxDemoCredentialProvider,
    _is_attested_okx_demo_credential_provider,
)
from app.adapters.okx_demo.errors import OkxReadAdapterError
from app.adapters.okx_demo.models import (
    AccountConfig,
    Balance,
    Candle,
    FundingRate,
    InstrumentSpec,
    LeverageInfo,
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
    "fees": 3600,
    "order": 15,
}


class OkxDemoReadAdapter:
    """GET-only OKX_DEMO adapter with normalized, credential-free output."""

    def __init__(
        self,
        *,
        execution_target: str,
        transport: Optional[OkxReadTransport] = None,
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
        resolved_transport = transport or UrllibOkxReadTransport()
        resolved_provider = (
            credential_provider or UnavailableOkxDemoCredentialProvider()
        )
        if (
            isinstance(resolved_transport, UrllibOkxReadTransport)
            and not isinstance(
                resolved_provider,
                UnavailableOkxDemoCredentialProvider,
            )
            and not _is_attested_okx_demo_credential_provider(resolved_provider)
        ):
            raise OkxReadAdapterError(
                kind="UNAUTHORIZED",
                status="BLOCKED",
                message=(
                    "OKX real read transport requires the attested credential factory"
                ),
            )
        self._transport = resolved_transport
        self._credential_provider = resolved_provider
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
        if not re.fullmatch(r"[1-9][0-9]*(?:m|H|D|W|M)(?:utc)?", bar):
            self._invalid_request("bar is not an allowed OKX candle interval")
        if not 1 <= limit <= 300:
            self._invalid_request("candle limit must be between 1 and 300")
        return self._request(
            resource="candles",
            path="/api/v5/market/candles",
            query={"instId": self._swap_id(inst_id), "bar": bar, "limit": str(limit)},
            authenticated=False,
            parser=lambda data: [self._candle(item) for item in data],
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

    def _request(
        self,
        *,
        resource: str,
        path: str,
        query: Mapping[str, str],
        authenticated: bool,
        parser: Callable[[list[Any]], Sequence[BaseModel]],
        allow_empty: bool = False,
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

        exchange_timestamp = self._latest_timestamp(normalized)
        freshness_anchor = exchange_timestamp or response.received_at
        expires_at = freshness_anchor + timedelta(seconds=self._ttls[resource])
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
                fetched_at=response.received_at,
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
        return InstrumentSpec(
            inst_id=item["instId"],
            inst_type=item["instType"],
            base_ccy=item["baseCcy"],
            quote_ccy=item["quoteCcy"],
            settle_ccy=item["settleCcy"],
            contract_type=item.get("ctType", ""),
            contract_value=Decimal(item["ctVal"]),
            contract_value_ccy=item["ctValCcy"],
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
        return Position(
            inst_id=item["instId"],
            margin_mode=item["mgnMode"],
            position_side=item["posSide"],
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
        return LeverageInfo(
            inst_id=item["instId"],
            margin_mode=item["mgnMode"],
            position_side=item.get("posSide", "net"),
            leverage=Decimal(item["lever"]),
        )

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
        return OrderQuery(
            inst_id=item["instId"],
            order_id=item["ordId"],
            client_order_id=item.get("clOrdId") or None,
            state=item["state"],
            side=item["side"],
            position_side=item["posSide"],
            order_type=item["ordType"],
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
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
