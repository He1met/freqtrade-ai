from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.canonical_v13.market_acquisition import MarketAcquisitionPayload
from app.canonical_v13.phase9_production_composition import (
    CanonicalPhase9CompositionBlocked,
)
from app.canonical_v13.phase9_production_runtime import (
    DatabaseSignalReceiptWriter,
    DatabaseRuntimeLineageReader,
    FrozenIntradayLeverageEvaluator,
    PersistingRuntimeWorker,
    PublicOkxRuntimeMarketEvidence,
    ReleaseBoundReceiptSeal,
)
from app.canonical_v13.phase9_runtime_supervisor import (
    Phase9LaunchPlan,
    RuntimeImagePlanAuthority,
    build_launch_plan,
)
from app.canonical_v13.phase9_runtime_worker import natural_market_evidence_digest


NOW = datetime(2026, 8, 21, 6, 7, tzinfo=timezone.utc)
SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260821_06"
    / "canonical_intraday_leverage_run_1_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260822_07"
    / "canonical_four_hour_natural_long_run_2_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_08"
    / "canonical_four_hour_natural_long_acceptance_run_1_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_FINAL_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_09"
    / "canonical_four_hour_natural_long_acceptance_run_2_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_RETRY_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_10"
    / "canonical_four_hour_natural_long_acceptance_run_3_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_FINAL_RETRY_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_11"
    / "canonical_four_hour_natural_long_acceptance_run_4_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_FINAL_CANARY_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_12"
    / "canonical_four_hour_natural_long_acceptance_run_5_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_FINAL_CANARY_RETRY_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_13"
    / "canonical_four_hour_natural_long_acceptance_run_6_1.py"
).read_text(encoding="utf-8")
FOUR_HOUR_ORDER_PREPARE_REQUALIFICATION_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "canonical_v13_qualified_baselines"
    / "batch_20260825_14"
    / "canonical_four_hour_natural_long_acceptance_run_7_1.py"
).read_text(encoding="utf-8")


def _uuid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row


class _Connection:
    def __init__(self, rows):
        self._rows = list(rows)

    def execute(self, _statement):
        return _Result(self._rows.pop(0))


def _factory(connection):
    @contextmanager
    def factory():
        yield connection

    return factory


def _candles() -> list[dict[str, str]]:
    rows = []
    for index in range(1):
        close = 100 + index * 0.05 + (0.4 if index % 2 else -0.3)
        rows.append(
            {
                "open": str(close - 0.1),
                "high": str(close + 0.6),
                "low": str(close - 0.6),
                "close": str(close),
                "volume": "10",
                "opened_at": NOW.replace(hour=3, minute=0).isoformat(),
            }
        )
    return rows


def test_frozen_evaluator_uses_exact_artifact_and_closed_candles() -> None:
    assert sha256(SOURCE.encode()).hexdigest() == (
        "d5682ed00a4755afabd612ef86404c0663152104525010fcc2268c08d4659ac7"
    )
    lineage = SimpleNamespace(
        strategy_artifact_source=SOURCE,
        strategy_artifact_digest=sha256(SOURCE.encode()).hexdigest(),
    )
    evidence = SimpleNamespace(
        payload={"closed_candles": _candles()}, observed_at=NOW
    )
    evaluation = FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
        lineage=lineage, evidence=evidence
    )
    assert evaluation.outcome in {"SIGNAL", "NO_ACTION"}
    assert evaluation.evaluator_identity == "canonical-intraday-leverage-baseline-v1"
    assert evaluation.evaluation_payload["direction"] == "LONG"
    assert evaluation.evaluation_payload["artifact_digest"] == lineage.strategy_artifact_digest

    with pytest.raises(
        CanonicalPhase9CompositionBlocked, match="BLOCKED_PHASE9_EVALUATOR_ARTIFACT"
    ):
        FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
            lineage=SimpleNamespace(
                strategy_artifact_source=SOURCE + "\n# drift",
                strategy_artifact_digest=lineage.strategy_artifact_digest,
            ),
            evidence=evidence,
        )
    trend_source = (
        Path(__file__).resolve().parents[2]
        / "research"
        / "canonical_v13_qualified_baselines"
        / "batch_20260820_01"
        / "canonical_trend_pullback_run_1_1.py"
    ).read_text(encoding="utf-8")
    with pytest.raises(
        CanonicalPhase9CompositionBlocked, match="BLOCKED_PHASE9_EVALUATOR_ARTIFACT"
    ):
        FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
            lineage=SimpleNamespace(
                strategy_artifact_source=trend_source,
                strategy_artifact_digest=sha256(trend_source.encode()).hexdigest(),
            ),
            evidence=evidence,
        )


