"""No-network market acquisition ports and immutable provenance receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Protocol


class CanonicalMarketAcquisitionBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class MarketAcquisitionRequest:
    source_identity: str
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    requested_start: datetime
    requested_end: datetime


@dataclass(frozen=True)
class MarketAcquisitionPayload:
    content: bytes
    locator: str
    media_type: str
    observed_first_open: datetime
    observed_last_close: datetime
    observed_closed_candles: int


@dataclass(frozen=True)
class MarketAcquisitionReceipt:
    status: str
    provenance_class: str
    source_identity: str
    target_key: str
    instrument: str
    pair: str
    timeframe: str
    data_kind: str
    requested_start: str
    requested_end: str
    observed_first_open: str
    observed_last_close: str
    observed_closed_candles: int
    content_digest: str
    acquired_at: str
    network_access: str
    credential_access: str
    receipt_digest: str


class MarketDownloaderPort(Protocol):
    """External implementations live outside canonical domain code."""

    provenance_class: str
    network_access: str
    credential_access: str

    def acquire(self, request: MarketAcquisitionRequest) -> MarketAcquisitionPayload:
        ...


def _utc(value: datetime, *, field: str) -> str:
    if value.tzinfo is None:
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_TIMEZONE_UNSET", f"{field} must be timezone-aware"
        )
    return value.astimezone(timezone.utc).isoformat()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def acquire_market_evidence(
    request: MarketAcquisitionRequest,
    *,
    downloader: MarketDownloaderPort,
    observed_at: datetime,
) -> tuple[MarketAcquisitionPayload, MarketAcquisitionReceipt]:
    """Invoke an injected port and produce a typed receipt; no port is implicit."""

    requested_start = _utc(request.requested_start, field="requested_start")
    requested_end = _utc(request.requested_end, field="requested_end")
    acquired_at = _utc(observed_at, field="observed_at")
    if request.requested_end <= request.requested_start:
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_REQUEST_RANGE", "requested_end must be after start"
        )
    if not all(
        (
            request.source_identity,
            request.target_key,
            request.instrument,
            request.pair,
            request.timeframe,
            request.data_kind,
        )
    ):
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_PROVENANCE_UNSET", "all target/source facts are required"
        )
    if downloader.credential_access != "NONE":
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_CREDENTIAL_CAPABILITY",
            "canonical acquisition cannot mount credentials",
        )
    if downloader.network_access not in {"NONE", "PUBLIC_MARKET_DATA_ONLY"}:
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_NETWORK_CAPABILITY",
            "only an explicit public-data capability is allowed",
        )
    if (
        downloader.provenance_class == "PRODUCTION_PUBLIC_MARKET_DATA"
        and downloader.network_access != "PUBLIC_MARKET_DATA_ONLY"
    ):
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_PROVENANCE_CAPABILITY_DRIFT",
            "production provenance requires the explicit public-data capability",
        )
    if downloader.provenance_class not in {
        "TEST_SIMULATED",
        "PRODUCTION_PUBLIC_MARKET_DATA",
    }:
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_PROVENANCE_UNSET", "provenance class is unknown"
        )
    payload = downloader.acquire(request)
    first_open = _utc(payload.observed_first_open, field="observed_first_open")
    last_close = _utc(payload.observed_last_close, field="observed_last_close")
    if (
        not payload.content
        or payload.observed_closed_candles <= 0
        or payload.observed_last_close <= payload.observed_first_open
        or payload.observed_first_open > request.requested_start
        or payload.observed_last_close < request.requested_end
        or observed_at < payload.observed_last_close
    ):
        raise CanonicalMarketAcquisitionBlocked(
            "BLOCKED_MARKET_ACQUISITION_EMPTY",
            "content, requested coverage, closed candles, and causal time are required",
        )
    content_digest = sha256(payload.content).hexdigest()
    facts = {
        "contract": "canonical-v13-market-acquisition-receipt-v1",
        "provenance_class": downloader.provenance_class,
        "source_identity": request.source_identity,
        "target_key": request.target_key,
        "instrument": request.instrument,
        "pair": request.pair,
        "timeframe": request.timeframe,
        "data_kind": request.data_kind,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "observed_first_open": first_open,
        "observed_last_close": last_close,
        "observed_closed_candles": payload.observed_closed_candles,
        "content_digest": content_digest,
        "acquired_at": acquired_at,
        "network_access": downloader.network_access,
        "credential_access": downloader.credential_access,
    }
    receipt = MarketAcquisitionReceipt(
        status="ACCEPTED",
        receipt_digest=_digest({**facts, "status": "ACCEPTED"}),
        **{key: value for key, value in facts.items() if key != "contract"},
    )
    return payload, receipt


def verify_market_acquisition_receipt(receipt: MarketAcquisitionReceipt) -> bool:
    """Recompute the immutable receipt identity without contacting its source."""

    facts = {
        "contract": "canonical-v13-market-acquisition-receipt-v1",
        "provenance_class": receipt.provenance_class,
        "source_identity": receipt.source_identity,
        "target_key": receipt.target_key,
        "instrument": receipt.instrument,
        "pair": receipt.pair,
        "timeframe": receipt.timeframe,
        "data_kind": receipt.data_kind,
        "requested_start": receipt.requested_start,
        "requested_end": receipt.requested_end,
        "observed_first_open": receipt.observed_first_open,
        "observed_last_close": receipt.observed_last_close,
        "observed_closed_candles": receipt.observed_closed_candles,
        "content_digest": receipt.content_digest,
        "acquired_at": receipt.acquired_at,
        "network_access": receipt.network_access,
        "credential_access": receipt.credential_access,
        "status": receipt.status,
    }
    return receipt.status == "ACCEPTED" and _digest(facts) == receipt.receipt_digest


__all__ = [
    "CanonicalMarketAcquisitionBlocked",
    "MarketAcquisitionPayload",
    "MarketAcquisitionReceipt",
    "MarketAcquisitionRequest",
    "MarketDownloaderPort",
    "acquire_market_evidence",
    "verify_market_acquisition_receipt",
]
