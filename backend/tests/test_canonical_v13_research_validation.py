from __future__ import annotations

import builtins
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.bundles import (
    activate_research_bundle,
    preview_research_bundle,
)
from app.canonical_v13.control_plane import (
    ConfigurationDependencyInput,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.intake import (
    ExternalSourceEntrySnapshot,
    ExternalVersionSnapshot,
    controlled_submit_latest,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.market import (
    MarketInspectionFacts,
    accept_market_artifact,
    create_market_profile_draft,
    seal_market_snapshot,
    validate_market_profile,
)
from app.canonical_v13.market_acquisition import (
    MarketAcquisitionPayload,
    MarketAcquisitionRequest,
    acquire_market_evidence,
)
from app.canonical_v13.models import (
    CONFIGURATION_ACTIVATIONS_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
    RESEARCH_GATE_RECEIPTS_TABLE,
    RESEARCH_TARGETS_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
    VALIDATION_PLAN_WINDOWS_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)
from app.canonical_v13.offline_exchange_metadata import (
    MEDIA_TYPE as OFFLINE_METADATA_MEDIA_TYPE,
    offline_exchange_metadata_receipt_digest,
)
from app.canonical_v13.research_gates import (
    CanonicalGateBlocked,
    claim_gate_attempt,
    create_gate_attempt,
    persist_lookahead_gate_receipt,
    persist_static_gate_receipt,
    read_gate_projection,
    recover_expired_gate_attempts,
)
from app.canonical_v13.research_validation import (
    CanonicalResearchValidationBlocked,
    ResearchLineage,
    build_ephemeral_launch_spec,
    build_lookahead_receipt,
    canonical_research_digest,
    declare_validation_plan,
    ephemeral_attempt_receipt_digest,
    mark_validation_plan_ready,
    record_terminal_attempt,
    simulate_ephemeral_attempt,
    start_validation_attempt,
    validate_ephemeral_launch_spec,
    validate_lookahead_receipt,
    validate_static_source,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
GATE_NOW = NOW + timedelta(hours=4)
SOURCE = (
    "from freqtrade.strategy import IStrategy\n"
    "class CanonicalFixtureStrategy(IStrategy):\n    pass\n"
)
SCHEMA = {"type": "object", "additionalProperties": False}
ADAPTER_DIGEST = "a" * 64
ADAPTER_MANIFEST_DIGEST = "b" * 64
STATIC_VALIDATOR_DIGEST = "c" * 64
LOOKAHEAD_ANALYZER_DIGEST = "d" * 64
EXECUTOR_IMAGE_DIGEST = "e" * 64


class _FixtureMarketDownloader:
    provenance_class = "TEST_SIMULATED"
    network_access = "NONE"
    credential_access = "NONE"

    def acquire(self, request: MarketAcquisitionRequest) -> MarketAcquisitionPayload:
        return MarketAcquisitionPayload(
            content=b"isolated phase6 market fixture",
            locator="fixtures/phase6-market.parquet",
            media_type="application/x-parquet",
            observed_first_open=NOW,
            observed_last_close=NOW + timedelta(hours=4),
            observed_closed_candles=48,
        )


@dataclass(frozen=True)
class PreparedResearch:
    lineage: ResearchLineage
    artifact_digest: str
    static_receipt: object
    lookahead_receipt: object
    plan_id: UUID
    plan_digest: str
    gate_attempt_id: UUID
    gate_lease_token: str
    gate_idempotency_key: str


@pytest.fixture
def canonical_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="phase6-validation-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    try:
        yield connection
    finally:
        raw.close()
        engine.dispose()


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _draft(connection, kind, payload, *, dependencies=()):
    return create_configuration_draft(
        connection,
        profile_key=f"phase6-{kind.lower()}",
        configuration_kind=kind,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        schema_json=SCHEMA,
        payload_json=payload,
        adapter_identity=f"{kind.lower()}-adapter-v1",
        adapter_digest=ADAPTER_DIGEST,
        dependencies=dependencies,
    )


def _dependency(draft, kind):
    return ConfigurationDependencyInput(
        version_id=draft.version_id,
        expected_kind=kind,
        relation_key=f"snapshot:{kind.lower()}",
    )


def _submit_strategy(connection):
    artifact = SOURCE.encode("utf-8")
    return controlled_submit_latest(
        connection,
        caller_identity="phase6-fixture",
        idempotency_key="phase6-fixture-entry",
        display_name="Canonical Phase 6 fixture",
        snapshot=ExternalSourceEntrySnapshot(
            archive_snapshot_digest="1" * 64,
            source_entry_key="strategies/canonical_fixture.py",
            source_strategy_key="canonical-fixture",
            current_version_id="source-version-1",
            versions=(
                ExternalVersionSnapshot(
                    source_strategy_key="canonical-fixture",
                    version_id="source-version-1",
                    version_number=1,
                    artifact_bytes=artifact,
                ),
            ),
        ),
    )


def _freeze_and_activate_bundle(connection):
    target = _draft(
        connection,
        "TARGET",
        {
            "targets": [
                {
                    "target_key": "btc-5m",
                    "instrument": "BTC-USDT-SWAP",
                    "pair": "BTC/USDT:USDT",
                    "timeframe": "5m",
                    "data_kind": "futures",
                }
            ]
        },
    )
    target_snapshot = validate_configuration_version(
        connection,
        version_id=target.version_id,
        adapter_manifest_digest=ADAPTER_MANIFEST_DIGEST,
    )
    windows_payload = {
        "windows": [
            {
                "window_key": "required-a",
                "required": True,
                "start_at": NOW.isoformat(),
                "end_at": (NOW + timedelta(hours=1)).isoformat(),
                "coverage": {
                    "minimum_closed_candles": 12,
                    "warmup_closed_candles": 2,
                    "integrity_margin_closed_candles": 1,
                    "freshness_max_age_seconds": 3600,
                },
            },
            {
                "window_key": "required-b",
                "required": True,
                "start_at": (NOW + timedelta(hours=1)).isoformat(),
                "end_at": (NOW + timedelta(hours=2)).isoformat(),
                "coverage": {
                    "minimum_closed_candles": 12,
                    "warmup_closed_candles": 2,
                    "integrity_margin_closed_candles": 1,
                    "freshness_max_age_seconds": 3600,
                },
            },
            {
                "window_key": "optional-c",
                "required": False,
                "start_at": (NOW + timedelta(hours=2)).isoformat(),
                "end_at": (NOW + timedelta(hours=3)).isoformat(),
                "coverage": {
                    "minimum_closed_candles": 12,
                    "warmup_closed_candles": 2,
                    "integrity_margin_closed_candles": 1,
                    "freshness_max_age_seconds": 3600,
                },
            },
        ]
    }
    window = _draft(connection, "WINDOW", windows_payload)
    window_snapshot = validate_configuration_version(
        connection,
        version_id=window.version_id,
        adapter_manifest_digest=ADAPTER_MANIFEST_DIGEST,
    )
    generation = _draft(
        connection,
        "GENERATION",
        {
            "allocations": [
                {
                    "target_key": "btc-5m",
                    "allocation_count": 1,
                    "candidate_cap": 1,
                }
            ]
        },
        dependencies=(_dependency(target, "TARGET"),),
    )
    generation_snapshot = validate_configuration_version(
        connection,
        version_id=generation.version_id,
        adapter_manifest_digest=ADAPTER_MANIFEST_DIGEST,
    )
    drafts = {"TARGET": target, "WINDOW": window, "GENERATION": generation}
    snapshots = {
        "TARGET": target_snapshot,
        "WINDOW": window_snapshot,
        "GENERATION": generation_snapshot,
    }
    payloads = {
        "DIVERSITY": {
            "rules": [
                {
                    "rule_key": "family",
                    "algorithm": "correlation-v1",
                    "metric": "return_correlation",
                    "operator": "<=",
                    "threshold": 0.8,
                }
            ]
        },
        "QUALITY_QUALIFICATION": {
            "minimum_score": 50,
            "required_window_gates": [
                {
                    "gate_key": "minimum-trades",
                    "metric": "trade_count",
                    "operator": ">=",
                    "threshold": 1,
                }
            ],
        },
        "SCORING": {
            "window_aggregation": "MINIMUM",
            "components": [
                {
                    "component_key": "profit-factor",
                    "metric": "profit_factor",
                    "weight": 1.0,
                    "direction": "maximize",
                    "minimum": 0.0,
                    "maximum": 3.0,
                }
            ],
        },
    }
    for kind, payload in payloads.items():
        draft = _draft(connection, kind, payload)
        drafts[kind] = draft
        snapshots[kind] = validate_configuration_version(
            connection,
            version_id=draft.version_id,
            adapter_manifest_digest=ADAPTER_MANIFEST_DIGEST,
        )
    aggregate = _draft(
        connection,
        "RESEARCH_AGGREGATE",
        {"assembly_key": "phase6-explicit"},
        dependencies=tuple(_dependency(drafts[kind], kind) for kind in drafts),
    )
    snapshots["RESEARCH_AGGREGATE"] = validate_configuration_version(
        connection,
        version_id=aggregate.version_id,
        adapter_manifest_digest=ADAPTER_MANIFEST_DIGEST,
    )
    snapshot_ids = {
        kind: snapshots[kind].snapshot_id for kind in P0_CONFIGURATION_KINDS
    }
    target_id = connection.execute(
        select(RESEARCH_TARGETS_TABLE.c.id).where(
            RESEARCH_TARGETS_TABLE.c.target_snapshot_id
            == target_snapshot.snapshot_id
        )
    ).scalar_one()
    metadata_observed_at = NOW + timedelta(hours=4)
    metadata_fresh_until = metadata_observed_at + timedelta(hours=1)
    metadata_facts = {
        "contract": "canonical-v13-okx-offline-exchange-metadata-v1",
        "source_identity": "okx-public-instruments-position-tiers-v1",
        "adapter_identity": "freqtrade-2026.6-ccxt-4.5.61-okx-offline-v1",
        "freqtrade_version": "2026.6",
        "ccxt_version": "4.5.61",
        "target_key": "btc-5m",
        "instrument": "BTC-USDT-SWAP",
        "pair": "BTC/USDT:USDT",
        "timeframe": "5m",
        "data_kind": "futures",
        "target_snapshot_id": str(target_snapshot.snapshot_id),
        "target_snapshot_digest": target_snapshot.snapshot_digest,
        "window_snapshot_id": str(window_snapshot.snapshot_id),
        "window_snapshot_digest": window_snapshot.snapshot_digest,
        "observed_at": metadata_observed_at.isoformat(),
        "fresh_until": metadata_fresh_until.isoformat(),
        "network_access": "PUBLIC_MARKET_DATA_ONLY",
        "credential_access": "NONE",
        "markets": {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT"}},
        "leverage_tiers": {"BTC/USDT:USDT": [{"minNotional": 0, "maxNotional": 1}]},
    }
    metadata_content = json.dumps(
        metadata_facts, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    metadata_digest = sha256(metadata_content).hexdigest()
    metadata_acquisition_receipt = offline_exchange_metadata_receipt_digest(
        content_digest=metadata_digest,
        observed_at=metadata_observed_at.isoformat(),
        fresh_until=metadata_fresh_until.isoformat(),
    )
    metadata_artifact_id = uuid4()
    metadata_inspection_id = uuid4()
    metadata_receipt_id = uuid4()
    metadata_locator = f"canonical_v13/fixture/exchange-metadata/{metadata_digest}.json"
    metadata_inspection = {
        "contract": "canonical-v13-offline-exchange-metadata-inspection-v1",
        "status": "ACCEPTED",
        "source_identity": "okx-public-instruments-position-tiers-v1",
        "provenance_class": "PRODUCTION_PUBLIC_EXCHANGE_METADATA",
        "target_key": "btc-5m",
        "instrument": "BTC-USDT-SWAP",
        "pair": "BTC/USDT:USDT",
        "timeframe": "5m",
        "data_kind": "futures",
        "target_snapshot_id": str(target_snapshot.snapshot_id),
        "target_snapshot_digest": target_snapshot.snapshot_digest,
        "window_snapshot_id": str(window_snapshot.snapshot_id),
        "window_snapshot_digest": window_snapshot.snapshot_digest,
        "observed_at": metadata_observed_at.isoformat(),
        "fresh_until": metadata_fresh_until.isoformat(),
        "market_count": 1,
        "leverage_tier_count": 1,
        "content_digest": metadata_digest,
        "acquisition_receipt_digest": metadata_acquisition_receipt,
        "network_access": "PUBLIC_MARKET_DATA_ONLY",
        "credential_access": "NONE",
    }
    metadata_inspection_digest = sha256(
        json.dumps(metadata_inspection, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    metadata_table_receipt_digest = sha256(
        json.dumps(
            {
                "artifact_digest": metadata_digest,
                "inspection_digest": metadata_inspection_digest,
                "status": "ACCEPTED",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    connection.execute(
        MARKET_ARTIFACTS_TABLE.insert().values(
            id=metadata_artifact_id,
            content_digest=metadata_digest,
            locator=metadata_locator,
            size_bytes=len(metadata_content),
            media_type=OFFLINE_METADATA_MEDIA_TYPE,
            created_at=metadata_observed_at,
        )
    )
    connection.execute(
        MARKET_INSPECTIONS_TABLE.insert().values(
            id=metadata_inspection_id,
            market_artifact_id=metadata_artifact_id,
            status="ACCEPTED",
            inspection_json=metadata_inspection,
            inspection_digest=metadata_inspection_digest,
            inspector_identity="canonical-v13-fixture-metadata-inspector-v1",
            created_at=metadata_observed_at,
        )
    )
    connection.execute(
        MARKET_RECEIPTS_TABLE.insert().values(
            id=metadata_receipt_id,
            market_artifact_id=metadata_artifact_id,
            market_inspection_id=metadata_inspection_id,
            status="ACCEPTED",
            artifact_digest=metadata_digest,
            inspection_digest=metadata_inspection_digest,
            receipt_digest=metadata_table_receipt_digest,
            created_at=metadata_observed_at,
        )
    )
    _profile_id, market_version_id, _payload_digest = create_market_profile_draft(
        connection,
        profile_key="phase6-market",
        scope_key="isolated-research",
        payload={
            "source": "isolated-fixture",
            "network_access": "NONE",
            "offline_exchange_metadata": {
                "artifact_id": str(metadata_artifact_id),
                "artifact_locator": metadata_locator,
                "artifact_digest": metadata_digest,
                "receipt_id": str(metadata_receipt_id),
                "receipt_digest": metadata_table_receipt_digest,
                "acquisition_receipt_digest": metadata_acquisition_receipt,
                "observed_at": metadata_observed_at.isoformat(),
                "fresh_until": metadata_fresh_until.isoformat(),
                "adapter_identity": "freqtrade-2026.6-ccxt-4.5.61-okx-offline-v1",
            },
        },
    )
    validate_market_profile(connection, version_id=market_version_id)
    acquisition_payload, acquisition_receipt = acquire_market_evidence(
        MarketAcquisitionRequest(
            source_identity="phase6-market-fixture-v1",
            target_key="btc-5m",
            instrument="BTC-USDT-SWAP",
            pair="BTC/USDT:USDT",
            timeframe="5m",
            data_kind="futures",
            requested_start=NOW,
            requested_end=NOW + timedelta(hours=4),
        ),
        downloader=_FixtureMarketDownloader(),
        observed_at=NOW + timedelta(hours=4),
    )
    evidence = accept_market_artifact(
        connection,
        locator=acquisition_payload.locator,
        content=acquisition_payload.content,
        media_type=acquisition_payload.media_type,
        inspector_identity="phase6-market-inspector-v1",
        facts=MarketInspectionFacts(
            row_count=48,
            first_open_at=NOW,
            last_close_at=NOW + timedelta(hours=4),
            gap_count=0,
            duplicate_count=0,
            null_count=0,
            monotonic=True,
            source_identity=acquisition_receipt.source_identity,
            provenance_class=acquisition_receipt.provenance_class,
            target_key=acquisition_receipt.target_key,
            instrument=acquisition_receipt.instrument,
            pair=acquisition_receipt.pair,
            timeframe=acquisition_receipt.timeframe,
            data_kind=acquisition_receipt.data_kind,
            acquired_at=NOW + timedelta(hours=4),
            acquisition_receipt_digest=acquisition_receipt.receipt_digest,
        ),
        acquisition_receipt=acquisition_receipt,
    )
    market_snapshot = seal_market_snapshot(
        connection,
        market_profile_version_id=market_version_id,
        members=(
            (
                evidence.artifact_id,
                evidence.receipt_id,
                target_id,
                NOW,
                NOW + timedelta(hours=4),
            ),
        ),
    )
    preview = preview_research_bundle(
        connection,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot.snapshot_id,
    )
    assert preview.status == "READY"
    assert preview.bundle_digest is not None
    assert preview.prospective_bundle_id is not None
    activation = activate_research_bundle(
        connection,
        scope_key="isolated-research",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot.snapshot_id,
        actor_identity="phase6-control-writer",
        expected_bundle_digest=preview.bundle_digest,
        expected_bundle_id=preview.prospective_bundle_id,
    )
    return (
        ResearchLineage(
            strategy_version_id=UUID(int=0),
            research_target_id=target_id,
            configuration_bundle_id=activation.configuration_bundle_id,
            configuration_bundle_digest=activation.bundle_digest,
            market_snapshot_id=market_snapshot.snapshot_id,
            market_snapshot_digest=market_snapshot.snapshot_digest,
        ),
        window_snapshot.snapshot_id,
    )


def _prepare_ready_plan(connection) -> PreparedResearch:
    intake = _submit_strategy(connection)
    lineage, _window_snapshot_id = _freeze_and_activate_bundle(connection)
    lineage = replace(lineage, strategy_version_id=intake.strategy_version_id)
    static_receipt = validate_static_source(
        SOURCE,
        strategy_version_id=intake.strategy_version_id,
        expected_artifact_digest=intake.artifact_digest,
        validator_identity="canonical-static-validator-v1",
        validator_digest=STATIC_VALIDATOR_DIGEST,
    )
    lookahead_receipt = build_lookahead_receipt(
        lineage=lineage,
        artifact_digest=intake.artifact_digest,
        analyzer_identity="canonical-lookahead-simulator-v1",
        analyzer_digest=LOOKAHEAD_ANALYZER_DIGEST,
        evidence_digest=canonical_research_digest(
            {"fixture": "explicit-lookahead-evidence", "has_bias": False}
        ),
        status="PASSED",
        has_bias=False,
        observed_signal_count=3,
    )
    gate_idempotency_key = (
        f"fixture-gate-primary-v3:{intake.strategy_version_id}:"
        f"{lineage.configuration_bundle_id}"
    )
    gate = create_gate_attempt(
        connection,
        lineage=lineage,
        idempotency_key=gate_idempotency_key,
        release_commit="1" * 40,
        executor_image_digest=EXECUTOR_IMAGE_DIGEST,
        worker_source_digest="f" * 64,
        observed_at=GATE_NOW,
    )
    lease = claim_gate_attempt(connection, gate_attempt_id=gate.gate_attempt_id, observed_at=GATE_NOW)
    persist_static_gate_receipt(
        connection,
        gate_attempt_id=gate.gate_attempt_id,
        lease_token=lease.lease_token,
        receipt=static_receipt,
        observed_at=GATE_NOW,
    )
    persist_lookahead_gate_receipt(
        connection,
        gate_attempt_id=gate.gate_attempt_id,
        lease_token=lease.lease_token,
        receipt=lookahead_receipt,
        observed_at=GATE_NOW,
    )
    rows = connection.execute(
        select(RESEARCH_GATE_RECEIPTS_TABLE).where(
            RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id == gate.gate_attempt_id
        )
    ).mappings().all()
    gate_ids = {row["gate_type"]: row["id"] for row in rows}
    plan = declare_validation_plan(
        connection,
        lineage=lineage,
        static_receipt=static_receipt,
        lookahead_receipt=lookahead_receipt,
        static_gate_receipt_id=gate_ids["STATIC"],
        lookahead_gate_receipt_id=gate_ids["LOOKAHEAD"],
        orchestrator_identity="canonical-research-orchestrator-v1",
    )
    ready = mark_validation_plan_ready(
        connection,
        validation_plan_id=plan.validation_plan_id,
        expected_plan_digest=plan.validation_plan_digest,
        static_receipt=static_receipt,
        lookahead_receipt=lookahead_receipt,
        static_gate_receipt_id=gate_ids["STATIC"],
        lookahead_gate_receipt_id=gate_ids["LOOKAHEAD"],
        orchestrator_identity="canonical-research-orchestrator-v1",
    )
    assert ready.status == "READY"
    return PreparedResearch(
        lineage=lineage,
        artifact_digest=intake.artifact_digest,
        static_receipt=static_receipt,
        lookahead_receipt=lookahead_receipt,
        plan_id=plan.validation_plan_id,
        plan_digest=plan.validation_plan_digest,
        gate_attempt_id=gate.gate_attempt_id,
        gate_lease_token=lease.lease_token,
        gate_idempotency_key=gate_idempotency_key,
    )


def test_planless_gate_receipts_survive_pointer_churn_and_recovery_is_fail_closed(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        before = read_gate_projection(
            canonical_connection, gate_attempt_id=prepared.gate_attempt_id
        )
        assert before.validation_eligible is True
        replayed_attempt = create_gate_attempt(
            canonical_connection,
            lineage=prepared.lineage,
            idempotency_key=prepared.gate_idempotency_key,
            release_commit="1" * 40,
            executor_image_digest=EXECUTOR_IMAGE_DIGEST,
            worker_source_digest="f" * 64,
            observed_at=GATE_NOW + timedelta(hours=2),
        )
        assert replayed_attempt.repeat_noop is True
        assert replayed_attempt.gate_attempt_id == prepared.gate_attempt_id
        with pytest.raises(CanonicalGateBlocked) as attempt_conflict:
            create_gate_attempt(
                canonical_connection,
                lineage=prepared.lineage,
                idempotency_key=prepared.gate_idempotency_key,
                release_commit="2" * 40,
                executor_image_digest=EXECUTOR_IMAGE_DIGEST,
                worker_source_digest="f" * 64,
                observed_at=GATE_NOW + timedelta(minutes=1),
            )
        assert attempt_conflict.value.code == "BLOCKED_GATE_IDEMPOTENCY_CONFLICT"
        receipt_count = _count(canonical_connection, RESEARCH_GATE_RECEIPTS_TABLE)
        assert persist_static_gate_receipt(
            canonical_connection,
            gate_attempt_id=prepared.gate_attempt_id,
            lease_token=prepared.gate_lease_token,
            receipt=prepared.static_receipt,
            observed_at=GATE_NOW + timedelta(minutes=1),
        ) == before.static_receipt_digest
        assert persist_lookahead_gate_receipt(
            canonical_connection,
            gate_attempt_id=prepared.gate_attempt_id,
            lease_token=prepared.gate_lease_token,
            receipt=prepared.lookahead_receipt,
            observed_at=GATE_NOW + timedelta(minutes=1),
        ) == before.lookahead_receipt_digest
        assert _count(canonical_connection, RESEARCH_GATE_RECEIPTS_TABLE) == receipt_count
        conflicting = validate_static_source(
            SOURCE,
            strategy_version_id=prepared.lineage.strategy_version_id,
            expected_artifact_digest=prepared.artifact_digest,
            validator_identity="canonical-static-validator-v2",
            validator_digest="9" * 64,
        )
        with pytest.raises(CanonicalGateBlocked) as conflict:
            persist_static_gate_receipt(
                canonical_connection,
                gate_attempt_id=prepared.gate_attempt_id,
                lease_token=prepared.gate_lease_token,
                receipt=conflicting,
                observed_at=GATE_NOW + timedelta(minutes=1),
            )
        assert conflict.value.code == "BLOCKED_GATE_IDEMPOTENCY_CONFLICT"
        orphan = create_gate_attempt(
            canonical_connection,
            lineage=prepared.lineage,
            idempotency_key=f"fixture-gate-orphan-v3:{prepared.lineage.strategy_version_id}",
            release_commit="2" * 40,
            executor_image_digest=EXECUTOR_IMAGE_DIGEST,
            worker_source_digest="f" * 64,
            observed_at=GATE_NOW,
        )
        claim_gate_attempt(
            canonical_connection, gate_attempt_id=orphan.gate_attempt_id, observed_at=GATE_NOW
        )
        canonical_connection.execute(
            CONFIGURATION_ACTIVATIONS_TABLE.update().values(scope_key="superseded-pointer")
        )
        after = read_gate_projection(
            canonical_connection, gate_attempt_id=prepared.gate_attempt_id
        )
        assert after == before
        receipt_rows = canonical_connection.execute(
            select(RESEARCH_GATE_RECEIPTS_TABLE).where(
                RESEARCH_GATE_RECEIPTS_TABLE.c.gate_attempt_id
                == prepared.gate_attempt_id
            )
        ).mappings().all()
        receipt_ids = {row["gate_type"]: row["id"] for row in receipt_rows}
        replayed_plan = declare_validation_plan(
            canonical_connection,
            lineage=prepared.lineage,
            static_receipt=prepared.static_receipt,
            lookahead_receipt=prepared.lookahead_receipt,
            static_gate_receipt_id=receipt_ids["STATIC"],
            lookahead_gate_receipt_id=receipt_ids["LOOKAHEAD"],
            orchestrator_identity="canonical-research-orchestrator-v1",
        )
        assert replayed_plan.repeat_noop is True
        assert replayed_plan.validation_plan_id == prepared.plan_id
        assert recover_expired_gate_attempts(
            canonical_connection, observed_at=GATE_NOW + timedelta(minutes=21)
        ) == 1
        recovered = read_gate_projection(
            canonical_connection, gate_attempt_id=orphan.gate_attempt_id
        )
        assert recovered.status == "BLOCKED"
        assert recovered.terminal_reason_code == "GATE_LEASE_EXPIRED"
        assert recovered.validation_eligible is False


def _start(connection, prepared):
    spec = build_ephemeral_launch_spec(
        connection,
        validation_plan_id=prepared.plan_id,
        expected_plan_digest=prepared.plan_digest,
        executor_identity="canonical-ephemeral-simulator-v1",
        executor_image_digest=EXECUTOR_IMAGE_DIGEST,
    )
    return start_validation_attempt(connection, launch_spec=spec)


def _metrics():
    return {
        "required-a": {"trade_count": 2, "profit_factor": 1.4},
        "required-b": {"trade_count": 3, "profit_factor": 1.2},
    }


def test_static_validator_only_parses_ast_and_never_imports_source(monkeypatch) -> None:
    source = "value = __import__('module_that_must_not_load')\n"
    imported: list[str] = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    receipt = validate_static_source(
        source,
        strategy_version_id=uuid4(),
        expected_artifact_digest=sha256(source.encode()).hexdigest(),
        validator_identity="canonical-static-validator-v1",
        validator_digest=STATIC_VALIDATOR_DIGEST,
    )

    assert receipt.status == "FAILED"
    assert {finding.rule_id for finding in receipt.findings} == {
        "unsafe.dynamic_execution"
    }
    assert "module_that_must_not_load" not in imported


def test_static_validator_detects_lookahead_and_digest_drift() -> None:
    source = "def indicators(frame):\n    return frame.shift(-1), frame.iloc[-1]\n"
    receipt = validate_static_source(
        source,
        strategy_version_id=uuid4(),
        expected_artifact_digest=sha256(source.encode()).hexdigest(),
        validator_identity="canonical-static-validator-v1",
        validator_digest=STATIC_VALIDATOR_DIGEST,
    )
    assert receipt.status == "FAILED"
    assert {finding.rule_id for finding in receipt.findings} == {
        "lookahead.shift_negative",
        "lookahead.iloc_negative",
    }
    with pytest.raises(CanonicalResearchValidationBlocked) as raised:
        validate_static_source(
            source,
            strategy_version_id=uuid4(),
            expected_artifact_digest="f" * 64,
            validator_identity="canonical-static-validator-v1",
            validator_digest=STATIC_VALIDATOR_DIGEST,
        )
    assert raised.value.code == "BLOCKED_ARTIFACT_DIGEST_DRIFT"


def test_lookahead_validator_requires_explicit_unbiased_digest_bound_receipt() -> None:
    lineage = ResearchLineage(
        strategy_version_id=uuid4(),
        research_target_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="1" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="2" * 64,
    )
    receipt = build_lookahead_receipt(
        lineage=lineage,
        artifact_digest="3" * 64,
        analyzer_identity="lookahead-simulator-v1",
        analyzer_digest=LOOKAHEAD_ANALYZER_DIGEST,
        evidence_digest="4" * 64,
        status="PASSED",
        has_bias=False,
        observed_signal_count=2,
    )
    assert validate_lookahead_receipt(
        receipt,
        expected_lineage=lineage,
        expected_artifact_digest="3" * 64,
    ).status == "PASSED"

    biased = build_lookahead_receipt(
        lineage=lineage,
        artifact_digest="3" * 64,
        analyzer_identity="lookahead-simulator-v1",
        analyzer_digest=LOOKAHEAD_ANALYZER_DIGEST,
        evidence_digest="4" * 64,
        status="FAILED",
        has_bias=True,
        observed_signal_count=2,
    )
    assert validate_lookahead_receipt(
        biased,
        expected_lineage=lineage,
        expected_artifact_digest="3" * 64,
    ).status == "FAILED"
    insufficient = build_lookahead_receipt(
        lineage=lineage,
        artifact_digest="3" * 64,
        analyzer_identity="lookahead-simulator-v1",
        analyzer_digest=LOOKAHEAD_ANALYZER_DIGEST,
        evidence_digest="4" * 64,
        status="BLOCKED",
        has_bias=None,
        observed_signal_count=0,
        failure_stage="OUTPUT_INTERPRETATION",
        failure_code="LOOKAHEAD_INSUFFICIENT_TRADES",
        tool_return_code=0,
        stdout_digest="5" * 64,
        stderr_digest="6" * 64,
        redacted_detail="Freqtrade observed fewer trades than required",
        blocked_observed_trade_count=3,
        blocked_required_trade_count=10,
    )
    insufficient_decision = validate_lookahead_receipt(
        insufficient,
        expected_lineage=lineage,
        expected_artifact_digest="3" * 64,
    )
    assert insufficient_decision.status == "BLOCKED"
    assert insufficient_decision.reason_codes == (
        "LOOKAHEAD_INSUFFICIENT_TRADES",
        "LOOKAHEAD_EVIDENCE_BLOCKED",
        "LOOKAHEAD_OBSERVATIONS_UNSET",
    )
    with pytest.raises(CanonicalResearchValidationBlocked) as raised:
        validate_lookahead_receipt(
            replace(receipt, evidence_digest="5" * 64),
            expected_lineage=lineage,
            expected_artifact_digest="3" * 64,
        )
    assert raised.value.code == "BLOCKED_LOOKAHEAD_RECEIPT_DIGEST_DRIFT"


def test_plan_copies_dynamic_window_member_ids_and_records_exact_required_results(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        plan_windows = canonical_connection.execute(
            select(VALIDATION_PLAN_WINDOWS_TABLE).where(
                VALIDATION_PLAN_WINDOWS_TABLE.c.validation_plan_id == prepared.plan_id
            )
        ).mappings().all()
        snapshot_member_ids = set(
            canonical_connection.execute(
                select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.id).where(
                    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%")
                )
            ).scalars()
        )
        running = _start(canonical_connection, prepared)
        receipt = simulate_ephemeral_attempt(
            running, metrics_by_window_key=_metrics()
        )
        terminal = record_terminal_attempt(canonical_connection, receipt=receipt)

    assert len(plan_windows) == 3
    assert {row["window_snapshot_member_id"] for row in plan_windows} == snapshot_member_ids
    assert sum(bool(row["required"]) for row in plan_windows) == 2
    assert terminal.attempt_status == "SUCCEEDED"
    assert terminal.plan_status == "COMPLETE"
    assert terminal.window_result_count == 2
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 2


def test_launch_spec_rejects_any_network_credential_exchange_order_or_writer_capability(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        spec = build_ephemeral_launch_spec(
            canonical_connection,
            validation_plan_id=prepared.plan_id,
            expected_plan_digest=prepared.plan_digest,
            executor_identity="canonical-ephemeral-simulator-v1",
            executor_image_digest=EXECUTOR_IMAGE_DIGEST,
        )
    assert validate_ephemeral_launch_spec(spec)
    unsafe_specs = (
        replace(spec, container_class="LONG_LIVED_TRADING_RUNTIME"),
        replace(spec, filesystem_mode="PERSISTENT"),
        replace(spec, long_lived_runtime=True),
        replace(spec, network_mode="bridge"),
        replace(spec, credential_mounts=("secret",)),
        replace(spec, exchange_capabilities=("exchange-client",)),
        replace(spec, order_capabilities=("submit-order",)),
        replace(spec, writer_capabilities=("canonical_validation_writer",)),
        replace(spec, order_submission=True),
    )
    for unsafe in unsafe_specs:
        with pytest.raises(CanonicalResearchValidationBlocked) as raised:
            validate_ephemeral_launch_spec(unsafe)
        assert raised.value.code == "BLOCKED_EXECUTOR_CAPABILITY"


def test_missing_required_window_is_blocked_without_terminal_writes(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        running = _start(canonical_connection, prepared)
        with pytest.raises(CanonicalResearchValidationBlocked) as raised:
            simulate_ephemeral_attempt(
                running,
                metrics_by_window_key={"required-a": _metrics()["required-a"]},
            )
        attempt_status = canonical_connection.execute(
            select(VALIDATION_ATTEMPTS_TABLE.c.status).where(
                VALIDATION_ATTEMPTS_TABLE.c.id == running.validation_attempt_id
            )
        ).scalar_one()
        plan_status = canonical_connection.execute(
            select(VALIDATION_PLANS_TABLE.c.status).where(
                VALIDATION_PLANS_TABLE.c.id == prepared.plan_id
            )
        ).scalar_one()

    assert raised.value.code == "BLOCKED_REQUIRED_WINDOW_RESULT_SET"
    assert attempt_status == "RUNNING"
    assert plan_status == "RUNNING"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 0


def test_recorder_rejects_mixed_lineage_and_exact_missing_set(canonical_connection) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        running = _start(canonical_connection, prepared)
        receipt = simulate_ephemeral_attempt(
            running, metrics_by_window_key=_metrics()
        )
        mixed = replace(
            receipt,
            lineage=replace(receipt.lineage, research_target_id=uuid4()),
        )
        with pytest.raises(CanonicalResearchValidationBlocked) as mixed_raised:
            record_terminal_attempt(canonical_connection, receipt=mixed)

        missing = replace(receipt, window_results=receipt.window_results[:1])
        missing = replace(
            missing,
            receipt_digest=ephemeral_attempt_receipt_digest(missing),
        )
        with pytest.raises(CanonicalResearchValidationBlocked) as missing_raised:
            record_terminal_attempt(canonical_connection, receipt=missing)

    assert mixed_raised.value.code == "BLOCKED_MIXED_LINEAGE"
    assert missing_raised.value.code == "BLOCKED_REQUIRED_WINDOW_RESULT_SET"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 0


def test_terminal_attempt_is_immutable_even_for_identical_receipt(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        running = _start(canonical_connection, prepared)
        receipt = simulate_ephemeral_attempt(
            running, metrics_by_window_key=_metrics()
        )
        record_terminal_attempt(canonical_connection, receipt=receipt)
        with pytest.raises(CanonicalResearchValidationBlocked) as raised:
            record_terminal_attempt(canonical_connection, receipt=receipt)

    assert raised.value.code == "BLOCKED_TERMINAL_ATTEMPT_REWRITE"
    assert _count(canonical_connection, VALIDATION_WINDOW_RESULTS_TABLE) == 2


def test_plan_without_persisted_gate_pair_fails_closed_before_writes(canonical_connection) -> None:
    with canonical_connection.begin():
        intake = _submit_strategy(canonical_connection)
        lineage, _window_snapshot_id = _freeze_and_activate_bundle(canonical_connection)
        lineage = replace(lineage, strategy_version_id=intake.strategy_version_id)
        canonical_connection.execute(
            CONFIGURATION_ACTIVATIONS_TABLE.update().values(scope_key="inactive-scope")
        )
        static_receipt = validate_static_source(
            SOURCE,
            strategy_version_id=intake.strategy_version_id,
            expected_artifact_digest=intake.artifact_digest,
            validator_identity="canonical-static-validator-v1",
            validator_digest=STATIC_VALIDATOR_DIGEST,
        )
        lookahead_receipt = build_lookahead_receipt(
            lineage=lineage,
            artifact_digest=intake.artifact_digest,
            analyzer_identity="canonical-lookahead-simulator-v1",
            analyzer_digest=LOOKAHEAD_ANALYZER_DIGEST,
            evidence_digest="9" * 64,
            status="PASSED",
            has_bias=False,
            observed_signal_count=1,
        )
        with pytest.raises(CanonicalResearchValidationBlocked) as raised:
            declare_validation_plan(
                canonical_connection,
                lineage=lineage,
                static_receipt=static_receipt,
                lookahead_receipt=lookahead_receipt,
                static_gate_receipt_id=uuid4(),
                lookahead_gate_receipt_id=uuid4(),
                orchestrator_identity="canonical-research-orchestrator-v1",
            )

    assert raised.value.code == "BLOCKED_GATE_RECEIPT_UNAVAILABLE"
    assert _count(canonical_connection, VALIDATION_PLANS_TABLE) == 0
    assert _count(canonical_connection, VALIDATION_PLAN_WINDOWS_TABLE) == 0
    MARKET_ARTIFACTS_TABLE,
    MARKET_INSPECTIONS_TABLE,
    MARKET_RECEIPTS_TABLE,