@pytest.mark.parametrize(
    ("source", "expected_digest", "expected_evaluator_identity"),
    (
        (
            FOUR_HOUR_SOURCE,
            "d8f0ed49773f061e43730bd328ef919602a18499bfb5c12377358f5906cbb7bd",
            "canonical-four-hour-natural-long-baseline-v1",
        ),
        (
            FOUR_HOUR_REQUALIFICATION_SOURCE,
            "ff423436d2de18052d9e6a113e71ee7ea2915d30218ed4b8a974354483bc07a2",
            "canonical-four-hour-natural-long-baseline-v2",
        ),
        (
            FOUR_HOUR_FINAL_REQUALIFICATION_SOURCE,
            "390ee1a74a7e5dbe125c5b69438791b44f4b4802ab8b6870c10031464cd80c3d",
            "canonical-four-hour-natural-long-baseline-v3",
        ),
        (
            FOUR_HOUR_RETRY_REQUALIFICATION_SOURCE,
            "cebadac26170922228acacc033f9d6e3ab24603ab0ebca4431775abe8f914d85",
            "canonical-four-hour-natural-long-baseline-v4",
        ),
        (
            FOUR_HOUR_FINAL_RETRY_REQUALIFICATION_SOURCE,
            "b454acfa6576ec4e83fb7d7ed69762ad5bdca3f06c636836b4781954b0c3496c",
            "canonical-four-hour-natural-long-baseline-v5",
        ),
        (
            FOUR_HOUR_FINAL_CANARY_REQUALIFICATION_SOURCE,
            "feca7b0112580b91edebdba6ef8f07899309ad41293069fed7ad169d58260c73",
            "canonical-four-hour-natural-long-baseline-v6",
        ),
        (
            FOUR_HOUR_FINAL_CANARY_RETRY_REQUALIFICATION_SOURCE,
            "dc1e70a1e0b0fb91e162a5cac374316fcc1a615fd8eb5734fa21f55fb459d7f4",
            "canonical-four-hour-natural-long-baseline-v7",
        ),
        (
            FOUR_HOUR_ORDER_PREPARE_REQUALIFICATION_SOURCE,
            "03a76a9230cfc7dd74e348b3424f22c61136452f974bdcd699bb51b15150b469",
            "canonical-four-hour-natural-long-baseline-v8",
        ),
    ),
)
def test_frozen_evaluator_accepts_exact_four_hour_qualified_artifact(
    source: str, expected_digest: str, expected_evaluator_identity: str
) -> None:
    digest = sha256(source.encode()).hexdigest()
    assert digest == expected_digest
    lineage = SimpleNamespace(
        strategy_artifact_source=source,
        strategy_artifact_digest=digest,
    )
    candles = _candles()
    candles[0]["opened_at"] = NOW.replace(hour=6, minute=0).isoformat()
    evidence = SimpleNamespace(
        payload={"closed_candles": candles}, observed_at=NOW
    )

    evaluation = FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
        lineage=lineage, evidence=evidence
    )

    assert evaluation.outcome == "SIGNAL"
    assert evaluation.evaluator_identity == expected_evaluator_identity
    assert evaluation.evaluation_payload["effective_strategy_leverage"] == "12"
    assert evaluation.evaluation_payload["artifact_digest"] == digest

    candles[0]["opened_at"] = NOW.replace(hour=3, minute=0).isoformat()
    no_action = FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
        lineage=lineage, evidence=evidence
    )
    assert no_action.outcome == "NO_ACTION"

    with pytest.raises(
        CanonicalPhase9CompositionBlocked, match="BLOCKED_PHASE9_EVALUATOR_ARTIFACT"
    ):
        FrozenIntradayLeverageEvaluator().evaluate_natural_signal(
            lineage=SimpleNamespace(
                strategy_artifact_source=source + "\n# drift",
                strategy_artifact_digest=digest,
            ),
            evidence=evidence,
        )


