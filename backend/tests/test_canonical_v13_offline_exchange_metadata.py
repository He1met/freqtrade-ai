from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.canonical_v13.offline_exchange_metadata import (
    INSTRUMENTS_URL,
    POSITION_TIERS_URL,
    OfflineExchangeMetadataBlocked,
    OfflineExchangeMetadataRequest,
    OkxPublicOfflineExchangeMetadataDownloader,
    verify_offline_exchange_metadata,
)


NOW = datetime(2026, 8, 15, 5, tzinfo=timezone.utc)


def _request(
    instrument: str = "BTC-USDT-SWAP",
    pair: str = "BTC/USDT:USDT",
) -> OfflineExchangeMetadataRequest:
    return OfflineExchangeMetadataRequest(
        target_key=instrument.lower() + "-15m",
        instrument=instrument,
        pair=pair,
        timeframe="15m",
        data_kind="futures",
        target_snapshot_id="11111111-1111-1111-1111-111111111111",
        target_snapshot_digest="a" * 64,
        window_snapshot_id="22222222-2222-2222-2222-222222222222",
        window_snapshot_digest="b" * 64,
        freshness_max_age_seconds=3600,
    )


def _instrument(
    instrument: str = "BTC-USDT-SWAP",
    underlying: str = "BTC-USDT",
) -> dict[str, str]:
    return {
        "instId": instrument,
        "instType": "SWAP",
        "state": "live",
        "uly": underlying,
        "baseCcy": "",
        "quoteCcy": "",
        "settleCcy": "USDT",
        "ctVal": "0.01",
        "lotSz": "0.01",
        "tickSz": "0.1",
        "minSz": "0.01",
        "lever": "100",
    }


def _tier(
    number: int,
    minimum: str,
    maximum: str,
    underlying: str = "BTC-USDT",
) -> dict[str, str]:
    return {
        "tier": str(number),
        "uly": underlying,
        "instId": "",
        "minSz": minimum,
        "maxSz": maximum,
        "mmr": "0.01",
        "maxLever": "100" if number == 1 else "50",
    }


def test_public_metadata_is_allowlisted_digest_bound_and_replayable() -> None:
    urls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        urls.append(url)
        assert timeout == 3
        data = [_instrument()] if url.startswith(INSTRUMENTS_URL + "?") else [
            _tier(1, "0", "1000"),
            _tier(2, "1000", "5000"),
        ]
        return json.dumps({"code": "0", "msg": "", "data": data}).encode()

    sleeps: list[float] = []
    downloader = OkxPublicOfflineExchangeMetadataDownloader(
        fetch=fetch, sleep=sleeps.append, timeout_seconds=3
    )
    first = downloader.acquire(_request(), observed_at=NOW)
    second = downloader.acquire(_request(), observed_at=NOW)
    assert first == second
    assert first.locator.endswith(first.content_digest + ".json")
    assert sleeps == [0.11, 0.11]
    assert all(url.startswith((INSTRUMENTS_URL + "?", POSITION_TIERS_URL + "?")) for url in urls)
    assert all("apiKey" not in url and "sign" not in url and "secret" not in url for url in urls)
    payload = verify_offline_exchange_metadata(
        first.content,
        expected_digest=first.content_digest,
        observed_at=NOW,
        expected_receipt_digest=first.receipt_digest,
    )
    assert payload["credential_access"] == "NONE"
    assert payload["network_access"] == "PUBLIC_MARKET_DATA_ONLY"
    assert list(payload["markets"]) == ["BTC/USDT:USDT"]
    assert len(payload["leverage_tiers"]["BTC/USDT:USDT"]) == 2


@pytest.mark.parametrize(
    ("instrument", "pair", "base", "underlying"),
    (
        ("BTC-USDT-SWAP", "BTC/USDT:USDT", "BTC", "BTC-USDT"),
        ("ETH-USDT-SWAP", "ETH/USDT:USDT", "ETH", "ETH-USDT"),
        ("SOL-USDT-SWAP", "SOL/USDT:USDT", "SOL", "SOL-USDT"),
    ),
)
def test_public_metadata_supports_exact_frozen_multi_asset_targets(
    instrument: str,
    pair: str,
    base: str,
    underlying: str,
) -> None:
    urls: list[str] = []

    def fetch(url: str, _timeout: float) -> bytes:
        urls.append(url)
        rows = (
            [_instrument(instrument, underlying)]
            if url.startswith(INSTRUMENTS_URL + "?")
            else [_tier(1, "0", "1000", underlying)]
        )
        return json.dumps({"code": "0", "msg": "", "data": rows}).encode()

    payload = OkxPublicOfflineExchangeMetadataDownloader(
        fetch=fetch,
        sleep=lambda _: None,
    ).acquire(_request(instrument, pair), observed_at=NOW)
    observed = json.loads(payload.content)

    assert observed["markets"][pair]["base"] == base
    assert observed["markets"][pair]["baseId"] == base
    assert observed["markets"][pair]["id"] == instrument
    assert observed["leverage_tiers"][pair][0]["info"]["uly"] == underlying
    assert all(underlying.replace("-", "%2D") in url or underlying in url for url in urls)


def test_public_metadata_rejects_pair_instrument_mismatch() -> None:
    downloader = OkxPublicOfflineExchangeMetadataDownloader(
        fetch=lambda _url, _timeout: b"{}",
        sleep=lambda _: None,
    )

    with pytest.raises(OfflineExchangeMetadataBlocked) as raised:
        downloader.acquire(
            _request("ETH-USDT-SWAP", "BTC/USDT:USDT"),
            observed_at=NOW,
        )
    assert raised.value.code == "BLOCKED_OKX_METADATA_TARGET"


def test_metadata_freshness_and_tier_gaps_fail_closed() -> None:
    def fetch(url: str, _timeout: float) -> bytes:
        data = [_instrument()] if url.startswith(INSTRUMENTS_URL + "?") else [
            _tier(1, "0", "1000"),
            _tier(2, "1001", "5000"),
        ]
        return json.dumps({"code": "0", "msg": "", "data": data}).encode()

    downloader = OkxPublicOfflineExchangeMetadataDownloader(fetch=fetch, sleep=lambda _: None)
    with pytest.raises(OfflineExchangeMetadataBlocked) as raised:
        downloader.acquire(_request(), observed_at=NOW)
    assert raised.value.code == "BLOCKED_OKX_METADATA_TIERS"

    valid = OkxPublicOfflineExchangeMetadataDownloader(
        fetch=lambda url, _timeout: json.dumps(
            {
                "code": "0",
                "msg": "",
                "data": [_instrument()]
                if url.startswith(INSTRUMENTS_URL + "?")
                else [_tier(1, "0", "1000")],
            }
        ).encode(),
        sleep=lambda _: None,
    ).acquire(_request(), observed_at=NOW)
    with pytest.raises(OfflineExchangeMetadataBlocked) as stale:
        verify_offline_exchange_metadata(
            valid.content,
            expected_digest=valid.content_digest,
            observed_at=NOW + timedelta(seconds=3601),
        )
    assert stale.value.code == "BLOCKED_OFFLINE_METADATA_STALE"
