from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.canonical_v13.phase9_production_composition import (
    CanonicalPhase9CompositionBlocked,
    DatabaseOrderWriterAuthorityVerifier,
    compose_supervise_ports,
    confirm_running_runtime_from_supervisor,
    confirm_stopped_runtime_from_supervisor,
    record_current_canary_attestation,
    record_current_canary_probe_receipt,
)
from app.canonical_v13.phase9_canary_policy import CanaryProbeReceiptResult
from app.canonical_v13.phase9_execution_authority import (
    RedactedExecutionAttestationResult,
)
from app.canonical_v13.phase9_runtime_supervisor import (
    Phase9Lease,
    RuntimeImagePlanAuthority,
    build_launch_plan,
    build_lifecycle_receipt,
    build_order_writer_canary_authority,
)


NOW = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
IMAGE_ACCEPTANCE_ID = UUID("00000000-0000-4000-8000-000000000099")
IMAGE_ACCEPTANCE_RECEIPT = "9" * 64


def _uuid(value: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{value:012d}")


def _runtime_image_authority() -> RuntimeImagePlanAuthority:
    return RuntimeImagePlanAuthority(
        acceptance_id=IMAGE_ACCEPTANCE_ID,
        image_manifest_digest="7" * 64,
        image_config_digest="8" * 64,
        acceptance_receipt_digest=IMAGE_ACCEPTANCE_RECEIPT,
        release_digest="6" * 64,
    )


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _Connection:
    def __init__(self, rows):
        self.rows = list(rows)

    def execute(self, _statement):
        return _Result(self.rows.pop(0))


def _factory(connection):
    @contextmanager
    def open_connection():
        yield connection

    return open_connection


def _writer_plan():
    authority = build_order_writer_canary_authority(
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        execution_canary_risk_policy_id=_uuid(2),
        execution_canary_risk_policy_digest="2" * 64,
        attestation_id=_uuid(3),
        attestation_digest="3" * 64,
        attestation_expires_at=NOW + timedelta(seconds=30),
        instrument_metadata_digest="4" * 64,
        mark_price_snapshot_digest="5" * 64,
        strategy_max_leverage="12",
        effective_leverage="12",
    )
    return build_launch_plan(
        service_key="order_writer",
        stage="OKX_DEMO_CANARY",
        generation=1,
        prepared_at=NOW,
        release_digest="6" * 64,
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        order_writer_enabled=True,
        order_writer_canary_authority=authority,
    )


def test_order_supervise_composition_never_returns_none_authority() -> None:
    plan = _writer_plan()
    ports = compose_supervise_ports(
        plan,
        runtime_worker_factory=None,
        order_connection_factory=_factory(_Connection([])),
    )
    assert ports.worker_port is None
    assert isinstance(ports.authority_port, DatabaseOrderWriterAuthorityVerifier)


def test_runtime_composition_fails_closed_without_sealed_factory() -> None:
    plan = build_launch_plan(
        service_key="long_lived_runtime",
        stage="SIGNAL_RISK_SHADOW",
        generation=1,
        prepared_at=NOW,
        release_digest="6" * 64,
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        runtime_image_authority=_runtime_image_authority(),
    )
    with pytest.raises(
        CanonicalPhase9CompositionBlocked,
        match="BLOCKED_PHASE9_RUNTIME_COMPOSITION_UNSET",
    ):
        compose_supervise_ports(
            plan, runtime_worker_factory=None, order_connection_factory=None
        )


def test_database_authority_verifier_recomputes_all_frozen_sources() -> None:
    plan = _writer_plan()
    authority = plan.order_writer_canary_authority
    assert authority is not None
    connection = _Connection(
        [
            {
                "status": "ACTIVE",
                "demo_only": True,
                "allow_real_funds": False,
                "capability_digest": "1" * 64,
                "deployment_approval_id": _uuid(4),
            },
            {
                "deployment_approval_id": _uuid(4),
                "execution_attestation_id": _uuid(3),
                "probe_receipt_id": _uuid(7),
                "policy_digest": "2" * 64,
                "status": "ACTIVE",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "position_policy": "LONG_ONLY",
                "strategy_max_leverage": "12.000",
                "effective_leverage": "12.000",
                "instrument": "BTC-USDT-SWAP",
                "metadata_receipt_digest": "4" * 64,
                "mark_price_receipt_digest": "5" * 64,
                "accepted_at": NOW + timedelta(seconds=25) - timedelta(minutes=30),
                "expires_at": NOW + timedelta(seconds=25),
            },
            {
                "deployment_id": _uuid(1),
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "permissions_json": {"read": True, "trade": True, "withdraw": False},
                "attestation_digest": "3" * 64,
                "observed_at": NOW - timedelta(seconds=1),
                "expires_at": NOW + timedelta(seconds=30),
            },
            {
                "id": _uuid(7),
                "execution_attestation_id": _uuid(3),
                "deployment_id": _uuid(1),
                "execution_target": "OKX_DEMO",
                "instrument": "BTC-USDT-SWAP",
                "allow_real_funds": False,
                "simulated_trading": True,
                "instrument_digest": "4" * 64,
                "mark_price_digest": "5" * 64,
                "observed_at": NOW - timedelta(seconds=1),
                "expires_at": NOW + timedelta(seconds=20),
                **{
                    f"{prefix}_{suffix}": value
                    for prefix in (
                        "instrument",
                        "mark_price",
                        "account_config",
                        "leverage",
                        "exchange_max_leverage",
                    )
                    for suffix, value in (
                        ("observed_at", NOW - timedelta(seconds=1)),
                        ("expires_at", NOW + timedelta(seconds=20)),
                    )
                },
            },
        ]
    )
    assert DatabaseOrderWriterAuthorityVerifier(_factory(connection)).verify(
        authority, observed_at=NOW
    )


def test_database_authority_verifier_accepts_expired_frozen_probe_lineage() -> None:
    historical = NOW - timedelta(minutes=1)
    authority = build_order_writer_canary_authority(
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        execution_canary_risk_policy_id=_uuid(2),
        execution_canary_risk_policy_digest="2" * 64,
        attestation_id=_uuid(3),
        attestation_digest="3" * 64,
        attestation_expires_at=historical,
        instrument_metadata_digest="4" * 64,
        mark_price_snapshot_digest="5" * 64,
        strategy_max_leverage="12",
        effective_leverage="12",
    )
    connection = _Connection(
        [
            {
                "status": "ACTIVE",
                "demo_only": True,
                "allow_real_funds": False,
                "capability_digest": "1" * 64,
                "deployment_approval_id": _uuid(4),
            },
            {
                "deployment_approval_id": _uuid(4),
                "execution_attestation_id": _uuid(3),
                "probe_receipt_id": _uuid(7),
                "policy_digest": "2" * 64,
                "status": "ACTIVE",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "position_policy": "LONG_ONLY",
                "strategy_max_leverage": "12",
                "effective_leverage": "12",
                "instrument": "BTC-USDT-SWAP",
                "metadata_receipt_digest": "4" * 64,
                "mark_price_receipt_digest": "5" * 64,
                "accepted_at": historical - timedelta(minutes=30),
                "expires_at": historical,
            },
            {
                "deployment_id": _uuid(1),
                "status": "READY",
                "execution_target": "OKX_DEMO",
                "allow_real_funds": False,
                "permissions_json": {"read": True, "trade": True, "withdraw": False},
                "attestation_digest": "3" * 64,
                "observed_at": historical - timedelta(seconds=10),
                "expires_at": historical,
            },
            {
                "id": _uuid(7),
                "execution_attestation_id": _uuid(3),
                "deployment_id": _uuid(1),
                "execution_target": "OKX_DEMO",
                "instrument": "BTC-USDT-SWAP",
                "allow_real_funds": False,
                "simulated_trading": True,
                "instrument_digest": "4" * 64,
                "mark_price_digest": "5" * 64,
                "observed_at": historical - timedelta(seconds=10),
                "expires_at": historical,
                **{
                    f"{prefix}_{suffix}": value
                    for prefix in (
                        "instrument",
                        "mark_price",
                        "account_config",
                        "leverage",
                        "exchange_max_leverage",
                    )
                    for suffix, value in (
                        ("observed_at", historical - timedelta(seconds=10)),
                        ("expires_at", historical),
                    )
                },
            },
        ]
    )
    assert DatabaseOrderWriterAuthorityVerifier(_factory(connection)).verify(
        authority, observed_at=NOW
    )


def test_runtime_confirmation_derives_receipt_from_exact_live_supervisor(
    monkeypatch,
) -> None:
    plan = build_launch_plan(
        service_key="long_lived_runtime",
        stage="NO_ORDER_SOAK",
        generation=1,
        prepared_at=NOW - timedelta(seconds=10),
        release_digest="6" * 64,
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        runtime_image_authority=_runtime_image_authority(),
    )
    heartbeat_at = NOW - timedelta(seconds=1)
    lease = Phase9Lease(
        service_key=plan.service_key,
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        runtime_image_acceptance_id=plan.runtime_image_acceptance_id,
        runtime_image_acceptance_receipt_digest=plan.runtime_image_acceptance_receipt_digest,
        runtime_image_config_digest=plan.runtime_image_config_digest,
        holder_token_digest="8" * 64,
        pid=321,
        acquired_at=NOW - timedelta(seconds=5),
        heartbeat_at=heartbeat_at,
        expires_at=NOW + timedelta(seconds=20),
    )
    heartbeat = build_lifecycle_receipt(
        service_key=plan.service_key,
        action="HEARTBEAT",
        status="RUNNING",
        generation=plan.generation,
        observed_at=heartbeat_at,
        plan_digest=plan.plan_digest,
        holder_token_digest=lease.holder_token_digest,
        details={
            "pid": lease.pid,
            "expires_at": lease.expires_at,
            "release_digest": plan.release_digest,
            "deployment_id": str(plan.deployment_id),
            "deployment_capability_digest": plan.deployment_capability_digest,
            "image_digest": plan.image_digest,
            "runtime_image_acceptance_id": str(plan.runtime_image_acceptance_id),
            "runtime_image_acceptance_receipt_digest": plan.runtime_image_acceptance_receipt_digest,
            "runtime_image_config_digest": plan.runtime_image_config_digest,
        },
    )
    connection = _Connection(
        [
            {
                "id": _uuid(1),
                "deployment_approval_id": _uuid(2),
                "strategy_version_id": _uuid(3),
                "configuration_bundle_id": _uuid(4),
                "configuration_bundle_digest": "9" * 64,
                "market_snapshot_id": _uuid(5),
                "market_snapshot_digest": "a" * 64,
                "capability_digest": "1" * 64,
                "status": "PENDING",
                "demo_only": True,
                "allow_real_funds": False,
            },
            {
                "id": _uuid(2),
                "qualification_decision_id": _uuid(6),
                "status": "APPROVED",
            },
            {"id": _uuid(6), "status": "QUALIFIED"},
        ]
    )
    captured = {}

    def confirm(_connection, **kwargs):
        captured.update(kwargs)
        return kwargs["receipt"].runtime_instance_id

    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_composition.confirm_production_demo_runtime_observation",
        confirm,
    )
    runtime_id = confirm_running_runtime_from_supervisor(
        connection,
        plan=plan,
        lease=lease,
        heartbeat_receipt=heartbeat,
        observed_at=NOW,
        credential_reference="none:public-okx-market-only",
    )
    assert runtime_id == captured["receipt"].runtime_instance_id
    assert captured["deployment_id"] == plan.deployment_id
    assert captured["receipt"].evidence_class == "PRODUCTION_DEMO_RUNTIME"