def test_public_market_port_is_credential_free_and_digest_sealed() -> None:
    candles = _candles()
    content = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for row in candles
    )

    class Downloader:
        provenance_class = "PRODUCTION_PUBLIC_MARKET_DATA"
        network_access = "PUBLIC_MARKET_DATA_ONLY"
        credential_access = "NONE"

        def __init__(self):
            self.request = None

        def acquire(self, request):
            self.request = request
            return MarketAcquisitionPayload(
                content=content,
                locator="canonical-public-evidence",
                media_type="application/x-ndjson",
                observed_first_open=request.requested_start,
                observed_last_close=request.requested_end,
                observed_closed_candles=1,
            )

    downloader = Downloader()
    evidence = PublicOkxRuntimeMarketEvidence(downloader).read_market_evidence(
        lineage=SimpleNamespace(
            target_instrument="BTC-USDT-SWAP",
            target_pair="BTC/USDT:USDT",
            target_timeframe="15m",
            target_data_kind="futures",
        ),
        observed_at=NOW,
    )
    assert downloader.request.instrument == "BTC-USDT-SWAP"
    assert downloader.request.requested_end.minute % 15 == 0
    assert evidence.payload["credential_access"] == "NONE"
    assert evidence.evidence_digest == natural_market_evidence_digest(evidence)


def _runtime_lineage_plan() -> Phase9LaunchPlan:
    return build_launch_plan(
        service_key="long_lived_runtime",
        stage="SIGNAL_RISK_SHADOW",
        generation=1,
        prepared_at=NOW - timedelta(seconds=5),
        release_digest="1" * 64,
        deployment_id=_uuid(1),
        deployment_capability_digest="2" * 64,
        runtime_image_authority=RuntimeImagePlanAuthority(
            acceptance_id=_uuid(99),
            image_manifest_digest="3" * 64,
            image_config_digest="5" * 64,
            acceptance_receipt_digest="4" * 64,
            release_digest="1" * 64,
        ),
    )


