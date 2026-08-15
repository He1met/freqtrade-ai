"""Public-only OKX metadata frozen for networkless Freqtrade research."""

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


INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
POSITION_TIERS_URL = "https://www.okx.com/api/v5/public/position-tiers"
CONTRACT = "canonical-v13-okx-offline-exchange-metadata-v1"
SOURCE_IDENTITY = "okx-public-instruments-position-tiers-v1"
ADAPTER_IDENTITY = "freqtrade-2026.6-ccxt-4.5.61-okx-offline-v1"
MEDIA_TYPE = f"application/json; schema={CONTRACT}"
MAXIMUM_RESPONSE_BYTES = 2_000_000
MAXIMUM_TIER_ROWS = 256


class OfflineExchangeMetadataBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class OfflineExchangeMetadataRequest:
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    target_snapshot_id: str
    target_snapshot_digest: str
    window_snapshot_id: str
    window_snapshot_digest: str
    freshness_max_age_seconds: int


@dataclass(frozen=True)
class OfflineExchangeMetadataPayload:
    content: bytes
    locator: str
    content_digest: str
    observed_at: datetime
    fresh_until: datetime
    market_count: int
    leverage_tier_count: int
    receipt_digest: str


Fetch = Callable[[str, float], bytes]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def offline_exchange_metadata_receipt_digest(
    *, content_digest: str, observed_at: str, fresh_until: str
) -> str:
    return _digest(
        {
            "contract": "canonical-v13-offline-exchange-metadata-receipt-v1",
            "content_digest": content_digest,
            "observed_at": observed_at,
            "fresh_until": fresh_until,
            "source_identity": SOURCE_IDENTITY,
            "network_access": "PUBLIC_MARKET_DATA_ONLY",
            "credential_access": "NONE",
            "status": "ACCEPTED",
        }
    )


def _default_fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "freqtrade-ai-canonical-v13-public-metadata/1",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAXIMUM_RESPONSE_BYTES + 1)
    if len(payload) > MAXIMUM_RESPONSE_BYTES:
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OKX_METADATA_RESPONSE_TOO_LARGE", "response exceeded limit"
        )
    return payload


def _number(value: object, *, field: str, positive: bool = False) -> float:
    if not isinstance(value, str) or not value:
        raise OfflineExchangeMetadataBlocked("BLOCKED_OKX_METADATA_SHAPE", field)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise OfflineExchangeMetadataBlocked("BLOCKED_OKX_METADATA_NUMBER", field) from exc
    if not parsed.is_finite() or parsed < (Decimal("0") if not positive else Decimal("0.000000000000000001")):
        raise OfflineExchangeMetadataBlocked("BLOCKED_OKX_METADATA_NUMBER", field)
    return float(parsed)


def _envelope(raw: bytes, *, maximum_rows: int) -> list[dict[str, object]]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OKX_METADATA_JSON", "response is not JSON"
        ) from exc
    rows = value.get("data") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("code") != "0"
        or value.get("msg") not in {"", None}
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= maximum_rows
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OKX_METADATA_ENVELOPE", "public response envelope is invalid"
        )
    return [dict(row) for row in rows]


