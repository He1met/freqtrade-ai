"""Credential-free OKX public history-candle adapter for canonical market intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.canonical_v13.market_acquisition import (
    CanonicalMarketAcquisitionBlocked,
    MarketAcquisitionPayload,
    MarketAcquisitionRequest,
)


OKX_HISTORY_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
MAXIMUM_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class OkxPublicCandle:
    opened_at_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    volume_currency: str
    volume_quote: str


Fetch = Callable[[str, float], bytes]


def _default_fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "freqtrade-ai-canonical-v13-public-market/1",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAXIMUM_RESPONSE_BYTES + 1)
    if len(payload) > MAXIMUM_RESPONSE_BYTES:
        raise _blocked("BLOCKED_OKX_RESPONSE_TOO_LARGE", "response exceeded limit")
    return payload


def _blocked(code: str, detail: str) -> CanonicalMarketAcquisitionBlocked:
    return CanonicalMarketAcquisitionBlocked(code, detail)


def _number(value: object, *, field: str, positive: bool) -> str:
    if not isinstance(value, str) or not value:
        raise _blocked("BLOCKED_OKX_CANDLE_SHAPE", f"{field} must be text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise _blocked("BLOCKED_OKX_CANDLE_NUMBER", field) from exc
    minimum = Decimal("0.000000000000000001") if positive else Decimal("0")
    if not parsed.is_finite() or parsed < minimum:
        raise _blocked("BLOCKED_OKX_CANDLE_NUMBER", field)
    return value


def _parse_candle(raw: object) -> OkxPublicCandle:
    if not isinstance(raw, list) or len(raw) != 9 or raw[8] != "1":
        raise _blocked(
            "BLOCKED_OKX_CANDLE_UNCONFIRMED_OR_INVALID",
            "only exact confirmed nine-field candles are accepted",
        )
    if not isinstance(raw[0], str) or not raw[0].isdigit():
        raise _blocked("BLOCKED_OKX_CANDLE_TIMESTAMP", "timestamp is invalid")
    values = [
        _number(raw[index], field=field, positive=index in {1, 2, 3, 4})
        for index, field in enumerate(
            (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "volume_currency",
                "volume_quote",
            ),
            start=1,
        )
    ]
    opened, high, low, closed = map(
        Decimal, (values[0], values[1], values[2], values[3])
    )
    if high < max(opened, closed, low) or low > min(opened, closed, high):
        raise _blocked("BLOCKED_OKX_CANDLE_OHLC", "OHLC bounds are inconsistent")
    return OkxPublicCandle(int(raw[0]), *values)


def _interval(timeframe: str) -> timedelta:
    if timeframe.endswith("m") and timeframe[:-1].isdigit():
        return timedelta(minutes=int(timeframe[:-1]))
    if timeframe.endswith("H") and timeframe[:-1].isdigit():
        return timedelta(hours=int(timeframe[:-1]))
    raise _blocked("BLOCKED_MARKET_TIMEFRAME_UNSUPPORTED", timeframe)


class OkxPublicHistoryCandleDownloader:
    """Finite, rate-limited public downloader with no credential surface."""

    provenance_class = "PRODUCTION_PUBLIC_MARKET_DATA"
    network_access = "PUBLIC_MARKET_DATA_ONLY"
    credential_access = "NONE"

    def __init__(
        self,
        *,
        fetch: Fetch = _default_fetch,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 10.0,
        maximum_attempts: int = 3,
        minimum_request_interval_seconds: float = 0.11,
        page_limit: int = 300,
        maximum_pages: int = 1000,
    ) -> None:
        if (
            timeout_seconds <= 0
            or maximum_attempts < 1
            or minimum_request_interval_seconds < 0.1
            or not 1 <= page_limit <= 300
            or maximum_pages < 1
        ):
            raise ValueError("invalid OKX public downloader policy")
        self._fetch = fetch
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        self._maximum_attempts = maximum_attempts
        self._minimum_request_interval_seconds = minimum_request_interval_seconds
        self._page_limit = page_limit
        self._maximum_pages = maximum_pages

    def _request_page(self, url: str) -> bytes:
        for attempt in range(1, self._maximum_attempts + 1):
            try:
                payload = self._fetch(url, self._timeout_seconds)
                if len(payload) > MAXIMUM_RESPONSE_BYTES:
                    raise _blocked(
                        "BLOCKED_OKX_RESPONSE_TOO_LARGE", "response exceeded limit"
                    )
                return payload
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self._maximum_attempts:
                    raise _blocked(
                        "BLOCKED_OKX_PUBLIC_HTTP", f"status={exc.code}"
                    ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self._maximum_attempts:
                    raise _blocked(
                        "BLOCKED_OKX_PUBLIC_UNAVAILABLE",
                        "finite retries exhausted",
                    ) from exc
            self._sleep(min(2 ** (attempt - 1), 4))
        raise AssertionError("unreachable")

    def acquire(self, request: MarketAcquisitionRequest) -> MarketAcquisitionPayload:
        if request.source_identity != "okx-public-history-candles-v1":
            raise _blocked("BLOCKED_OKX_SOURCE_IDENTITY", request.source_identity)
        if (
            request.instrument != "BTC-USDT-SWAP"
            or request.pair != "BTC/USDT:USDT"
            or request.timeframe != "15m"
            or request.data_kind != "futures"
        ):
            raise _blocked("BLOCKED_OKX_PUBLIC_TARGET", request.instrument)
        interval = _interval(request.timeframe)
        interval_ms = int(interval.total_seconds() * 1000)
        start_ms = int(request.requested_start.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(request.requested_end.astimezone(timezone.utc).timestamp() * 1000)
        if start_ms % interval_ms or end_ms % interval_ms or end_ms <= start_ms:
            raise _blocked("BLOCKED_MARKET_REQUEST_RANGE", "range must align to timeframe")
        candles: dict[int, OkxPublicCandle] = {}
        cursor = end_ms
        for page_number in range(self._maximum_pages):
            query = urlencode(
                {
                    "instId": request.instrument,
                    "bar": request.timeframe,
                    "after": str(cursor),
                    "limit": str(self._page_limit),
                }
            )
            raw = self._request_page(f"{OKX_HISTORY_CANDLES_URL}?{query}")
            try:
                envelope = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _blocked("BLOCKED_OKX_RESPONSE_JSON", "response is not JSON") from exc
            if (
                not isinstance(envelope, dict)
                or envelope.get("code") != "0"
                or envelope.get("msg") not in {"", None}
                or not isinstance(envelope.get("data"), list)
            ):
                raise _blocked("BLOCKED_OKX_RESPONSE_ENVELOPE", "OKX did not accept request")
            rows = envelope["data"]
            if not rows:
                break
            page = [_parse_candle(row) for row in rows]
            for candle in page:
                previous = candles.get(candle.opened_at_ms)
                if previous is not None and previous != candle:
                    raise _blocked("BLOCKED_OKX_CONFLICTING_DUPLICATE", str(candle.opened_at_ms))
                candles[candle.opened_at_ms] = candle
            oldest = min(item.opened_at_ms for item in page)
            if oldest <= start_ms:
                break
            if oldest >= cursor:
                raise _blocked("BLOCKED_OKX_PAGINATION_STALLED", str(cursor))
            cursor = oldest
            if page_number + 1 >= self._maximum_pages:
                raise _blocked("BLOCKED_OKX_PAGE_LIMIT", str(self._maximum_pages))
            self._sleep(self._minimum_request_interval_seconds)
        selected = [
            candle
            for timestamp, candle in sorted(candles.items())
            if start_ms <= timestamp < end_ms
        ]
        expected_count = (end_ms - start_ms) // interval_ms
        if len(selected) != expected_count:
            raise _blocked(
                "BLOCKED_OKX_CANDLE_GAP",
                f"expected={expected_count},observed={len(selected)}",
            )
        for previous, current in zip(selected, selected[1:]):
            if current.opened_at_ms - previous.opened_at_ms != interval_ms:
                raise _blocked("BLOCKED_OKX_CANDLE_GAP", str(current.opened_at_ms))
        lines = [
            json.dumps(
                {
                    "close": item.close,
                    "high": item.high,
                    "low": item.low,
                    "open": item.open,
                    "opened_at": datetime.fromtimestamp(
                        item.opened_at_ms / 1000, tz=timezone.utc
                    ).isoformat(),
                    "volume": item.volume,
                    "volume_currency": item.volume_currency,
                    "volume_quote": item.volume_quote,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in selected
        ]
        content = ("\n".join(lines) + "\n").encode("utf-8")
        digest = sha256(content).hexdigest()
        locator = (
            f"canonical_v13/okx-public/{request.instrument}/{request.timeframe}/"
            f"{start_ms}-{end_ms}-{digest}.jsonl"
        )
        return MarketAcquisitionPayload(
            content=content,
            locator=locator,
            media_type="application/x-ndjson; schema=canonical-v13-okx-candle-v1",
            observed_first_open=request.requested_start.astimezone(timezone.utc),
            observed_last_close=request.requested_end.astimezone(timezone.utc),
            observed_closed_candles=len(selected),
        )


__all__ = ["OKX_HISTORY_CANDLES_URL", "OkxPublicHistoryCandleDownloader"]