def _runtime_lineage_rows() -> list[dict[str, object]]:
    return [
        {
            "id": _uuid(1),
            "deployment_approval_id": _uuid(2),
            "strategy_version_id": _uuid(3),
            "configuration_bundle_id": _uuid(4),
            "configuration_bundle_digest": "4" * 64,
            "market_snapshot_id": _uuid(5),
            "market_snapshot_digest": "5" * 64,
            "status": "ACTIVE",
            "demo_only": True,
            "allow_real_funds": False,
            "capability_digest": "2" * 64,
        },
        {
            "id": _uuid(6),
            "status": "HEALTHY",
            "runtime_identity": "canonical-v13-long-lived-runtime-v1",
            "image_digest": "3" * 64,
            "service_account": "canonical_runtime_reader",
            "order_writer_capability": False,
            "launch_spec_digest": "6" * 64,
        },
        {
            "status": "HEALTHY",
            "evidence_class": "PRODUCTION_DEMO_RUNTIME",
            "launch_spec_digest": "6" * 64,
            "capability_digest": "2" * 64,
            "receipt_digest": "7" * 64,
            "observed_at": NOW - timedelta(seconds=2),
        },
        {
            "id": _uuid(2),
            "status": "APPROVED",
            "strategy_version_id": _uuid(3),
            "qualification_decision_id": _uuid(7),
            "approval_digest": "8" * 64,
        },
        {
            "id": _uuid(7),
            "status": "QUALIFIED",
            "strategy_version_id": _uuid(3),
            "research_target_id": _uuid(8),
            "configuration_bundle_id": _uuid(4),
            "configuration_bundle_digest": "4" * 64,
            "market_snapshot_id": _uuid(5),
            "market_snapshot_digest": "5" * 64,
            "decision_digest": "9" * 64,
        },
        {
            "id": _uuid(3),
            "artifact_id": _uuid(9),
            "validation_status": "UNVALIDATED",
            "execution_authorized": False,
        },
        {
            "id": _uuid(9),
            # PostgreSQL preserves the canonical production artifact spelling.
            "encoding": "UTF-8",
            "normalized_content": SOURCE,
            "content_digest": sha256(SOURCE.encode()).hexdigest(),
        },
        {
            "id": _uuid(8),
            "instrument": "BTC-USDT-SWAP",
            "pair": "BTC/USDT:USDT",
            "timeframe": "15m",
            "data_kind": "futures",
        },
        {
            "id": _uuid(4),
            "bundle_digest": "4" * 64,
            "market_snapshot_id": _uuid(5),
            "market_snapshot_digest": "5" * 64,
        },
    ]


def test_runtime_reader_accepts_exact_immutable_qualification_lineage() -> None:
    lineage = DatabaseRuntimeLineageReader(
        _factory(_Connection(_runtime_lineage_rows())), _runtime_lineage_plan()
    ).read_active_runtime_lineage()
    assert lineage.qualification_decision_id == _uuid(7)
    assert lineage.strategy_artifact_digest == sha256(SOURCE.encode()).hexdigest()
    assert lineage.target_instrument == "BTC-USDT-SWAP"
    assert lineage.runtime_order_writer_capability is False


def test_runtime_reader_reports_only_exact_stopped_observation_as_pending() -> None:
    rows = _runtime_lineage_rows()
    rows[1]["status"] = "STOPPED"
    rows[2]["status"] = "STOPPED"
    rows[2]["evidence_class"] = "PRODUCTION_DEMO_RUNTIME_STOP"
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_OBSERVATION_PENDING",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_reader_allows_exact_stopped_image_rollover_as_pending() -> None:
    rows = _runtime_lineage_rows()
    rows[1]["status"] = "STOPPED"
    rows[1]["image_digest"] = "a" * 64
    rows[2]["status"] = "STOPPED"
    rows[2]["evidence_class"] = "PRODUCTION_DEMO_RUNTIME_STOP"
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_OBSERVATION_PENDING",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_reader_does_not_mask_stopped_receipt_drift_as_pending() -> None:
    rows = _runtime_lineage_rows()
    rows[1]["status"] = "STOPPED"
    rows[1]["image_digest"] = "a" * 64
    rows[2]["status"] = "STOPPED"
    rows[2]["evidence_class"] = "PRODUCTION_DEMO_RUNTIME_STOP"
    rows[2]["launch_spec_digest"] = "b" * 64
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_reader_rejects_non_utf8_artifact_encoding() -> None:
    rows = _runtime_lineage_rows()
    rows[6]["encoding"] = "UTF-16"
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_reader_rejects_non_qualified_decision() -> None:
    rows = _runtime_lineage_rows()
    rows[4]["status"] = "REJECTED"
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_reader_rejects_qualification_bundle_lineage_drift() -> None:
    rows = _runtime_lineage_rows()
    rows[4]["configuration_bundle_digest"] = "a" * 64
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE",
    ):
        DatabaseRuntimeLineageReader(
            _factory(_Connection(rows)), _runtime_lineage_plan()
        ).read_active_runtime_lineage()