def test_runtime_confirmation_keeps_identity_across_no_order_to_shadow_plan(
    monkeypatch,
) -> None:
    plans = (
        build_launch_plan(
            service_key="long_lived_runtime",
            stage=stage,
            generation=generation,
            prepared_at=NOW - timedelta(seconds=10),
            release_digest="6" * 64,
            deployment_id=_uuid(1),
            deployment_capability_digest="1" * 64,
            runtime_image_authority=_runtime_image_authority(),
        )
        for stage, generation in (
            ("NO_ORDER_SOAK", 1),
            ("SIGNAL_RISK_SHADOW", 2),
        )
    )
    captured = []

    def confirm(_connection, **kwargs):
        captured.append(kwargs["receipt"])
        return kwargs["receipt"].runtime_instance_id

    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_composition.confirm_production_demo_runtime_observation",
        confirm,
    )
    for plan in plans:
        observed_at = NOW + timedelta(seconds=plan.generation - 1)
        heartbeat_at = observed_at - timedelta(seconds=1)
        lease = Phase9Lease(
            service_key=plan.service_key,
            generation=plan.generation,
            plan_digest=plan.plan_digest,
            release_digest=plan.release_digest,
            deployment_id=plan.deployment_id,
            deployment_capability_digest=plan.deployment_capability_digest,
            image_digest=plan.image_digest,
            runtime_image_acceptance_id=plan.runtime_image_acceptance_id,
            runtime_image_acceptance_receipt_digest=plan.runtime_image_acceptance_receipt_digest,
            runtime_image_config_digest=plan.runtime_image_config_digest,
            holder_token_digest="8" * 64,
            pid=321,
            acquired_at=NOW - timedelta(seconds=5),
            heartbeat_at=heartbeat_at,
            expires_at=observed_at + timedelta(seconds=20),
        )
        heartbeat = build_lifecycle_receipt(
            service_key=plan.service_key,
            action="HEARTBEAT",
            status="RUNNING",
            generation=plan.generation,
            observed_at=heartbeat_at,
            plan_digest=plan.plan_digest,
            holder_token_digest=lease.holder_token_digest,
            details={
                "pid": lease.pid,
                "expires_at": lease.expires_at,
                "release_digest": plan.release_digest,
                "deployment_id": str(plan.deployment_id),
                "deployment_capability_digest": plan.deployment_capability_digest,
                "image_digest": plan.image_digest,
                "runtime_image_acceptance_id": str(plan.runtime_image_acceptance_id),
                "runtime_image_acceptance_receipt_digest": plan.runtime_image_acceptance_receipt_digest,
                "runtime_image_config_digest": plan.runtime_image_config_digest,
            },
        )
        connection = _Connection(
            [
                {
                    "id": _uuid(1),
                    "deployment_approval_id": _uuid(2),
                    "strategy_version_id": _uuid(3),
                    "configuration_bundle_id": _uuid(4),
                    "configuration_bundle_digest": "9" * 64,
                    "market_snapshot_id": _uuid(5),
                    "market_snapshot_digest": "a" * 64,
                    "capability_digest": "1" * 64,
                    "status": "ACTIVE" if plan.generation == 2 else "PENDING",
                    "demo_only": True,
                    "allow_real_funds": False,
                },
                {
                    "id": _uuid(2),
                    "qualification_decision_id": _uuid(6),
                    "status": "APPROVED",
                },
                {"id": _uuid(6), "status": "QUALIFIED"},
            ]
        )
        confirm_running_runtime_from_supervisor(
            connection,
            plan=plan,
            lease=lease,
            heartbeat_receipt=heartbeat,
            observed_at=observed_at,
            credential_reference="none:public-okx-market-only",
        )

    assert captured[0].runtime_instance_id == captured[1].runtime_instance_id
    assert captured[0].receipt_digest != captured[1].receipt_digest
    assert captured[0].observation_digest != captured[1].observation_digest