class OkxPublicOfflineExchangeMetadataDownloader:
    """Finite public-only acquisition with a closed host/path/query allowlist."""

    provenance_class = "PRODUCTION_PUBLIC_EXCHANGE_METADATA"
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
    ) -> None:
        if timeout_seconds <= 0 or maximum_attempts not in {1, 2, 3} or minimum_request_interval_seconds < 0.1:
            raise ValueError("invalid OKX public metadata downloader policy")
        self._fetch = fetch
        self._sleep = sleep
        self._timeout = timeout_seconds
        self._attempts = maximum_attempts
        self._interval = minimum_request_interval_seconds

    def _get(self, url: str) -> bytes:
        for attempt in range(1, self._attempts + 1):
            try:
                raw = self._fetch(url, self._timeout)
                if len(raw) > MAXIMUM_RESPONSE_BYTES:
                    raise OfflineExchangeMetadataBlocked(
                        "BLOCKED_OKX_METADATA_RESPONSE_TOO_LARGE", "response exceeded limit"
                    )
                return raw
            except HTTPError as exc:
                if attempt == self._attempts or not (exc.code == 429 or 500 <= exc.code < 600):
                    raise OfflineExchangeMetadataBlocked(
                        "BLOCKED_OKX_METADATA_HTTP", f"status={exc.code}"
                    ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self._attempts:
                    raise OfflineExchangeMetadataBlocked(
                        "BLOCKED_OKX_METADATA_UNAVAILABLE", "finite retries exhausted"
                    ) from exc
            self._sleep(min(2 ** (attempt - 1), 4))
        raise AssertionError("unreachable")

    def acquire(
        self, request: OfflineExchangeMetadataRequest, *, observed_at: datetime
    ) -> OfflineExchangeMetadataPayload:
        if observed_at.tzinfo is None or request.freshness_max_age_seconds <= 0:
            raise OfflineExchangeMetadataBlocked(
                "BLOCKED_OFFLINE_METADATA_FRESHNESS", "explicit UTC freshness is required"
            )
        if (
            request.instrument != "BTC-USDT-SWAP"
            or request.pair != "BTC/USDT:USDT"
            or request.timeframe != "15m"
            or request.data_kind != "futures"
        ):
            raise OfflineExchangeMetadataBlocked(
                "BLOCKED_OKX_METADATA_TARGET", request.instrument
            )
        instrument_url = INSTRUMENTS_URL + "?" + urlencode(
            {"instType": "SWAP", "instId": request.instrument}
        )
        tier_url = POSITION_TIERS_URL + "?" + urlencode(
            {"instType": "SWAP", "tdMode": "isolated", "uly": "BTC-USDT"}
        )
        instruments = _envelope(self._get(instrument_url), maximum_rows=1)
        self._sleep(self._interval)
        tiers = _envelope(self._get(tier_url), maximum_rows=MAXIMUM_TIER_ROWS)
        instrument = instruments[0]
        if (
            instrument.get("instId") != request.instrument
            or instrument.get("instType") != "SWAP"
            or instrument.get("state") != "live"
            or instrument.get("uly") != "BTC-USDT"
            or instrument.get("settleCcy") != "USDT"
        ):
            raise OfflineExchangeMetadataBlocked(
                "BLOCKED_OKX_METADATA_INSTRUMENT", "instrument identity/state drifted"
            )
        selected_tiers = [row for row in tiers if row.get("uly") == "BTC-USDT"]
        if not selected_tiers or len(selected_tiers) != len(tiers):
            raise OfflineExchangeMetadataBlocked(
                "BLOCKED_OKX_METADATA_TIERS", "position tiers are incomplete or mixed"
            )
        normalized_tiers = []
        previous_max: Decimal | None = None
        tier_step = Decimal(str(instrument["lotSz"]))
        for expected_tier, row in enumerate(selected_tiers, 1):
            if str(row.get("tier")) != str(expected_tier):
                raise OfflineExchangeMetadataBlocked(
                    "BLOCKED_OKX_METADATA_TIERS", "tier sequence is not contiguous"
                )
            minimum = _number(row.get("minSz"), field="minSz")
            maximum = _number(row.get("maxSz"), field="maxSz", positive=True)
            minimum_raw = Decimal(str(row["minSz"]))
            maximum_raw = Decimal(str(row["maxSz"]))
            if (
                maximum_raw <= minimum_raw
                or (
                    previous_max is not None
                    and not Decimal("0") <= minimum_raw - previous_max <= tier_step
                )
            ):
                raise OfflineExchangeMetadataBlocked(
                    "BLOCKED_OKX_METADATA_TIERS", "tier bounds are not contiguous"
                )
            previous_max = maximum_raw
            normalized_tiers.append(
                {
                    "tier": expected_tier,
                    "symbol": request.pair,
                    "currency": "USDT",
                    "minNotional": minimum,
                    "maxNotional": maximum,
                    "maintenanceMarginRate": _number(row.get("mmr"), field="mmr"),
                    "maxLeverage": _number(row.get("maxLever"), field="maxLever", positive=True),
                    "info": row,
                }
            )
        market = {
            "id": request.instrument,
            "symbol": request.pair,
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "baseId": "BTC",
            "quoteId": "USDT",
            "settleId": "USDT",
            "type": "swap",
            "spot": False,
            "margin": False,
            "swap": True,
            "future": False,
            "option": False,
            "active": True,
            "contract": True,
            "linear": True,
            "inverse": False,
            "contractSize": _number(instrument.get("ctVal"), field="ctVal", positive=True),
            "expiry": None,
            "expiryDatetime": None,
            "strike": None,
            "optionType": None,
            "precision": {
                "amount": _number(instrument.get("lotSz"), field="lotSz", positive=True),
                "price": _number(instrument.get("tickSz"), field="tickSz", positive=True),
            },
            "limits": {
                "leverage": {"min": 1.0, "max": _number(instrument.get("lever"), field="lever", positive=True)},
                "amount": {"min": _number(instrument.get("minSz"), field="minSz", positive=True), "max": None},
                "price": {"min": None, "max": None},
                "cost": {"min": None, "max": None},
            },
            "info": instrument,
        }
        observed = observed_at.astimezone(timezone.utc)
        fresh_until = observed + timedelta(seconds=request.freshness_max_age_seconds)
        facts = {
            "contract": CONTRACT,
            "source_identity": SOURCE_IDENTITY,
            "adapter_identity": ADAPTER_IDENTITY,
            "freqtrade_version": "2026.6",
            "ccxt_version": "4.5.61",
            "target_key": request.target_key,
            "instrument": request.instrument,
            "pair": request.pair,
            "timeframe": request.timeframe,
            "data_kind": request.data_kind,
            "target_snapshot_id": request.target_snapshot_id,
            "target_snapshot_digest": request.target_snapshot_digest,
            "window_snapshot_id": request.window_snapshot_id,
            "window_snapshot_digest": request.window_snapshot_digest,
            "observed_at": observed.isoformat(),
            "fresh_until": fresh_until.isoformat(),
            "network_access": self.network_access,
            "credential_access": self.credential_access,
            "markets": {request.pair: market},
            "leverage_tiers": {request.pair: normalized_tiers},
        }
        content = _canonical(facts)
        content_digest = sha256(content).hexdigest()
        receipt_digest = offline_exchange_metadata_receipt_digest(
            content_digest=content_digest,
            observed_at=observed.isoformat(),
            fresh_until=fresh_until.isoformat(),
        )
        locator = (
            f"canonical_v13/okx-public/{request.instrument}/exchange-metadata/"
            f"{content_digest}.json"
        )
        return OfflineExchangeMetadataPayload(
            content=content,
            locator=locator,
            content_digest=content_digest,
            observed_at=observed,
            fresh_until=fresh_until,
            market_count=1,
            leverage_tier_count=len(normalized_tiers),
            receipt_digest=receipt_digest,
        )


def verify_offline_exchange_metadata(
    content: bytes,
    *,
    expected_digest: str,
    observed_at: datetime,
    expected_receipt_digest: str | None = None,
) -> dict[str, object]:
    if sha256(content).hexdigest() != expected_digest:
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_DIGEST", "content digest drifted"
        )
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_JSON", "artifact is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or _canonical(payload) != content
        or payload.get("contract") != CONTRACT
        or payload.get("source_identity") != SOURCE_IDENTITY
        or payload.get("adapter_identity") != ADAPTER_IDENTITY
        or payload.get("freqtrade_version") != "2026.6"
        or payload.get("ccxt_version") != "4.5.61"
    ):
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_CONTRACT", "artifact contract drifted"
        )
    if payload.get("credential_access") != "NONE" or payload.get("network_access") != "PUBLIC_MARKET_DATA_ONLY":
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_CAPABILITY", "public-only boundary drifted"
        )
    markets = payload.get("markets")
    tiers = payload.get("leverage_tiers")
    pair = payload.get("pair")
    if (
        not isinstance(pair, str)
        or not isinstance(markets, dict)
        or set(markets) != {pair}
        or not isinstance(tiers, dict)
        or set(tiers) != {pair}
        or not isinstance(tiers[pair], list)
        or not tiers[pair]
    ):
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_SET", "market/leverage set is incomplete"
        )
    try:
        fresh_until = datetime.fromisoformat(str(payload["fresh_until"]))
    except (KeyError, ValueError) as exc:
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_FRESHNESS", "freshness timestamp is invalid"
        ) from exc
    if fresh_until.tzinfo is None or observed_at.astimezone(timezone.utc) > fresh_until.astimezone(timezone.utc):
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_STALE", "artifact freshness expired"
        )
    if expected_receipt_digest is not None and expected_receipt_digest != offline_exchange_metadata_receipt_digest(
        content_digest=expected_digest,
        observed_at=str(payload.get("observed_at")),
        fresh_until=str(payload.get("fresh_until")),
    ):
        raise OfflineExchangeMetadataBlocked(
            "BLOCKED_OFFLINE_METADATA_RECEIPT", "acquisition receipt drifted"
        )
    return payload


__all__ = [
    "ADAPTER_IDENTITY",
    "CONTRACT",
    "MEDIA_TYPE",
    "OfflineExchangeMetadataBlocked",
    "OfflineExchangeMetadataPayload",
    "OfflineExchangeMetadataRequest",
    "OkxPublicOfflineExchangeMetadataDownloader",
    "offline_exchange_metadata_receipt_digest",
    "verify_offline_exchange_metadata",
]