def test_runtime_receipt_seal_requires_dedicated_hmac_key() -> None:
    seal = ReleaseBoundReceiptSeal("1" * 64, "runtime-signer-key-" + "x" * 64)
    signature = seal.sign_digest("2" * 64)
    assert seal.key_id.startswith("canonical-signal-receipt-v1:")
    assert "runtime-signer-key" not in seal.key_id
    assert seal.verify_digest(
        key_id=seal.key_id,
        algorithm="HMAC_SHA256_V1",
        digest="2" * 64,
        signature=signature,
    )
    other = ReleaseBoundReceiptSeal("1" * 64, "different-signer-key-" + "y" * 64)
    assert not other.verify_digest(
        key_id=seal.key_id,
        algorithm="HMAC_SHA256_V1",
        digest="2" * 64,
        signature=signature,
    )


def test_persisting_worker_exposes_the_same_verifier_used_for_persistence() -> None:
    receipt = SimpleNamespace(receipt_digest="1" * 64)

    class Worker:
        def heartbeat(self, **_kwargs):
            return receipt

    class Writer:
        def __init__(self) -> None:
            self.persisted = []
            self.verified = []

        def persist(self, candidate) -> None:
            self.persisted.append(candidate)

        def verify(self, candidate) -> bool:
            self.verified.append(candidate)
            return candidate is receipt

    writer = Writer()
    worker = PersistingRuntimeWorker(Worker(), writer)

    observed = worker.heartbeat(
        stage="SIGNAL_RISK_SHADOW", plan_digest="a" * 64, observed_at=NOW
    )

    assert observed is receipt
    assert writer.persisted == [receipt]
    assert worker.verify(receipt) is True
    tampered = SimpleNamespace(receipt_digest="2" * 64)
    assert worker.verify(tampered) is False
    assert writer.verified == [receipt, tampered]


def _signed_signal_receipt(*, observed_at: datetime, evaluated_at: datetime):
    return SimpleNamespace(
        observed_at=observed_at,
        signal_candidate=SimpleNamespace(
            deployment_id=_uuid(1),
            runtime_instance_id=_uuid(2),
            research_target_id=_uuid(3),
            signal_json={"runtime_receipt_digest": "4" * 64},
            evaluated_at=evaluated_at,
        ),
    )


def test_signal_writer_uses_fresh_signed_worker_receipt_as_liveness(
    monkeypatch,
) -> None:
    captured = {}
    receipt = _signed_signal_receipt(observed_at=NOW, evaluated_at=NOW)
    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_runtime.verify_runtime_worker_receipt",
        lambda observed, *, verifier: observed is receipt and verifier == "seal",
    )

    def record(_connection, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_runtime.record_production_demo_signal",
        record,
    )
    DatabaseSignalReceiptWriter(
        _factory(object()),
        "seal",
        clock=lambda: NOW + timedelta(seconds=30),
    ).persist(receipt)

    assert captured["runtime_liveness_observed_at"] == NOW
    assert captured["evaluated_at"] == NOW
    assert captured["signal_json"]["runtime_receipt_digest"] == "4" * 64


@pytest.mark.parametrize(
    ("observed_at", "evaluated_at", "clock"),
    (
        (NOW, NOW, NOW + timedelta(minutes=3)),
        (NOW + timedelta(seconds=1), NOW + timedelta(seconds=1), NOW),
        (NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=30)),
    ),
)
def test_signal_writer_rejects_stale_future_or_time_drifted_worker_receipt(
    monkeypatch, observed_at, evaluated_at, clock
) -> None:
    receipt = _signed_signal_receipt(
        observed_at=observed_at, evaluated_at=evaluated_at
    )
    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_runtime.verify_runtime_worker_receipt",
        lambda _receipt, *, verifier: verifier == "seal",
    )
    writer = DatabaseSignalReceiptWriter(
        _factory(object()), "seal", clock=lambda: clock
    )

    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_SIGNAL_RECEIPT_FRESHNESS",
    ):
        writer.persist(receipt)
