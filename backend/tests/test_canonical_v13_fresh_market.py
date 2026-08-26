from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from urllib.error import URLError

import pytest
from sqlalchemy import create_engine

from app.canonical_v13 import okx_public_market
from app.canonical_v13.control_plane import (
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.market_acquisition import (
    CanonicalMarketAcquisitionBlocked,
    MarketAcquisitionRequest,
    MarketAcquisitionPayload,
    acquire_market_evidence,
    verify_market_acquisition_receipt,
)
from app.canonical_v13.fresh_market_rollout import (
    CanonicalFreshMarketRolloutBlocked,
    acquire_register_and_seal_fresh_market,
    persist_immutable_market_artifact,
)
from app.canonical_v13.models import (
    MARKET_ARTIFACTS_TABLE,
    MARKET_RECEIPTS_TABLE,
    MARKET_SNAPSHOTS_TABLE,
)
from sqlalchemy import func, select
from app.canonical_v13.market_planning import (
    CanonicalMarketPlanningBlocked,
    plan_fresh_market_acquisition,
)
from app.canonical_v13.okx_public_market import (
    OKX_HISTORY_CANDLES_URL,
    OkxPublicHistoryCandleDownloader,
)
from app.canonical_v13.offline_exchange_metadata import (
    OfflineExchangeMetadataPayload,
)


DIGEST = "a" * 64
MANIFEST = "b" * 64


@pytest.fixture
def canonical_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="fresh-market-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    try:
        yield connection
    finally:
        raw.close()
        engine.dispose()


def _snapshot(connection, kind: str, payload: dict):
    draft = create_configuration_draft(
        connection,
        profile_key=f"fresh-market-{kind.lower()}",
        configuration_kind=kind,
        scope_key="production-research-v13",
        workflow_key="research",
        schema_json={"type": "object", "additionalProperties": False},
        payload_json=payload,
        adapter_identity="fresh-market-contract-v1",
        adapter_digest=DIGEST,
    )
    return validate_configuration_version(
        connection,
        version_id=draft.version_id,
        adapter_manifest_digest=MANIFEST,
    )


def _target_payload() -> dict:
    return {
        "targets": [
            {
                "target_key": "btc-usdt-swap-15m",
                "instrument": "BTC-USDT-SWAP",
                "pair": "BTC/USDT:USDT",
                "timeframe": "15m",
                "data_kind": "futures",
            }
        ]
    }


def _window_payload(*, include_margins: bool = True) -> dict:
    coverage = {"minimum_closed_candles": 4}
    if include_margins:
        coverage.update(
            {
                "warmup_closed_candles": 2,
                "integrity_margin_closed_candles": 1,
                "freshness_max_age_seconds": 3600,
            }
        )
    return {
        "windows": [
            {
                "window_key": "required-hour",
                "required": True,
                "start_at": "2026-08-14T00:00:00+00:00",
                "end_at": "2026-08-14T01:00:00+00:00",
                "coverage": coverage,
            }
        ]
    }


def test_range_is_derived_from_exact_frozen_target_and_window(canonical_connection) -> None:
    with canonical_connection.begin():
        target = _snapshot(canonical_connection, "TARGET", _target_payload())
        window = _snapshot(canonical_connection, "WINDOW", _window_payload())
        plan = plan_fresh_market_acquisition(
            canonical_connection,
            target_snapshot_id=target.snapshot_id,
            expected_target_snapshot_digest=target.snapshot_digest,
            window_snapshot_id=window.snapshot_id,
            expected_window_snapshot_digest=window.snapshot_digest,
            target_key="btc-usdt-swap-15m",
        )
    assert plan.requested_start.isoformat() == "2026-08-13T23:15:00+00:00"
    assert plan.requested_end.isoformat() == "2026-08-14T01:00:00+00:00"
    assert plan.warmup_closed_candles == 2
    assert plan.integrity_margin_closed_candles == 1
    assert plan.freshness_max_age_seconds == 3600


def test_missing_window_margins_and_snapshot_drift_block(canonical_connection) -> None:
    with canonical_connection.begin():
        target = _snapshot(canonical_connection, "TARGET", _target_payload())
        window = _snapshot(
            canonical_connection, "WINDOW", _window_payload(include_margins=False)
        )
        with pytest.raises(CanonicalMarketPlanningBlocked) as missing:
            plan_fresh_market_acquisition(
                canonical_connection,
                target_snapshot_id=target.snapshot_id,
                expected_target_snapshot_digest=target.snapshot_digest,
                window_snapshot_id=window.snapshot_id,
                expected_window_snapshot_digest=window.snapshot_digest,
                target_key="btc-usdt-swap-15m",
            )
        assert missing.value.code == "BLOCKED_WINDOW_ACQUISITION_MARGIN_UNSET"
        with pytest.raises(CanonicalMarketPlanningBlocked) as drift:
            plan_fresh_market_acquisition(
                canonical_connection,
                target_snapshot_id=target.snapshot_id,
                expected_target_snapshot_digest="0" * 64,
                window_snapshot_id=window.snapshot_id,
                expected_window_snapshot_digest=window.snapshot_digest,
                target_key="btc-usdt-swap-15m",
            )
        assert drift.value.code == "BLOCKED_CONFIGURATION_SNAPSHOT_DRIFT"


def _row(opened_at_ms: int) -> list[str]:
    return [
        str(opened_at_ms),
        "100",
        "102",
        "99",
        "101",
        "10",
        "1",
        "1000",
        "1",
    ]


def _request(
    instrument: str = "BTC-USDT-SWAP",
    pair: str = "BTC/USDT:USDT",
) -> MarketAcquisitionRequest:
    return MarketAcquisitionRequest(
        source_identity="okx-public-history-candles-v1",
        target_key=instrument.lower() + "-15m",
        instrument=instrument,
        pair=pair,
        timeframe="15m",
        data_kind="futures",
        requested_start=datetime(2026, 8, 14, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 14, 1, tzinfo=timezone.utc),
    )


def test_public_adapter_is_paged_deterministic_and_receipted() -> None:
    observed_urls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        observed_urls.append(url)
        assert timeout == 3
        rows = [_row(1786668300000), _row(1786667400000)] if len(observed_urls) == 1 else [
            _row(1786666500000),
            _row(1786665600000),
        ]
        return json.dumps({"code": "0", "msg": "", "data": rows}).encode()

    sleeps: list[float] = []
    downloader = OkxPublicHistoryCandleDownloader(
        fetch=fetch,
        sleep=sleeps.append,
        timeout_seconds=3,
        page_limit=2,
    )
    payload, receipt = acquire_market_evidence(
        _request(),
        downloader=downloader,
        observed_at=datetime(2026, 8, 14, 2, tzinfo=timezone.utc),
    )
    assert len(observed_urls) == 2
    assert all(url.startswith(OKX_HISTORY_CANDLES_URL + "?") for url in observed_urls)
    assert all("apiKey" not in url and "sign" not in url for url in observed_urls)
    assert sleeps == [0.11]
    assert payload.observed_closed_candles == 4
    assert payload.locator.endswith(receipt.content_digest + ".jsonl")
    assert verify_market_acquisition_receipt(receipt) is True
    assert [json.loads(line)["opened_at"] for line in payload.content.splitlines()] == [
        "2026-08-14T00:00:00+00:00",
        "2026-08-14T00:15:00+00:00",
        "2026-08-14T00:30:00+00:00",
        "2026-08-14T00:45:00+00:00",
    ]


def test_public_fetch_does_not_require_content_length_and_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_sizes: list[int] = []

    class ResponseWithoutContentLength:
        headers: dict[str, str] = {}

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, size: int) -> bytes:
            requested_sizes.append(size)
            return self.payload[:size]

    monkeypatch.setattr(
        okx_public_market,
        "urlopen",
        lambda _request, timeout: ResponseWithoutContentLength(b"{}"),
    )
    assert okx_public_market._default_fetch("https://www.okx.com/public", 3) == b"{}"
    assert requested_sizes == [okx_public_market.MAXIMUM_RESPONSE_BYTES + 1]

    monkeypatch.setattr(
        okx_public_market,
        "urlopen",
        lambda _request, timeout: ResponseWithoutContentLength(
            b"x" * (okx_public_market.MAXIMUM_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(CanonicalMarketAcquisitionBlocked) as raised:
        okx_public_market._default_fetch("https://www.okx.com/public", 3)
    assert raised.value.code == "BLOCKED_OKX_RESPONSE_TOO_LARGE"


@pytest.mark.parametrize(
    ("instrument", "pair"),
    (
        ("BTC-USDT-SWAP", "BTC/USDT:USDT"),
        ("ETH-USDT-SWAP", "ETH/USDT:USDT"),
        ("SOL-USDT-SWAP", "SOL/USDT:USDT"),
    ),
)
def test_public_adapter_supports_exact_frozen_multi_asset_targets(
    instrument: str,
    pair: str,
) -> None:
    observed_urls: list[str] = []

    def fetch(url: str, _timeout: float) -> bytes:
        observed_urls.append(url)
        return json.dumps(
            {
                "code": "0",
                "msg": "",
                "data": [
                    _row(1786668300000),
                    _row(1786667400000),
                    _row(1786666500000),
                    _row(1786665600000),
                ],
            }
        ).encode()

    payload = OkxPublicHistoryCandleDownloader(
        fetch=fetch,
        sleep=lambda _: None,
    ).acquire(_request(instrument, pair))

    assert payload.observed_closed_candles == 4
    assert f"/{instrument}/15m/" in payload.locator
    assert all(f"instId={instrument}" in url for url in observed_urls)


def test_public_adapter_rejects_pair_instrument_mismatch() -> None:
    downloader = OkxPublicHistoryCandleDownloader(
        fetch=lambda _url, _timeout: b"{}",
        sleep=lambda _: None,
    )

    with pytest.raises(CanonicalMarketAcquisitionBlocked) as raised:
        downloader.acquire(_request("ETH-USDT-SWAP", "BTC/USDT:USDT"))
    assert raised.value.code == "BLOCKED_OKX_PUBLIC_TARGET"


def test_finite_retry_and_gap_fail_closed() -> None:
    attempts = 0

    def transient_then_gap(url: str, timeout: float) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise URLError("offline")
        return json.dumps(
            {"code": "0", "msg": "", "data": [_row(1786668300000), _row(1786665600000)]}
        ).encode()

    sleeps: list[float] = []
    downloader = OkxPublicHistoryCandleDownloader(
        fetch=transient_then_gap,
        sleep=sleeps.append,
        maximum_attempts=3,
    )
    with pytest.raises(CanonicalMarketAcquisitionBlocked) as raised:
        downloader.acquire(_request())
    assert raised.value.code == "BLOCKED_OKX_CANDLE_GAP"
    assert attempts == 3
    assert sleeps == [1, 2]


class _CompleteFakeDownloader:
    provenance_class = "PRODUCTION_PUBLIC_MARKET_DATA"
    network_access = "PUBLIC_MARKET_DATA_ONLY"
    credential_access = "NONE"

    def acquire(self, request: MarketAcquisitionRequest) -> MarketAcquisitionPayload:
        content = b'{"closed":"candle"}\n'
        digest = sha256(content).hexdigest()
        return MarketAcquisitionPayload(
            content=content,
            locator=f"canonical_v13/fake/range-{digest}.jsonl",
            media_type="application/x-ndjson",
            observed_first_open=request.requested_start,
            observed_last_close=request.requested_end,
            observed_closed_candles=7,
        )


class _CompleteFakeMetadataDownloader:
    provenance_class = "PRODUCTION_PUBLIC_EXCHANGE_METADATA"
    network_access = "PUBLIC_MARKET_DATA_ONLY"
    credential_access = "NONE"

    def acquire(self, request, *, observed_at):
        content = b'{"contract":"test-offline-exchange-metadata"}'
        digest = sha256(content).hexdigest()
        acquired = datetime(2026, 8, 14, 1, 30, tzinfo=timezone.utc)
        return OfflineExchangeMetadataPayload(
            content=content,
            locator=f"canonical_v13/fake/exchange-metadata/{digest}.json",
            content_digest=digest,
            observed_at=acquired,
            fresh_until=acquired + timedelta(hours=1),
            market_count=1,
            leverage_tier_count=2,
            receipt_digest="f" * 64,
        )


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_rollout_is_immutable_append_only_and_idempotent(
    canonical_connection, tmp_path
) -> None:
    with canonical_connection.begin():
        target = _snapshot(canonical_connection, "TARGET", _target_payload())
        window = _snapshot(canonical_connection, "WINDOW", _window_payload())
        plan = plan_fresh_market_acquisition(
            canonical_connection,
            target_snapshot_id=target.snapshot_id,
            expected_target_snapshot_digest=target.snapshot_digest,
            window_snapshot_id=window.snapshot_id,
            expected_window_snapshot_digest=window.snapshot_digest,
            target_key="btc-usdt-swap-15m",
        )
        first = acquire_register_and_seal_fresh_market(
            canonical_connection,
            plan=plan,
            downloader=_CompleteFakeDownloader(),
            artifact_root=tmp_path,
            observed_at=datetime(2026, 8, 14, 1, 45, tzinfo=timezone.utc),
            profile_key="fresh-okx-btc-15m",
            scope_key="production-research-v13",
            inspector_identity="canonical-v13-market-inspector-v1",
            metadata_downloader=_CompleteFakeMetadataDownloader(),
        )
        replay = acquire_register_and_seal_fresh_market(
            canonical_connection,
            plan=plan,
            downloader=_CompleteFakeDownloader(),
            artifact_root=tmp_path,
            observed_at=datetime(2026, 8, 14, 1, 30, tzinfo=timezone.utc),
            profile_key="fresh-okx-btc-15m",
            scope_key="production-research-v13",
            inspector_identity="canonical-v13-market-inspector-v1",
            metadata_downloader=_CompleteFakeMetadataDownloader(),
        )
        assert _count(canonical_connection, MARKET_ARTIFACTS_TABLE) == 2
        assert _count(canonical_connection, MARKET_RECEIPTS_TABLE) == 2
        assert _count(canonical_connection, MARKET_SNAPSHOTS_TABLE) == 1
    assert first.database_replay is False
    assert replay.database_replay is True
    assert replay.artifact_file_replay is True
    assert first.market_snapshot_id == replay.market_snapshot_id


def test_artifact_symlink_and_staleness_fail_closed(
    canonical_connection, tmp_path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    content = b"x"
    digest = sha256(content).hexdigest()
    with pytest.raises(CanonicalFreshMarketRolloutBlocked) as unsafe:
        persist_immutable_market_artifact(
            root=tmp_path,
            locator=f"linked/item-{digest}.jsonl",
            content=content,
        )
    assert unsafe.value.code == "BLOCKED_MARKET_ARTIFACT_PATH"

    with canonical_connection.begin():
        target = _snapshot(canonical_connection, "TARGET", _target_payload())
        window = _snapshot(canonical_connection, "WINDOW", _window_payload())
        plan = plan_fresh_market_acquisition(
            canonical_connection,
            target_snapshot_id=target.snapshot_id,
            expected_target_snapshot_digest=target.snapshot_digest,
            window_snapshot_id=window.snapshot_id,
            expected_window_snapshot_digest=window.snapshot_digest,
            target_key="btc-usdt-swap-15m",
        )
        with pytest.raises(CanonicalFreshMarketRolloutBlocked) as stale:
            acquire_register_and_seal_fresh_market(
                canonical_connection,
                plan=plan,
                downloader=_CompleteFakeDownloader(),
                artifact_root=tmp_path,
                observed_at=datetime(2026, 8, 14, 3, tzinfo=timezone.utc),
                profile_key="stale-okx-btc-15m",
                scope_key="production-research-v13",
                inspector_identity="canonical-v13-market-inspector-v1",
                metadata_downloader=_CompleteFakeMetadataDownloader(),
            )
        assert stale.value.code == "BLOCKED_MARKET_EVIDENCE_STALE"
        assert _count(canonical_connection, MARKET_ARTIFACTS_TABLE) == 1
