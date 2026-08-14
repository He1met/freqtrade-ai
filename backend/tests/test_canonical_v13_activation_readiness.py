from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.canonical_v13.activation_readiness import (
    MarketTargetEvidence,
    RequiredWindowEvidence,
    assess_persisted_bundle_activation_readiness,
    assess_production_activation_readiness,
)
from app.canonical_v13.bundles import (
    activate_research_bundle,
    preview_research_bundle,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.market_acquisition import (
    CanonicalMarketAcquisitionBlocked,
    MarketAcquisitionPayload,
    MarketAcquisitionRequest,
    acquire_market_evidence,
)
from tests.test_canonical_v13_bundles import _freeze_bundle_inputs


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


class IsolatedDownloader:
    provenance_class = "TEST_SIMULATED"
    network_access = "NONE"
    credential_access = "NONE"

    def acquire(self, request):
        return MarketAcquisitionPayload(
            content=b"isolated market fixture",
            locator="fixtures/BTC-USDT-SWAP-5m.parquet",
            media_type="application/x-parquet",
            observed_first_open=request.requested_start,
            observed_last_close=request.requested_end,
            observed_closed_candles=72,
        )


def _window() -> RequiredWindowEvidence:
    return RequiredWindowEvidence(
        window_key="dynamic-coverage-window",
        required=True,
        start_at=NOW - timedelta(hours=6),
        end_at=NOW,
        minimum_closed_candles=72,
    )


def _market(**changes) -> MarketTargetEvidence:
    evidence = MarketTargetEvidence(
        target_key="btc-5m",
        instrument="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        data_kind="futures",
        coverage_start=NOW - timedelta(hours=6),
        coverage_end=NOW,
        inspection_first_open=NOW - timedelta(hours=6),
        inspection_last_close=NOW,
        inspection_row_count=72,
        acquired_at=NOW - timedelta(minutes=1),
        provenance_class="PRODUCTION_PUBLIC_MARKET_DATA",
        acquisition_receipt_valid=True,
    )
    return replace(evidence, **changes)


def _assess(evidence: MarketTargetEvidence):
    return assess_production_activation_readiness(
        target_facts={
            "btc-5m": (
                "BTC-USDT-SWAP",
                "BTC/USDT:USDT",
                "5m",
                "futures",
            )
        },
        required_windows=(_window(),),
        market_evidence=(evidence,),
        evaluated_at=NOW,
        maximum_age=timedelta(hours=1),
    )


def test_exact_production_evidence_can_be_ready_but_fixture_cannot() -> None:
    ready = _assess(_market())
    simulated = _assess(_market(provenance_class="TEST_SIMULATED"))

    assert ready.status == "READY"
    assert ready.reason_codes == ()
    assert simulated.status == "BLOCKED"
    assert simulated.reason_codes == ("MARKET_PROVENANCE_NOT_PRODUCTION",)


def test_required_window_coverage_count_and_freshness_are_independent_gates() -> None:
    result = _assess(
        _market(
            coverage_start=NOW - timedelta(hours=5),
            inspection_first_open=NOW - timedelta(hours=5),
            inspection_row_count=71,
            acquired_at=NOW - timedelta(hours=2),
        )
    )

    assert result.status == "BLOCKED"
    assert set(result.reason_codes) == {
        "REQUIRED_WINDOW_COVERAGE_MISSING:dynamic-coverage-window",
        "REQUIRED_WINDOW_CANDLE_COUNT_LOW:dynamic-coverage-window",
        "MARKET_EVIDENCE_STALE",
    }


def test_snapshot_claim_cannot_disagree_with_inspection() -> None:
    result = _assess(
        _market(inspection_last_close=NOW - timedelta(minutes=5))
    )
    assert "MARKET_INSPECTION_COVERAGE_MISMATCH" in result.reason_codes


def test_invalid_required_window_is_blocked_without_datetime_exception() -> None:
    invalid = replace(_window(), start_at=NOW.replace(tzinfo=None))
    result = assess_production_activation_readiness(
        target_facts={
            "btc-5m": ("BTC-USDT-SWAP", "BTC/USDT:USDT", "5m", "futures")
        },
        required_windows=(invalid,),
        market_evidence=(_market(),),
        evaluated_at=NOW,
        maximum_age=timedelta(hours=1),
    )
    assert result.status == "BLOCKED"
    assert result.reason_codes == ("REQUIRED_WINDOW_CONTRACT_INVALID",)


def test_injected_acquisition_port_emits_test_receipt_without_network() -> None:
    request = MarketAcquisitionRequest(
        source_identity="isolated-fixture-port-v1",
        target_key="btc-5m",
        instrument="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        data_kind="futures",
        requested_start=NOW - timedelta(hours=6),
        requested_end=NOW,
    )
    payload, receipt = acquire_market_evidence(
        request, downloader=IsolatedDownloader(), observed_at=NOW
    )

    assert payload.content == b"isolated market fixture"
    assert receipt.status == "ACCEPTED"
    assert receipt.provenance_class == "TEST_SIMULATED"
    assert receipt.network_access == "NONE"
    assert receipt.credential_access == "NONE"
    assert len(receipt.receipt_digest) == 64
    assert _assess(
        _market(
            provenance_class=receipt.provenance_class,
            acquired_at=datetime.fromisoformat(receipt.acquired_at),
        )
    ).status == "BLOCKED"


def test_acquisition_receipt_rejects_partial_requested_coverage() -> None:
    class PartialDownloader(IsolatedDownloader):
        def acquire(self, request):
            payload = super().acquire(request)
            return replace(
                payload,
                observed_first_open=request.requested_start + timedelta(minutes=5),
            )

    request = MarketAcquisitionRequest(
        source_identity="isolated-fixture-port-v1",
        target_key="btc-5m",
        instrument="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        data_kind="futures",
        requested_start=NOW - timedelta(hours=6),
        requested_end=NOW,
    )
    with pytest.raises(CanonicalMarketAcquisitionBlocked) as blocked:
        acquire_market_evidence(request, downloader=PartialDownloader(), observed_at=NOW)
    assert blocked.value.code == "BLOCKED_MARKET_ACQUISITION_EMPTY"


def test_acquisition_rejects_credential_or_exchange_network_capability() -> None:
    request = MarketAcquisitionRequest(
        source_identity="unsafe-port",
        target_key="btc-5m",
        instrument="BTC-USDT-SWAP",
        pair="BTC/USDT:USDT",
        timeframe="5m",
        data_kind="futures",
        requested_start=NOW - timedelta(hours=6),
        requested_end=NOW,
    )
    unsafe_type = type(
        "UnsafeDownloader",
        (),
        {
            "provenance_class": "UNKNOWN",
            "network_access": "EXCHANGE_PRIVATE_API",
            "credential_access": "MOUNTED",
            "acquire": lambda self, value: None,
        },
    )
    try:
        acquire_market_evidence(request, downloader=unsafe_type(), observed_at=NOW)
    except CanonicalMarketAcquisitionBlocked as exc:
        assert exc.code == "BLOCKED_MARKET_CREDENTIAL_CAPABILITY"
    else:
        raise AssertionError("unsafe acquisition capability was accepted")


def test_persisted_simulator_bundle_cannot_be_production_ready() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    try:
        with raw.begin():
            install_canonical_genesis(raw, installer_identity="phase7-readiness-test")
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        with connection.begin():
            snapshot_ids, market_snapshot_id = _freeze_bundle_inputs(connection)
            preview = preview_research_bundle(
                connection,
                scope_key="isolated-research",
                workflow_key="RESEARCH",
                snapshot_ids=snapshot_ids,
                market_snapshot_id=market_snapshot_id,
            )
            activation = activate_research_bundle(
                connection,
                scope_key="isolated-research",
                workflow_key="RESEARCH",
                snapshot_ids=snapshot_ids,
                market_snapshot_id=market_snapshot_id,
                actor_identity="isolated-phase7-operator",
                expected_bundle_digest=preview.bundle_digest,
                expected_bundle_id=preview.prospective_bundle_id,
            )
            readiness = assess_persisted_bundle_activation_readiness(
                connection,
                configuration_bundle_id=activation.configuration_bundle_id,
                expected_bundle_digest=activation.bundle_digest,
                evaluated_at=NOW,
                maximum_age=timedelta(hours=1),
            )
        assert readiness.status == "BLOCKED"
        assert set(readiness.reason_codes) >= {
            "MARKET_PROVENANCE_NOT_PRODUCTION",
            "MARKET_ACQUISITION_RECEIPT_INVALID",
            "MARKET_ACQUISITION_RECEIPT_UNSET",
        }
    finally:
        raw.close()
        engine.dispose()