def test_runtime_stop_confirmation_uses_exact_database_and_supervisor_lineage(
    monkeypatch,
) -> None:
    plan = build_launch_plan(
        service_key="long_lived_runtime",
        stage="NO_ORDER_SOAK",
        generation=1,
        prepared_at=NOW - timedelta(seconds=10),
        release_digest="6" * 64,
        deployment_id=_uuid(1),
        deployment_capability_digest="1" * 64,
        runtime_image_authority=_runtime_image_authority(),
    )
    stop_receipt = build_lifecycle_receipt(
        service_key=plan.service_key,
        action="STOP",
        status="STOPPED",
        generation=plan.generation,
        observed_at=NOW - timedelta(seconds=1),
        plan_digest=plan.plan_digest,
        details={"label": plan.launch_agent_label},
    )
    connection = _Connection(
        [
            {
                "id": _uuid(1),
                "deployment_approval_id": _uuid(2),
                "strategy_version_id": _uuid(3),
                "configuration_bundle_id": _uuid(4),
                "configuration_bundle_digest": "9" * 64,
                "market_snapshot_id": _uuid(5),
                "market_snapshot_digest": "a" * 64,
                "capability_digest": "1" * 64,
                "status": "ACTIVE",
                "demo_only": True,
                "allow_real_funds": False,
            },
            {
                "id": _uuid(7),
                "runtime_identity": plan.process_identity,
                "image_digest": plan.image_digest,
                "service_account": "canonical_runtime_reader",
                "order_writer_capability": False,
            },
            {
                "id": _uuid(2),
                "qualification_decision_id": _uuid(6),
                "status": "APPROVED",
            },
            {"id": _uuid(6), "status": "QUALIFIED"},
        ]
    )
    captured = {}

    def persist(_connection, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            runtime_instance_id=_uuid(7),
            receipt_digest=kwargs["receipt"].receipt_digest,
            status="STOPPED",
            repeat_noop=False,
        )

    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_composition.confirm_production_demo_runtime_stop_observation",
        persist,
    )
    result = confirm_stopped_runtime_from_supervisor(
        connection,
        plan=plan,
        stop_receipt=stop_receipt,
        observed_at=NOW,
        launch_agent_loaded=False,
        holder_pid_alive=False,
        lease=None,
        container_present=False,
        credential_reference="none:public-okx-market-only",
    )

    assert result.status == "STOPPED"
    assert captured["deployment_id"] == plan.deployment_id
    assert captured["receipt"].runtime_instance_id == _uuid(7)
    assert captured["receipt"].status == "STOPPED"


def test_probe_orchestration_uses_only_sealed_current_session_result(
    monkeypatch,
) -> None:
    probe = SimpleNamespace(
        instrument="BTC-USDT-SWAP",
        account_fingerprint_digest="1" * 64,
        credential_generation_digest="2" * 64,
        permissions={"read": True, "trade": True, "withdraw": False},
        observed_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
    )

    class Session:
        def probe(self, *, instrument):
            assert instrument == "BTC-USDT-SWAP"
            return probe

    observed = []
    attestation = RedactedExecutionAttestationResult(
        _uuid(80), "8" * 64, probe.expires_at, False
    )
    receipt = CanaryProbeReceiptResult(
        _uuid(81), "9" * 64, probe.observed_at, probe.expires_at, False
    )
    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_composition.record_redacted_demo_attestation",
        lambda connection, **kwargs: observed.append(
            ("attestation", connection, kwargs)
        )
        or attestation,
    )
    monkeypatch.setattr(
        "app.canonical_v13.phase9_production_composition.persist_canary_probe_receipt",
        lambda connection, **kwargs: observed.append(("probe", connection, kwargs))
        or receipt,
    )
    deployment_connection = object()
    approval_connection = object()
    sealed_probe, first = record_current_canary_attestation(
        deployment_connection,
        deployment_id=_uuid(1),
        session=Session(),
        evaluated_at=NOW,
    )
    second = record_current_canary_probe_receipt(
        approval_connection,
        deployment_id=_uuid(1),
        probe=sealed_probe,
        attestation=first,
        evaluated_at=NOW,
    )
    assert first == attestation
    assert second == receipt
    assert observed[0][1] is deployment_connection
    assert observed[1][1] is approval_connection
    assert observed[0][2]["observed_at"] == observed[1][2]["probe"].observed_at
    assert observed[0][2]["expires_at"] == observed[1][2]["probe"].expires_at
    assert observed[1][2]["execution_attestation_id"] == attestation.attestation_id
