from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.canonical_v13.phase9_runtime_supervisor import (
    CanonicalPhase9SupervisorBlocked,
    OrderWriterCanaryAuthority,
    Phase9Lease,
    RuntimeImagePlanAuthority,
    build_lifecycle_receipt,
    build_launch_plan,
    build_order_writer_canary_authority,
    claim_lease,
    heartbeat_lease,
    release_lease,
    verify_lifecycle_receipt,
)
from app.canonical_v13.phase9_okx_demo import RedactedOkxDemoProbe
from app.canonical_v13.phase9_canary_policy import CanaryRiskPolicyResult


NOW = datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc)
PLAN_ID = UUID("10000000-0000-4000-8000-000000000001")
DEPLOYMENT_ID = UUID("30000000-0000-4000-8000-000000000003")
RELEASE_DIGEST = "a" * 64
CAPABILITY_DIGEST = "b" * 64
IMAGE_DIGEST = "c" * 64
IMAGE_CONFIG_DIGEST = "8" * 64
IMAGE_ACCEPTANCE_ID = UUID("70000000-0000-4000-8000-000000000007")
IMAGE_ACCEPTANCE_RECEIPT = "2" * 64
RISK_POLICY_ID = UUID("50000000-0000-4000-8000-000000000005")
ATTESTATION_ID = UUID("60000000-0000-4000-8000-000000000006")
RISK_POLICY_DIGEST = "d" * 64
ATTESTATION_DIGEST = "e" * 64
METADATA_DIGEST = "f" * 64
MARK_DIGEST = "1" * 64
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "canonical_v13_phase9_service.py"
)


class MemoryLeasePort:
    def __init__(self, lease: Phase9Lease | None = None) -> None:
        self.lease = lease

    def read(self, _service_key: str) -> Phase9Lease | None:
        return self.lease

    def claim(self, lease: Phase9Lease) -> None:
        assert self.lease is None
        self.lease = lease

    def replace(self, expected: Phase9Lease, lease: Phase9Lease) -> None:
        assert self.lease == expected
        self.lease = lease

    def release(self, expected: Phase9Lease) -> None:
        assert self.lease == expected
        self.lease = None


class ProcessProbe:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self, _pid: int) -> bool:
        return self.alive


class AuthorityPort:
    def __init__(self, expected: OrderWriterCanaryAuthority, *, valid=True) -> None:
        self.expected = expected
        self.valid = valid
        self.calls: list[tuple[OrderWriterCanaryAuthority, datetime]] = []
        self.recovery_calls: list[
            tuple[OrderWriterCanaryAuthority, UUID, datetime]
        ] = []

    def verify(self, authority, *, observed_at):
        self.calls.append((authority, observed_at))
        return self.valid and authority == self.expected

    def verify_recovery(self, authority, *, order_id, observed_at):
        self.recovery_calls.append((authority, order_id, observed_at))
        return self.valid and authority == self.expected


def _writer_authority(*, expires_at=NOW + timedelta(seconds=60), **changes):
    values = {
        "deployment_id": DEPLOYMENT_ID,
        "deployment_capability_digest": CAPABILITY_DIGEST,
        "execution_canary_risk_policy_id": RISK_POLICY_ID,
        "execution_canary_risk_policy_digest": RISK_POLICY_DIGEST,
        "attestation_id": ATTESTATION_ID,
        "attestation_digest": ATTESTATION_DIGEST,
        "attestation_expires_at": expires_at,
        "instrument_metadata_digest": METADATA_DIGEST,
        "mark_price_snapshot_digest": MARK_DIGEST,
        "strategy_max_leverage": "12",
        "effective_leverage": "12",
        "position_policy": "LONG_ONLY",
    }
    return build_order_writer_canary_authority(**{**values, **changes})


def _writer_plan(*, authority=None, recovery_order_id=None):
    resolved = authority or _writer_authority()
    return build_launch_plan(
        service_key="order_writer",
        stage="OKX_DEMO_CANARY",
        generation=1,
        prepared_at=NOW,
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        order_writer_enabled=True,
        order_writer_canary_authority=resolved,
        recovery_order_id=recovery_order_id,
        plan_id=UUID("20000000-0000-4000-8000-000000000002"),
    )


def test_recovery_writer_plan_binds_order_and_uses_immutable_verifier() -> None:
    order_id = UUID("90000000-0000-4000-8000-000000000009")
    authority = _writer_authority(expires_at=NOW + timedelta(seconds=1))
    plan = _writer_plan(authority=authority, recovery_order_id=order_id)
    normal = _writer_plan(authority=authority)
    assert plan.recovery_order_id == order_id
    assert plan.plan_digest != normal.plan_digest

    port = MemoryLeasePort()
    authority_port = AuthorityPort(authority)
    lease, _receipt = claim_lease(
        port,
        plan=plan,
        holder_token="r" * 48,
        pid=4321,
        now=NOW + timedelta(minutes=5),
        ttl=timedelta(seconds=30),
        process_probe=ProcessProbe(False),
        authority_port=authority_port,
    )
    assert lease.recovery_order_id == order_id
    assert authority_port.calls == []
    assert authority_port.recovery_calls == [
        (authority, order_id, NOW + timedelta(minutes=5))
    ]


def _runtime_plan(*, generation: int = 1):
    return build_launch_plan(
        service_key="long_lived_runtime",
        stage="NO_ORDER_SOAK",
        generation=generation,
        prepared_at=NOW,
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        runtime_image_authority=_runtime_image_authority(),
        plan_id=PLAN_ID,
    )


def _runtime_image_authority(
    *, image_digest: str = IMAGE_DIGEST, release_digest: str = RELEASE_DIGEST
) -> RuntimeImagePlanAuthority:
    return RuntimeImagePlanAuthority(
        acceptance_id=IMAGE_ACCEPTANCE_ID,
        image_manifest_digest=image_digest,
        image_config_digest=IMAGE_CONFIG_DIGEST,
        acceptance_receipt_digest=IMAGE_ACCEPTANCE_RECEIPT,
        release_digest=release_digest,
    )


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_writer_lease_release_uses_valid_stopped_lifecycle_status(
    monkeypatch,
) -> None:
    service = _load_script("canonical_phase9_db_lease_release_receipt")
    captured = []

    @contextmanager
    def connection_factory():
        yield object()

    monkeypatch.setattr(service, "_read_order_holder_token", lambda: "x" * 64)
    monkeypatch.setattr(service, "_phase9_database_url", lambda _role: "unused")
    monkeypatch.setattr(
        service, "_connection_factory", lambda _url: connection_factory
    )
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_append_receipt", captured.append)
    monkeypatch.setattr(
        service,
        "release_demo_order_writer_lease",
        lambda *_args, **_kwargs: {
            "status": "RELEASED",
            "generation": 1,
            "lease_digest": "9" * 64,
            "repeat_noop": False,
        },
    )

    service._release_stopped_order_writer_database_lease(_writer_plan())

    assert len(captured) == 1
    assert captured[0].action == "DATABASE_WRITER_LEASE_RELEASE"
    assert captured[0].status == "STOPPED"
    verify_lifecycle_receipt(captured[0])


def _configure_roots(module, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(module, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(module, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(module, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(module, "BACKEND_PYTHON", tmp_path / "venv" / "python")
    monkeypatch.setattr(module, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(module, "_now", lambda: NOW)
    monkeypatch.setattr(
        module, "_load_runtime_image_authority", lambda _id: _runtime_image_authority()
    )


def _redacted_probe(*, observed_at: datetime, expires_at: datetime):
    resource_observed = observed_at
    resource_expires = expires_at
    return RedactedOkxDemoProbe(
        execution_target="OKX_DEMO",
        instrument="BTC-USDT-SWAP",
        account_fingerprint_digest="1" * 64,
        credential_generation_digest="2" * 64,
        permissions={"read": True, "trade": True, "withdraw": False},
        simulated_trading=True,
        allow_real_funds=False,
        observed_at=observed_at,
        expires_at=expires_at,
        contract_value="0.01",
        contract_value_currency="BTC",
        lot_size="1",
        min_size="1",
        tick_size="0.1",
        mark_price="100000",
        current_long_leverage="12",
        current_short_leverage="12",
        exchange_max_leverage="20",
        limit_price="100000",
        maximum_buy_contracts="2",
        long_contracts="0",
        short_contracts="0",
        active_position_count=0,
        pending_order_count=0,
        instrument_digest="3" * 64,
        instrument_observed_at=resource_observed,
        instrument_expires_at=resource_expires,
        mark_price_digest="4" * 64,
        mark_price_observed_at=resource_observed,
        mark_price_expires_at=resource_expires,
        account_config_digest="5" * 64,
        account_config_observed_at=resource_observed,
        account_config_expires_at=resource_expires,
        leverage_digest="6" * 64,
        leverage_observed_at=resource_observed,
        leverage_expires_at=resource_expires,
        exchange_max_leverage_digest="7" * 64,
        exchange_max_leverage_observed_at=resource_observed,
        exchange_max_leverage_expires_at=resource_expires,
        positions_digest="8" * 64,
        positions_observed_at=resource_observed,
        positions_expires_at=resource_expires,
        pending_orders_digest="9" * 64,
        pending_orders_observed_at=resource_observed,
        pending_orders_expires_at=resource_expires,
        maximum_order_quantity_digest="0" * 64,
        maximum_order_quantity_observed_at=resource_observed,
        maximum_order_quantity_expires_at=resource_expires,
    )


def _prepare_runtime(service, stage: str = "NO_ORDER_SOAK"):
    return service.prepare(
        "long_lived_runtime",
        stage,
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
        enable_order_writer=False,
    )


def _prepare_writer(service, *, authority=None):
    resolved = authority or _writer_authority()
    return service.prepare(
        "order_writer",
        "OKX_DEMO_CANARY",
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        runtime_image_acceptance_id=None,
        enable_order_writer=True,
        order_writer_canary_authority=resolved,
    )


def test_launch_plans_keep_runtime_and_writer_identity_and_lifecycle_separate() -> None:
    runtime = _runtime_plan()
    writer = _writer_plan()

    assert runtime.launch_agent_label != writer.launch_agent_label
    assert runtime.process_identity != writer.process_identity
    assert runtime.postgres_capability == "canonical_runtime_reader"
    assert writer.postgres_capability == "canonical_order_writer"
    assert runtime.order_writer_enabled is False
    assert writer.order_writer_enabled is True
    assert runtime.demo_only is writer.demo_only is True
    assert runtime.allow_real_funds is writer.allow_real_funds is False
    assert writer.order_writer_canary_authority == _writer_authority()


def test_acceptance_trigger_lifecycle_receipt_is_accepted_and_verifiable() -> None:
    receipt = build_lifecycle_receipt(
        service_key="long_lived_runtime",
        action="ACCEPTANCE_SIGNAL_TRIGGER",
        status="ACCEPTED",
        generation=37,
        observed_at=NOW,
        plan_digest="9" * 64,
        details={
            "source_kind": "ACCEPTANCE_SCHEDULED_TEST",
            "order_submission_enabled": False,
        },
    )

    verify_lifecycle_receipt(receipt)
    assert receipt.status == "ACCEPTED"
    assert len(receipt.receipt_digest) == 64


def test_runtime_container_port_uses_exact_digest_and_hardened_rootless_flags() -> None:
    service = _load_script("canonical_phase9_runtime_container_test")
    plan = _runtime_plan()
    container_id = "d" * 64
    calls: list[tuple[str, ...]] = []

    def runner(command):
        calls.append(tuple(command))
        if command[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                f"true sha256:{plan.runtime_image_config_digest} {plan.plan_digest}\n",
                "",
            )
        if command[1] == "run":
            return subprocess.CompletedProcess(command, 0, container_id + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    port = service.RuntimeContainerPort(runner)
    observed = port.start(plan)
    digest = port.verify(plan, observed)
    port.stop(plan, observed)

    assert observed == container_id
    assert len(digest) == 64
    run = calls[0]
    assert "--network=none" in run
    assert "--read-only" in run
    assert "--cap-drop=all" in run
    assert "--security-opt=no-new-privileges" in run
    assert "--pids-limit=64" in run
    assert "--memory=256m" in run
    assert "--cpus=0.5" in run
    assert f"sha256:{plan.runtime_image_config_digest}" in run
    assert IMAGE_ACCEPTANCE_RECEIPT not in " ".join(run)
    assert calls[-1] == (service.PODMAN_PATH, "stop", "--time=10", container_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("execution_canary_risk_policy_id", UUID(int=10)),
        ("execution_canary_risk_policy_digest", "2" * 64),
        ("attestation_id", UUID(int=11)),
        ("attestation_digest", "3" * 64),
        ("attestation_expires_at", NOW + timedelta(seconds=30)),
        ("instrument_metadata_digest", "4" * 64),
        ("mark_price_snapshot_digest", "5" * 64),
        ("strategy_max_leverage", "12.5"),
        ("effective_leverage", "10"),
    ),
)
def test_writer_plan_digest_binds_every_canary_authority_fact(field, value) -> None:
    baseline = _writer_plan()
    changed_authority = _writer_authority(**{field: value})
    changed = _writer_plan(authority=changed_authority)
    assert changed.plan_digest != baseline.plan_digest


def test_writer_plan_requires_exact_long_only_frozen_authority() -> None:
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        build_launch_plan(
            service_key="order_writer",
            stage="OKX_DEMO_CANARY",
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
            deployment_id=DEPLOYMENT_ID,
            deployment_capability_digest=CAPABILITY_DIGEST,
            order_writer_enabled=True,
        )
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_POSITION_POLICY",
    ):
        _writer_authority(position_policy="LONG_SHORT")
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_EFFECTIVE_LEVERAGE",
    ):
        _writer_authority(effective_leverage="12.1")
    expired = _writer_authority(expires_at=NOW - timedelta(seconds=1))
    assert _writer_plan(authority=expired).order_writer_canary_authority == expired
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        build_launch_plan(
            service_key="order_writer",
            stage="OKX_DEMO_CANARY",
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
            deployment_id=UUID(int=12),
            deployment_capability_digest=CAPABILITY_DIGEST,
            order_writer_enabled=True,
            order_writer_canary_authority=_writer_authority(),
        )


def test_writer_lease_binds_and_revalidates_authority_on_claim_and_heartbeat() -> None:
    authority = _writer_authority()
    plan = _writer_plan(authority=authority)
    port = MemoryLeasePort()
    verifier = AuthorityPort(authority)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        claim_lease(
            port,
            plan=plan,
            holder_token="w" * 48,
            pid=4321,
            now=NOW,
            ttl=timedelta(seconds=35),
            process_probe=ProcessProbe(False),
        )
    lease, _receipt = claim_lease(
        port,
        plan=plan,
        holder_token="w" * 48,
        pid=4321,
        now=NOW,
        ttl=timedelta(seconds=35),
        process_probe=ProcessProbe(False),
        authority_port=verifier,
    )
    assert lease.order_writer_canary_authority == authority
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        heartbeat_lease(
            port,
            lease=lease,
            holder_token="w" * 48,
            now=NOW + timedelta(seconds=10),
            ttl=timedelta(seconds=35),
            authority_port=AuthorityPort(authority, valid=False),
        )
    renewed, _receipt = heartbeat_lease(
        port,
        lease=lease,
        holder_token="w" * 48,
        now=NOW + timedelta(seconds=10),
        ttl=timedelta(seconds=35),
        authority_port=verifier,
    )
    assert renewed.order_writer_canary_authority == authority


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_digest", "d" * 64),
        ("deployment_id", UUID("40000000-0000-4000-8000-000000000004")),
        ("deployment_capability_digest", "e" * 64),
        ("image_digest", "f" * 64),
    ],
)
def test_runtime_plan_digest_binds_release_deployment_capability_and_image(
    field: str, value: object
) -> None:
    baseline = _runtime_plan()
    arguments = {
        "service_key": "long_lived_runtime",
        "stage": "NO_ORDER_SOAK",
        "generation": 1,
        "prepared_at": NOW,
        "release_digest": RELEASE_DIGEST,
        "deployment_id": DEPLOYMENT_ID,
        "deployment_capability_digest": CAPABILITY_DIGEST,
        "runtime_image_authority": _runtime_image_authority(),
        "plan_id": PLAN_ID,
    }
    if field == "image_digest":
        changed = build_launch_plan(
            **{
                **arguments,
                "runtime_image_authority": _runtime_image_authority(
                    image_digest=str(value)
                ),
            }
        )
    elif field == "release_digest":
        changed = build_launch_plan(
            **{
                **arguments,
                field: value,
                "runtime_image_authority": _runtime_image_authority(
                    release_digest=str(value)
                ),
            }
        )
    else:
        changed = build_launch_plan(**{**arguments, field: value})
    assert changed.plan_digest != baseline.plan_digest


def test_runtime_plan_requires_exact_release_and_deployment_lineage() -> None:
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_RUNTIME_PLAN_LINEAGE_UNSET",
    ):
        build_launch_plan(
            service_key="long_lived_runtime",
            stage="NO_ORDER_SOAK",
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
        )
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_PHASE9_PLAN_DIGEST",
    ):
        build_launch_plan(
            service_key="order_writer",
            stage="OKX_DEMO_CANARY",
            generation=1,
            prepared_at=NOW,
            release_digest="",
            order_writer_enabled=True,
        )


@pytest.mark.parametrize("stage", ["NO_ORDER_SOAK", "SIGNAL_RISK_SHADOW"])
def test_order_writer_cannot_be_prepared_before_canary(stage: str) -> None:
    with pytest.raises(CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_STAGE"):
        build_launch_plan(
            service_key="order_writer",
            stage=stage,
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
            order_writer_enabled=True,
        )


def test_order_writer_requires_explicit_enable_and_runtime_rejects_it() -> None:
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_ORDER_WRITER_DISABLED"
    ):
        build_launch_plan(
            service_key="order_writer",
            stage="OKX_DEMO_CANARY",
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
        )
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_RUNTIME_ORDER_WRITER_FORBIDDEN",
    ):
        build_launch_plan(
            service_key="long_lived_runtime",
            stage="OKX_DEMO_CANARY",
            generation=1,
            prepared_at=NOW,
            release_digest=RELEASE_DIGEST,
            deployment_id=DEPLOYMENT_ID,
            deployment_capability_digest=CAPABILITY_DIGEST,
            runtime_image_authority=_runtime_image_authority(),
            order_writer_enabled=True,
        )


def test_single_lease_heartbeat_release_and_fencing() -> None:
    port = MemoryLeasePort()
    plan = _runtime_plan()
    token = "a" * 48
    lease, claimed = claim_lease(
        port,
        plan=plan,
        holder_token=token,
        pid=4321,
        now=NOW,
        ttl=timedelta(seconds=35),
        process_probe=ProcessProbe(False),
    )

    assert claimed.status == "RUNNING"
    assert claimed.details == {
        "pid": 4321,
        "orphan_cleaned": False,
        "release_digest": RELEASE_DIGEST,
        "deployment_id": str(DEPLOYMENT_ID),
        "deployment_capability_digest": CAPABILITY_DIGEST,
        "image_digest": IMAGE_DIGEST,
        "runtime_image_acceptance_id": str(IMAGE_ACCEPTANCE_ID),
        "runtime_image_acceptance_receipt_digest": IMAGE_ACCEPTANCE_RECEIPT,
        "runtime_image_config_digest": IMAGE_CONFIG_DIGEST,
    }
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_LEASE_HELD"
    ):
        claim_lease(
            port,
            plan=plan,
            holder_token="b" * 48,
            pid=4322,
            now=NOW + timedelta(seconds=1),
            ttl=timedelta(seconds=35),
            process_probe=ProcessProbe(False),
        )

    renewed, heartbeat = heartbeat_lease(
        port,
        lease=lease,
        holder_token=token,
        now=NOW + timedelta(seconds=10),
        ttl=timedelta(seconds=35),
    )
    assert heartbeat.status == "RUNNING"
    assert renewed.expires_at == NOW + timedelta(seconds=45)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_LEASE_FENCED"
    ):
        release_lease(
            port,
            lease=renewed,
            holder_token="wrong" * 8,
            now=NOW + timedelta(seconds=11),
        )
    stopped = release_lease(
        port,
        lease=renewed,
        holder_token=token,
        now=NOW + timedelta(seconds=11),
    )
    assert stopped.status == "STOPPED"
    assert port.lease is None


def test_expired_dead_orphan_is_cleaned_but_live_or_unexpired_owner_blocks() -> None:
    stale_plan = _runtime_plan()
    stale = Phase9Lease(
        service_key="long_lived_runtime",
        generation=1,
        plan_digest=stale_plan.plan_digest,
        release_digest=stale_plan.release_digest,
        deployment_id=stale_plan.deployment_id,
        deployment_capability_digest=stale_plan.deployment_capability_digest,
        image_digest=stale_plan.image_digest,
        holder_token_digest="f" * 64,
        pid=3333,
        acquired_at=NOW - timedelta(minutes=2),
        heartbeat_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(seconds=1),
    )
    port = MemoryLeasePort(stale)
    lease, receipt = claim_lease(
        port,
        plan=_runtime_plan(generation=2),
        holder_token="n" * 48,
        pid=4444,
        now=NOW,
        ttl=timedelta(seconds=35),
        process_probe=ProcessProbe(False),
    )
    assert receipt.status == "RECOVERED"
    assert receipt.details["orphan_cleaned"] is True
    assert lease.generation == 2

    for existing, alive in (
        (stale, True),
        (
            Phase9Lease(
                **{
                    **stale.__dict__,
                    "expires_at": NOW + timedelta(seconds=1),
                }
            ),
            False,
        ),
    ):
        with pytest.raises(
            CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_LEASE_HELD"
        ):
            claim_lease(
                MemoryLeasePort(existing),
                plan=_runtime_plan(generation=2),
                holder_token="n" * 48,
                pid=4444,
                now=NOW,
                ttl=timedelta(seconds=35),
                process_probe=ProcessProbe(alive),
            )


def test_prepare_writes_secret_free_runtime_plist_without_launchctl(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_prepare_test")
    _configure_roots(service, tmp_path, monkeypatch)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            calls.append(tuple(command))
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    result = _prepare_runtime(service)

    assert result["status"] == "PREPARED"
    assert calls == []
    plist_path = service._plist_path("long_lived_runtime")
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    encoded = json.dumps(payload, sort_keys=True)
    environment_encoded = json.dumps(payload["EnvironmentVariables"], sort_keys=True)
    arguments_encoded = json.dumps(payload["ProgramArguments"], sort_keys=True)
    assert payload["Label"] == "ai.freqtrade.canonical-v13.runtime"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ExitTimeOut"] > service.RUNTIME_CONTAINER_STOP_GRACE_SECONDS
    assert "PASSWORD" not in environment_encoded.upper()
    assert "KEYCHAIN" not in environment_encoded.upper()
    assert "DATABASE_URL" not in environment_encoded.upper()
    assert "OKX_API" not in environment_encoded.upper()
    assert "--PASSWORD" not in arguments_encoded.upper()
    assert "--KEYCHAIN" not in arguments_encoded.upper()
    assert "--DATABASE-URL" not in arguments_encoded.upper()
    assert "--OKX" not in arguments_encoded.upper()
    assert "FREQTRADE_AI_CANONICAL_PHASE9_STAGE" in encoded
    state = json.loads(service._state_path("long_lived_runtime").read_text())
    assert state["status"] == "PREPARED"
    assert state["plan"]["order_writer_enabled"] is False


def test_prepare_writer_fails_closed_without_explicit_canary_enable(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_writer_disabled_test")
    _configure_roots(service, tmp_path, monkeypatch)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_ORDER_WRITER_DISABLED"
    ):
        service.prepare(
            "order_writer",
            "OKX_DEMO_CANARY",
            release_digest=RELEASE_DIGEST,
            deployment_id=None,
            deployment_capability_digest=None,
            runtime_image_acceptance_id=None,
            enable_order_writer=False,
        )
    assert not service._plist_path("order_writer").exists()


def test_prepare_rejects_release_digest_not_matching_exact_main(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_release_drift_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_require_release_checkout", lambda: "f" * 64)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_RELEASE_DRIFT"
    ):
        _prepare_runtime(service)
    assert not service._plist_path("long_lived_runtime").exists()


def test_confirmation_requires_exact_prepared_digest_before_bootstrap(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_confirm_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    prepared = _prepare_runtime(service)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_CONFIRMATION_DRIFT"
    ):
        service.confirm("long_lived_runtime", "0" * 64)
    assert calls == []

    confirmed = service.confirm("long_lived_runtime", prepared["plan_digest"])
    assert confirmed["status"] == "CONFIRMED"
    assert calls == [
        ("launchctl", "bootout", service._launchctl_target("long_lived_runtime")),
        (
            "launchctl",
            "bootstrap",
            service._launchctl_domain(),
            str(service._plist_path("long_lived_runtime")),
        ),
    ]


def test_writer_confirm_restart_and_recover_revalidate_exact_current_authority(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_writer_authority_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    authority = _writer_authority()
    verifier = AuthorityPort(authority)
    prepared = _prepare_writer(service, authority=authority)

    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        service.confirm("order_writer", prepared["plan_digest"])
    assert calls == []

    service.confirm(
        "order_writer", prepared["plan_digest"], authority_port=verifier
    )
    calls_after_confirm = len(calls)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        service.restart(
            "order_writer",
            prepared["plan_digest"],
            authority_port=AuthorityPort(authority, valid=False),
        )
    assert len(calls) == calls_after_confirm
    service.restart(
        "order_writer", prepared["plan_digest"], authority_port=verifier
    )

    calls_before_recover = len(calls)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        service.recover(
            "order_writer", authority_port=AuthorityPort(authority, valid=False)
        )
    assert len(calls) == calls_before_recover
    recovered = service.recover("order_writer", authority_port=verifier)
    assert recovered["status"] == "NO_OP"
    assert [call[1] for call in verifier.calls] == [NOW, NOW, NOW]


def test_writer_confirmation_accepts_expired_frozen_attestation_lineage(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_writer_expiry_test")
    _configure_roots(service, tmp_path, monkeypatch)
    authority = _writer_authority(expires_at=NOW + timedelta(seconds=5))
    prepared = _prepare_writer(service, authority=authority)
    monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(seconds=5))
    calls = []
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: (
            calls.append(tuple(command))
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    confirmed = service.confirm(
        "order_writer",
        prepared["plan_digest"],
        authority_port=AuthorityPort(authority),
    )
    assert confirmed["status"] == "CONFIRMED"
    assert calls != []


def test_file_lease_port_never_persists_raw_holder_token(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_file_lease_test")
    _configure_roots(service, tmp_path, monkeypatch)
    port = service.FileLeasePort(service.SUPPORT_ROOT)
    token = "raw-holder-token-material-should-never-be-written"
    lease, _receipt = claim_lease(
        port,
        plan=_runtime_plan(),
        holder_token=token,
        pid=4321,
        now=NOW,
        ttl=timedelta(seconds=35),
        process_probe=ProcessProbe(False),
    )
    content = port._path("long_lived_runtime").read_text()
    assert token not in content
    assert lease.holder_token_digest in content


def test_file_lease_round_trip_preserves_exact_writer_canary_authority(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_writer_file_lease_test")
    _configure_roots(service, tmp_path, monkeypatch)
    authority = _writer_authority()
    plan = _writer_plan(authority=authority)
    port = service.FileLeasePort(service.SUPPORT_ROOT)
    lease, _receipt = claim_lease(
        port,
        plan=plan,
        holder_token="writer-holder-token-material" * 2,
        pid=4321,
        now=NOW,
        ttl=timedelta(seconds=35),
        process_probe=ProcessProbe(False),
        authority_port=AuthorityPort(authority),
    )
    assert port.read("order_writer") == lease
    payload = json.loads(port._path("order_writer").read_text())
    persisted = payload["order_writer_canary_authority"]
    assert persisted["deployment_id"] == str(DEPLOYMENT_ID)
    assert persisted["execution_canary_risk_policy_id"] == str(RISK_POLICY_ID)
    assert persisted["attestation_id"] == str(ATTESTATION_ID)
    assert persisted["strategy_max_leverage"] == "12"
    assert persisted["effective_leverage"] == "12"
    assert persisted["position_policy"] == "LONG_ONLY"


def test_failed_bootstrap_restores_prepared_state(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_failed_confirm_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    prepared = _prepare_runtime(service)

    def fake_run(command):
        return subprocess.CompletedProcess(
            command, 1 if "bootstrap" in command else 0, "", ""
        )

    monkeypatch.setattr(service, "_run", fake_run)
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked, match="BLOCKED_PHASE9_BOOTSTRAP_FAILED"
    ):
        service.confirm("long_lived_runtime", prepared["plan_digest"])
    state = json.loads(service._state_path("long_lived_runtime").read_text())
    assert state["status"] == "PREPARED"
    assert state["confirmed_at"] is None


def test_restart_stop_and_recovery_write_independent_receipts(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_lifecycle_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []
    loaded = False

    def fake_run(command):
        nonlocal loaded
        resolved = tuple(command)
        calls.append(resolved)
        if resolved[:3] == (service.PODMAN_PATH, "container", "exists"):
            return subprocess.CompletedProcess(command, 1, "", "")
        if resolved[:2] == ("launchctl", "print"):
            return subprocess.CompletedProcess(command, 0 if loaded else 3, "", "")
        if "bootstrap" in resolved:
            loaded = True
        if "bootout" in resolved:
            loaded = False
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    prepared = _prepare_runtime(service)
    service.confirm("long_lived_runtime", prepared["plan_digest"])
    service.restart("long_lived_runtime", prepared["plan_digest"])
    recovered = service.recover("long_lived_runtime")
    assert recovered["status"] == "NO_OP"
    service.stop("long_lived_runtime")

    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert [receipt["action"] for receipt in receipts] == [
        "PREPARE",
        "CONFIRM",
        "RESTART",
        "RECOVER",
        "STOP",
    ]
    assert len({receipt["receipt_digest"] for receipt in receipts}) == 5
    assert receipts[2]["details"]["restart_mode"] == (
        "GRACEFUL_BOOTOUT_BOOTSTRAP"
    )
    assert receipts[3]["details"] == {
        "bootstrap_required": False,
        "orphan_cleaned": False,
    }
    assert (
        "launchctl",
        "bootout",
        service._launchctl_target("long_lived_runtime"),
    ) in calls
    assert calls[-1] == (
        service.PODMAN_PATH,
        "container",
        "exists",
        service.RuntimeContainerPort.name(
            service._load_plan("long_lived_runtime")[0]
        ),
    )


def test_runtime_stop_waits_beyond_container_grace_for_lease_release(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_runtime_stop_budget_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    def fake_run(command):
        return subprocess.CompletedProcess(
            command,
            1
            if tuple(command[:3])
            == (service.PODMAN_PATH, "container", "exists")
            else 0,
            "",
            "",
        )

    monkeypatch.setattr(service, "_run", fake_run)
    prepared = _prepare_runtime(service)
    service.confirm("long_lived_runtime", prepared["plan_digest"])

    elapsed = [0.0]
    lease_release_at = service.RUNTIME_CONTAINER_STOP_GRACE_SECONDS + 0.5

    class DelayedLeasePort:
        def __init__(self, _root) -> None:
            pass

        def read(self, _service_key):
            return (
                SimpleNamespace(expires_at=NOW + timedelta(minutes=1), pid=4321)
                if elapsed[0] < lease_release_at
                else None
            )

    monkeypatch.setattr(service, "FileLeasePort", DelayedLeasePort)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: True)
    monkeypatch.setattr(service.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        service.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )

    stopped = service.stop("long_lived_runtime")

    assert stopped["status"] == "STOPPED"
    assert elapsed[0] >= lease_release_at
    assert elapsed[0] < service.SUPERVISOR_TEARDOWN_TIMEOUT_SECONDS


def test_runtime_stop_recovers_exact_residual_container(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_runtime_stop_container_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    prepared = _prepare_runtime(service)
    container_name = service.RuntimeContainerPort.name(
        service._load_plan("long_lived_runtime")[0]
    )
    container_present = True
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        nonlocal container_present
        resolved = tuple(command)
        calls.append(resolved)
        if resolved[:3] == (service.PODMAN_PATH, "container", "exists"):
            return subprocess.CompletedProcess(
                command, 0 if container_present else 1, "", ""
            )
        if resolved[:2] == (service.PODMAN_PATH, "stop"):
            assert resolved[-1] == container_name
            container_present = False
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    service.confirm("long_lived_runtime", prepared["plan_digest"])

    stopped = service.stop("long_lived_runtime")
    repeated = service.stop("long_lived_runtime")

    assert stopped["status"] == "STOPPED"
    assert repeated["repeat_noop"] is True
    assert container_present is False
    assert (
        service.PODMAN_PATH,
        "stop",
        f"--time={service.RUNTIME_CONTAINER_STOP_GRACE_SECONDS}",
        container_name,
    ) in calls


def test_runtime_stop_observation_composes_only_after_all_holders_are_absent(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_runtime_stop_observation_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    prepared = _prepare_runtime(service)
    observing = [False]

    def fake_run(command):
        resolved = tuple(command)
        absent = (
            resolved[:3] == (service.PODMAN_PATH, "container", "exists")
            or (observing[0] and resolved[:2] == ("launchctl", "print"))
        )
        return subprocess.CompletedProcess(
            command,
            113
            if observing[0] and resolved[:2] == ("launchctl", "print")
            else (1 if absent else 0),
            "",
            "",
        )

    monkeypatch.setattr(service, "_run", fake_run)
    service.confirm("long_lived_runtime", prepared["plan_digest"])
    service.stop("long_lived_runtime")
    observing[0] = True
    observed = {}

    @contextmanager
    def connection_factory():
        yield object()

    monkeypatch.setattr(service, "_phase9_database_url", lambda _name: "redacted")
    monkeypatch.setattr(
        service, "_connection_factory", lambda _url: connection_factory
    )
    monkeypatch.setattr(
        service,
        "_latest_running_heartbeat",
        lambda _service, _plan: SimpleNamespace(details={"pid": 4321}),
    )
    monkeypatch.setattr(
        service.UnixProcessProbe, "is_alive", lambda _self, _pid: False
    )

    def confirm_stopped(connection, **kwargs):
        observed.update(kwargs)
        assert connection is not None
        return SimpleNamespace(
            runtime_instance_id=UUID("00000000-0000-4000-8000-000000000123"),
            receipt_digest="f" * 64,
            status="STOPPED",
            repeat_noop=False,
        )

    monkeypatch.setattr(
        service, "confirm_stopped_runtime_from_supervisor", confirm_stopped
    )
    result = service.confirm_runtime_stop_observation(prepared["plan_digest"])

    assert result["status"] == "STOPPED"
    assert observed["launch_agent_loaded"] is False
    assert observed["holder_pid_alive"] is False
    assert observed["lease"] is None
    assert observed["container_present"] is False
    assert observed["stop_receipt"].action == "STOP"


def test_runtime_stop_releases_only_expired_dead_lease(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_runtime_stop_orphan_lease_test")
    _configure_roots(service, tmp_path, monkeypatch)
    prepared = _prepare_runtime(service)
    plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "RUNNING"},
    )
    stale = Phase9Lease(
        service_key="long_lived_runtime",
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="a" * 64,
        pid=999_999,
        acquired_at=NOW - timedelta(minutes=2),
        heartbeat_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(seconds=1),
    )
    service.FileLeasePort(service.SUPPORT_ROOT).claim(stale)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: False)

    def fake_run(command):
        return subprocess.CompletedProcess(
            command,
            1
            if tuple(command[:3])
            == (service.PODMAN_PATH, "container", "exists")
            else 0,
            "",
            "",
        )

    monkeypatch.setattr(service, "_run", fake_run)

    stopped = service.stop("long_lived_runtime")

    assert stopped["status"] == "STOPPED"
    assert service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    receipts = [
        json.loads(line)
        for line in service._receipt_path("long_lived_runtime").read_text().splitlines()
    ]
    assert receipts[-2]["action"] == "STOP_ORPHAN_LEASE_RELEASE"
    assert receipts[-2]["details"]["reason_code"] == "EXPIRED_DEAD_LEASE"
    assert receipts[-1]["action"] == "STOP"


def test_runtime_restart_blocks_before_bootstrap_if_container_survives_bootout(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_runtime_restart_orphan_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        resolved = tuple(command)
        calls.append(resolved)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    prepared = _prepare_runtime(service)
    service.confirm("long_lived_runtime", prepared["plan_digest"])
    calls.clear()

    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_PHASE9_RESTART_CONTAINER_HELD",
    ):
        service.restart("long_lived_runtime", prepared["plan_digest"])

    assert calls == [
        (
            "launchctl",
            "bootout",
            service._launchctl_target("long_lived_runtime"),
        ),
        (
            service.PODMAN_PATH,
            "container",
            "exists",
            service.RuntimeContainerPort.name(
                service._load_plan("long_lived_runtime")[0]
            ),
        ),
    ]


def test_runtime_restart_waits_for_launchd_label_retirement_before_bootstrap(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_runtime_restart_label_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []
    print_results = iter((0, 0, 3))

    def fake_run(command):
        resolved = tuple(command)
        calls.append(resolved)
        if resolved[:3] == (service.PODMAN_PATH, "container", "exists"):
            return subprocess.CompletedProcess(command, 1, "", "")
        if resolved[:2] == ("launchctl", "print"):
            return subprocess.CompletedProcess(command, next(print_results), "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(service, "_run", fake_run)
    prepared = _prepare_runtime(service)
    service.confirm("long_lived_runtime", prepared["plan_digest"])
    calls.clear()

    result = service.restart("long_lived_runtime", prepared["plan_digest"])

    assert result["status"] == "RESTARTED"
    print_calls = [call for call in calls if call[:2] == ("launchctl", "print")]
    assert len(print_calls) == 3
    assert calls[-1] == (
        "launchctl",
        "bootstrap",
        service._launchctl_domain(),
        str(service._plist_path("long_lived_runtime")),
    )


def test_recovery_cleans_only_expired_dead_orphan_before_bootstrap(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_orphan_recovery_test")
    _configure_roots(service, tmp_path, monkeypatch)
    prepared = _prepare_runtime(service)
    plan, state = service._load_plan("long_lived_runtime")
    service._atomic_json(
        service._state_path("long_lived_runtime"),
        {**state, "status": "CONFIRMED", "confirmed_at": NOW.isoformat()},
    )
    stale = Phase9Lease(
        service_key="long_lived_runtime",
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="a" * 64,
        pid=999_999,
        acquired_at=NOW - timedelta(minutes=2),
        heartbeat_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(seconds=1),
    )
    service.FileLeasePort(service.SUPPORT_ROOT).claim(stale)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: False)
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(
            command, 3 if "print" in command else 0, "", ""
        )

    monkeypatch.setattr(service, "_run", fake_run)
    result = service.recover("long_lived_runtime")
    assert result["status"] == "RECOVERED"
    assert result["orphan_cleaned"] is True
    assert (
        service.FileLeasePort(service.SUPPORT_ROOT).read("long_lived_runtime") is None
    )
    assert calls == [
        ("launchctl", "print", service._launchctl_target("long_lived_runtime")),
        (
            "launchctl",
            "bootstrap",
            service._launchctl_domain(),
            str(service._plist_path("long_lived_runtime")),
        ),
    ]
    assert prepared["plan_digest"] == plan.plan_digest


def test_status_requires_loaded_fresh_and_live_single_holder(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_status_test")
    _configure_roots(service, tmp_path, monkeypatch)
    _prepare_runtime(service)
    plan, _state = service._load_plan("long_lived_runtime")
    lease = Phase9Lease(
        service_key="long_lived_runtime",
        generation=1,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="a" * 64,
        pid=4321,
        acquired_at=NOW,
        heartbeat_at=NOW,
        expires_at=NOW + timedelta(seconds=35),
    )
    service.FileLeasePort(service.SUPPORT_ROOT).claim(lease)
    monkeypatch.setattr(
        service,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: True)
    assert service.status("long_lived_runtime")["status"] == "RUNNING"

    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: False)
    dead = service.status("long_lived_runtime")
    assert dead["status"] == "BLOCKED"
    assert dead["lease_fresh"] is True
    assert dead["holder_alive"] is False


def test_prepare_confirm_and_stop_exact_replays_are_side_effect_free(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_exact_replay_test")
    _configure_roots(service, tmp_path, monkeypatch)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/bin/launchctl")
    calls: list[tuple[str, ...]] = []

    def fake_run(command):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            1
            if tuple(command[:3])
            == (service.PODMAN_PATH, "container", "exists")
            else 0,
            "",
            "",
        )

    monkeypatch.setattr(service, "_run", fake_run)
    first_prepare = _prepare_runtime(service)
    repeated_prepare = _prepare_runtime(service)
    assert repeated_prepare == {**first_prepare, "repeat_noop": True}
    assert calls == []
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_PHASE9_PREPARED_PLAN_EXISTS",
    ):
        service.prepare(
            "long_lived_runtime",
            "SIGNAL_RISK_SHADOW",
            release_digest=RELEASE_DIGEST,
            deployment_id=DEPLOYMENT_ID,
            deployment_capability_digest=CAPABILITY_DIGEST,
            runtime_image_acceptance_id=IMAGE_ACCEPTANCE_ID,
            enable_order_writer=False,
        )

    first_confirm = service.confirm("long_lived_runtime", first_prepare["plan_digest"])
    confirm_call_count = len(calls)
    repeated_confirm = service.confirm(
        "long_lived_runtime", first_prepare["plan_digest"]
    )
    assert repeated_confirm == {**first_confirm, "repeat_noop": True}
    assert len(calls) == confirm_call_count

    first_stop = service.stop("long_lived_runtime")
    stop_call_count = len(calls)
    repeated_stop = service.stop("long_lived_runtime")
    assert repeated_stop == {**first_stop, "repeat_noop": True}
    assert len(calls) == stop_call_count + 1
    assert calls[-1][:3] == (service.PODMAN_PATH, "container", "exists")
    receipts = service._receipt_path("long_lived_runtime").read_text().splitlines()
    assert len(receipts) == 3


def test_cli_defaults_only_runtime_to_no_order_soak(monkeypatch, capsys) -> None:
    service = _load_script("canonical_phase9_cli_default_test")
    observed: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        service,
        "prepare",
        lambda service_key, stage, **kwargs: (
            observed.append((service_key, stage, kwargs)) or {"status": "PREPARED"}
        ),
    )
    assert (
        service.main(
            [
                "prepare",
                "--service",
                "long_lived_runtime",
                "--release-digest",
                RELEASE_DIGEST,
                "--deployment-id",
                str(DEPLOYMENT_ID),
                "--deployment-capability-digest",
                CAPABILITY_DIGEST,
                "--runtime-image-acceptance-id",
                str(IMAGE_ACCEPTANCE_ID),
            ]
        )
        == 0
    )
    assert observed == [
        (
            "long_lived_runtime",
            "NO_ORDER_SOAK",
            {
                "release_digest": RELEASE_DIGEST,
                "deployment_id": DEPLOYMENT_ID,
                "deployment_capability_digest": CAPABILITY_DIGEST,
                "runtime_image_acceptance_id": IMAGE_ACCEPTANCE_ID,
                "enable_order_writer": False,
                "order_writer_canary_authority": None,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "PREPARED"

    assert service.main(["prepare", "--service", "order_writer"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "detail": "--stage is required",
        "reason": "BLOCKED_PHASE9_STAGE",
        "status": "BLOCKED",
    }
    assert len(observed) == 1


def test_cli_prepare_writer_requires_and_binds_all_canary_authority_fields(
    monkeypatch, capsys
) -> None:
    service = _load_script("canonical_phase9_writer_cli_test")
    observed = []
    monkeypatch.setattr(
        service,
        "prepare",
        lambda service_key, stage, **kwargs: (
            observed.append((service_key, stage, kwargs)) or {"status": "PREPARED"}
        ),
    )
    base = [
        "prepare",
        "--service",
        "order_writer",
        "--stage",
        "OKX_DEMO_CANARY",
        "--release-digest",
        RELEASE_DIGEST,
        "--deployment-id",
        str(DEPLOYMENT_ID),
        "--deployment-capability-digest",
        CAPABILITY_DIGEST,
        "--enable-order-writer",
    ]
    assert service.main(base) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == (
        "BLOCKED_ORDER_WRITER_CANARY_AUTHORITY"
    )
    assert observed == []

    args = [
        *base,
        "--execution-canary-risk-policy-id",
        str(RISK_POLICY_ID),
        "--execution-canary-risk-policy-digest",
        RISK_POLICY_DIGEST,
        "--attestation-id",
        str(ATTESTATION_ID),
        "--attestation-digest",
        ATTESTATION_DIGEST,
        "--attestation-expires-at",
        (NOW + timedelta(seconds=60)).isoformat(),
        "--instrument-metadata-digest",
        METADATA_DIGEST,
        "--mark-price-snapshot-digest",
        MARK_DIGEST,
        "--strategy-max-leverage",
        "12.0",
        "--effective-leverage",
        "12.0",
        "--position-policy",
        "LONG_ONLY",
    ]
    assert service.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PREPARED"
    authority = observed[0][2]["order_writer_canary_authority"]
    assert authority == _writer_authority()
    assert authority.strategy_max_leverage == "12"
    assert authority.effective_leverage == "12"


def test_cli_routes_sealed_runtime_and_canary_operator_commands(
    monkeypatch, capsys
) -> None:
    service = _load_script("canonical_phase9_production_commands_test")
    observed = []
    monkeypatch.setattr(
        service,
        "confirm_runtime_observation",
        lambda digest: observed.append(("confirm-runtime", digest))
        or {"status": "ACTIVE"},
    )
    monkeypatch.setattr(
        service,
        "probe_canary",
        lambda deployment_id: observed.append(("probe", deployment_id))
        or {"status": "READY"},
    )
    monkeypatch.setattr(
        service,
        "prepare_canary_order",
        lambda digest, risk_id: observed.append(("prepare-order", digest, risk_id))
        or {"status": "READY"},
    )
    monkeypatch.setattr(
        service,
        "cancel_prepared_canary_order",
        lambda order_id: observed.append(("cancel-order", order_id))
        or {"status": "CANCELLED"},
    )
    monkeypatch.setattr(
        service,
        "dispatch_canary",
        lambda digest, risk_id: observed.append(("dispatch", digest, risk_id))
        or {"status": "ACCEPTED"},
    )
    monkeypatch.setattr(
        service,
        "recover_canary",
        lambda digest, order_id: observed.append(("recover", digest, order_id))
        or {"status": "RECOVERED"},
    )
    monkeypatch.setattr(
        service,
        "accept_recovery_soak",
        lambda qualification_id: observed.append(("accept-recovery", qualification_id))
        or {"status": "ACCEPTED"},
    )
    risk_id = UUID("00000000-0000-4000-8000-000000000091")
    order_id = UUID("00000000-0000-4000-8000-000000000092")
    assert service.main(
        [
            "confirm-runtime-observation",
            "--service",
            "long_lived_runtime",
            "--plan-digest",
                RELEASE_DIGEST,
        ]
    ) == 0
    assert service.main(
        [
            "accept-recovery-soak",
            "--service",
            "recovery_control",
            "--qualification-decision-id",
            str(QUALIFICATION_ID := UUID("00000000-0000-4000-8000-000000000099")),
        ]
    ) == 0
    assert service.main(
        [
            "probe-canary",
            "--service",
            "order_writer",
            "--deployment-id",
            str(DEPLOYMENT_ID),
        ]
    ) == 0
    assert service.main(
        [
            "prepare-canary-order",
            "--service",
            "order_writer",
            "--plan-digest",
            RELEASE_DIGEST,
            "--risk-decision-id",
            str(risk_id),
        ]
    ) == 0
    assert service.main(
        [
            "cancel-prepared-canary-order",
            "--service",
            "order_writer",
            "--order-id",
            str(order_id),
        ]
    ) == 0
    assert service.main(
        [
            "dispatch-canary",
            "--service",
            "order_writer",
            "--plan-digest",
            RELEASE_DIGEST,
            "--risk-decision-id",
            str(risk_id),
        ]
    ) == 0
    assert service.main(
        [
            "recover-canary",
            "--service",
            "order_writer",
            "--plan-digest",
            RELEASE_DIGEST,
            "--order-id",
            str(order_id),
        ]
    ) == 0
    assert observed == [
        ("confirm-runtime", RELEASE_DIGEST),
        ("accept-recovery", QUALIFICATION_ID),
        ("probe", DEPLOYMENT_ID),
        ("prepare-order", RELEASE_DIGEST, risk_id),
        ("cancel-order", order_id),
        ("dispatch", RELEASE_DIGEST, risk_id),
        ("recover", RELEASE_DIGEST, order_id),
    ]
    assert [
        json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()
    ] == [
        "ACTIVE",
        "ACCEPTED",
        "READY",
        "READY",
        "CANCELLED",
        "ACCEPTED",
        "RECOVERED",
    ]


def test_cli_preserves_only_sanitized_order_recovery_diagnostic(
    monkeypatch, capsys
) -> None:
    service = _load_script("canonical_phase9_safe_order_diagnostic_test")
    risk_id = UUID("00000000-0000-4000-8000-000000000091")

    def blocked(_digest, _risk_id):
        raise service.CanonicalOrderRecoveryRequired(
            "BLOCKED_ORDER_RECOVERY_REQUIRED",
            "order outcome is unknown: HTTP_ERROR_AMBIGUOUS:"
            "http_status=400:okx_code=1:okx_s_code=51000:"
            "client_order_id=MISSING",
        )

    monkeypatch.setattr(service, "dispatch_canary", blocked)

    assert service.main(
        [
            "dispatch-canary",
            "--service",
            "order_writer",
            "--plan-digest",
            RELEASE_DIGEST,
            "--risk-decision-id",
            str(risk_id),
        ]
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "BLOCKED",
        "reason": "BLOCKED_ORDER_RECOVERY_REQUIRED",
        "detail": (
            "order outcome is unknown: HTTP_ERROR_AMBIGUOUS:"
            "http_status=400:okx_code=1:okx_s_code=51000:"
            "client_order_id=MISSING"
        ),
    }
def test_cli_supervise_enables_production_composition(monkeypatch) -> None:
    service = _load_script("canonical_phase9_production_supervise_test")
    observed = []
    monkeypatch.setattr(
        service,
        "supervise",
        lambda service_key, plan_digest, **kwargs: observed.append(
            (service_key, plan_digest, kwargs)
        ),
    )
    assert service.main(
        [
            "supervise",
            "--service",
            "order_writer",
            "--plan-digest",
            RELEASE_DIGEST,
        ]
    ) == 0
    assert observed == [
        ("order_writer", RELEASE_DIGEST, {"production_compose": True})
    ]


def test_production_order_operator_reuses_one_connection_factory(monkeypatch) -> None:
    service = _load_script("canonical_phase9_shared_order_connection_factory_test")
    connection_factory = object()
    observed_urls = []
    captured = {}

    monkeypatch.setattr(
        service,
        "_phase9_database_url",
        lambda capability: observed_urls.append(capability) or "order-url",
    )
    monkeypatch.setattr(
        service,
        "_connection_factory",
        lambda database_url: connection_factory
        if database_url == "order-url"
        else pytest.fail("unexpected database URL"),
    )
    monkeypatch.setattr(
        service,
        "_production_authority_port",
        lambda: pytest.fail("must not allocate a second connection pool"),
    )
    monkeypatch.setattr(service, "_production_okx_session_factory", object)
    monkeypatch.setattr(
        service,
        "CanonicalOrderWriterOperator",
        lambda **kwargs: captured.update(kwargs) or kwargs,
    )

    operator = service._production_order_operator(
        plan=_writer_plan(), lease=object(), holder_token="h" * 64
    )

    assert operator == captured
    assert observed_urls == ["canonical_order_writer"]
    assert captured["connection_factory"] is connection_factory
    assert captured["authority_port"]._connection_factory is connection_factory


def test_prepare_canary_order_persists_without_exchange_access(monkeypatch) -> None:
    service = _load_script("canonical_phase9_prepare_canary_order_test")
    plan = _writer_plan()
    lease = Phase9Lease(
        service_key=plan.service_key,
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="8" * 64,
        pid=321,
        acquired_at=NOW - timedelta(seconds=5),
        heartbeat_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
        order_writer_canary_authority=plan.order_writer_canary_authority,
    )
    risk_id = UUID("00000000-0000-4000-8000-000000000091")
    order_id = UUID("00000000-0000-4000-8000-000000000092")
    events = []
    receipts = []

    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(
        service, "_load_plan", lambda _service: (plan, {"status": "RUNNING"})
    )
    monkeypatch.setattr(service.FileLeasePort, "read", lambda _self, _service: lease)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: True)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_read_order_holder_token", lambda: "h" * 64)
    monkeypatch.setattr(
        service,
        "_production_order_operator",
        lambda **_kwargs: SimpleNamespace(
            prepare_canary=lambda **kwargs: events.append(kwargs)
            or SimpleNamespace(
                order_id=order_id,
                request_digest="9" * 64,
                lease_generation=4,
                repeat_noop=False,
            )
        ),
    )
    monkeypatch.setattr(service, "_append_receipt", receipts.append)

    payload = service.prepare_canary_order(plan.plan_digest, risk_id)

    assert events == [{"risk_decision_id": risk_id, "evaluated_at": NOW}]
    assert payload == {
        "status": "READY",
        "order_id": str(order_id),
        "request_digest": "9" * 64,
        "lease_generation": 4,
        "repeat_noop": False,
        "exchange_access": "NONE",
        "order_submission_enabled": False,
        "prepare_receipt_digest": receipts[0].receipt_digest,
    }
    assert receipts[0].action == "ORDER_PREPARE"
    assert receipts[0].status == "CONFIRMED"
    assert receipts[0].details["exchange_access"] == "NONE"
    assert receipts[0].details["order_submission_enabled"] is False


def test_cancel_prepared_canary_order_requires_stopped_services_and_no_exchange(
    monkeypatch,
) -> None:
    service = _load_script("canonical_phase9_cancel_prepared_order_test")
    order_id = UUID("00000000-0000-4000-8000-000000000092")
    receipt_digest = "9" * 64
    evidence_connection = object()
    writer_connection = object()
    calls = []
    status_calls = []

    @contextmanager
    def evidence_connection_context():
        yield evidence_connection

    @contextmanager
    def writer_connection_context():
        yield writer_connection

    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(
        service,
        "status",
        lambda service_key: status_calls.append(service_key)
        or {"service_key": service_key, "loaded": False},
    )
    monkeypatch.setattr(
        service,
        "_phase9_database_url",
        lambda capability: calls.append(("database", capability)) or "writer-url",
    )
    monkeypatch.setattr(
        service,
        "_control_database_url",
        lambda: calls.append(("control-database",)) or "control-url",
    )
    monkeypatch.setattr(
        service,
        "_connection_factory",
        lambda database_url: {
            "control-url": evidence_connection_context,
            "writer-url": writer_connection_context,
        }[database_url],
    )
    monkeypatch.setattr(
        service,
        "assert_pre_dispatch_downstream_absent",
        lambda observed_connection, **kwargs: calls.append(
            ("evidence", observed_connection, kwargs)
        ),
    )
    monkeypatch.setattr(
        service,
        "cancel_prepared_demo_order",
        lambda observed_connection, **kwargs: calls.append(
            ("cancel", observed_connection, kwargs)
        )
        or SimpleNamespace(
            status="CANCELLED",
            order_id=order_id,
            receipt_digest=receipt_digest,
            repeat_noop=False,
        ),
    )
    monkeypatch.setattr(
        service,
        "_production_okx_session_factory",
        lambda: pytest.fail("cancellation must not allocate an OKX session"),
    )

    assert service.cancel_prepared_canary_order(order_id) == {
        "status": "CANCELLED",
        "order_id": str(order_id),
        "receipt_digest": receipt_digest,
        "repeat_noop": False,
        "exchange_access": "NONE",
        "order_submission_enabled": False,
    }
    assert calls == [
        ("control-database",),
        ("evidence", evidence_connection, {"order_id": order_id}),
        ("database", "canonical_order_writer"),
        ("cancel", writer_connection, {"order_id": order_id}),
        ("evidence", evidence_connection, {"order_id": order_id}),
    ]
    assert status_calls == ["long_lived_runtime", "order_writer"]

    monkeypatch.setattr(
        service,
        "status",
        lambda service_key: {
            "service_key": service_key,
            "loaded": service_key == "long_lived_runtime",
        },
    )
    with pytest.raises(CanonicalPhase9SupervisorBlocked) as exc_info:
        service.cancel_prepared_canary_order(order_id)
    assert exc_info.value.code == "BLOCKED_PRE_DISPATCH_SUPERVISOR_LIVE"


def test_get_only_order_replay_appends_server_sealed_noop_receipt(monkeypatch) -> None:
    service = _load_script("canonical_phase9_order_replay_receipt_test")
    plan = _writer_plan()
    lease = Phase9Lease(
        service_key=plan.service_key,
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="8" * 64,
        pid=321,
        acquired_at=NOW - timedelta(seconds=5),
        heartbeat_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
        order_writer_canary_authority=plan.order_writer_canary_authority,
    )
    order_id = UUID("00000000-0000-4000-8000-000000000092")
    result = SimpleNamespace(
        order_id=order_id,
        exchange_order_id="redacted-demo-order",
        receipt_digest="9" * 64,
        repeat_noop=True,
    )
    receipts = []
    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(service, "_load_plan", lambda _service: (plan, {"status": "RUNNING"}))
    monkeypatch.setattr(service.FileLeasePort, "read", lambda _self, _service: lease)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: True)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_read_order_holder_token", lambda: "h" * 64)
    monkeypatch.setattr(
        service,
        "_production_order_operator",
        lambda **_kwargs: SimpleNamespace(
            recover_canary=lambda **_inner_kwargs: result
        ),
    )
    monkeypatch.setattr(service, "_append_receipt", receipts.append)
    payload = service.recover_canary(plan.plan_digest, order_id)
    assert payload["repeat_noop"] is True
    assert payload["replay_evidence_receipt_digest"] == receipts[0].receipt_digest
    assert receipts[0].action == "ORDER_REPLAY"
    assert receipts[0].status == "CONFIRMED"
    assert receipts[0].details == {
        "order_id": str(order_id),
        "order_receipt_digest": "9" * 64,
        "repeat_noop": True,
        "transport_mode": "GET_ONLY",
    }


def test_retry_canary_requires_exact_recovery_plan_and_seals_two_attempt_limit(
    monkeypatch,
) -> None:
    service = _load_script("canonical_phase9_order_retry_receipt_test")
    order_id = UUID("00000000-0000-4000-8000-000000000095")
    plan = _writer_plan(recovery_order_id=order_id)
    lease = Phase9Lease(
        service_key=plan.service_key,
        generation=plan.generation,
        plan_digest=plan.plan_digest,
        release_digest=plan.release_digest,
        deployment_id=plan.deployment_id,
        deployment_capability_digest=plan.deployment_capability_digest,
        image_digest=plan.image_digest,
        holder_token_digest="8" * 64,
        pid=321,
        acquired_at=NOW - timedelta(seconds=5),
        heartbeat_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
        order_writer_canary_authority=plan.order_writer_canary_authority,
        recovery_order_id=order_id,
    )
    result = SimpleNamespace(
        status="ACCEPTED",
        order_id=order_id,
        exchange_order_id="redacted-demo-order",
        receipt_digest="9" * 64,
        repeat_noop=False,
    )
    receipts = []
    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(
        service, "_load_plan", lambda _service: (plan, {"status": "RUNNING"})
    )
    monkeypatch.setattr(service.FileLeasePort, "read", lambda _self, _service: lease)
    monkeypatch.setattr(service.UnixProcessProbe, "is_alive", lambda _self, _pid: True)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_read_order_holder_token", lambda: "h" * 64)
    monkeypatch.setattr(
        service,
        "_production_order_operator",
        lambda **_kwargs: SimpleNamespace(
            retry_canary=lambda **_inner_kwargs: result
        ),
    )
    monkeypatch.setattr(service, "_append_receipt", receipts.append)

    payload = service.retry_canary(plan.plan_digest, order_id)
    assert payload["status"] == "ACCEPTED"
    assert payload["maximum_attempts"] == 2
    assert payload["third_post_allowed"] is False
    assert receipts[0].action == "ORDER_RETRY"
    assert receipts[0].details["attempt_ordinal"] == 2
    assert receipts[0].details["third_post_allowed"] is False


def test_cli_routes_independent_fill_ledger_reconciliation_workers(
    monkeypatch, capsys
) -> None:
    service = _load_script("canonical_phase9_post_order_workers_test")
    order_id = UUID("00000000-0000-4000-8000-000000000093")
    fill_id = UUID("00000000-0000-4000-8000-000000000094")
    observed = []
    monkeypatch.setattr(
        service,
        "collect_canary_fills",
        lambda value: observed.append(("fill", value)) or {"status": "RECORDED"},
    )
    monkeypatch.setattr(
        service,
        "post_canary_ledger",
        lambda value: observed.append(("ledger", value)) or {"status": "POSTED"},
    )
    monkeypatch.setattr(
        service,
        "reconcile_canary",
        lambda value: observed.append(("reconciliation", value))
        or {"status": "SUCCEEDED"},
    )
    assert service.main(
        [
            "collect-canary-fills",
            "--service",
            "fill_writer",
            "--order-id",
            str(order_id),
        ]
    ) == 0
    assert service.main(
        [
            "post-canary-ledger",
            "--service",
            "ledger_writer",
            "--fill-id",
            str(fill_id),
        ]
    ) == 0
    assert service.main(
        [
            "reconcile-canary",
            "--service",
            "reconciliation_writer",
            "--order-id",
            str(order_id),
        ]
    ) == 0
    assert observed == [
        ("fill", order_id),
        ("ledger", fill_id),
        ("reconciliation", order_id),
    ]
    assert [
        json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()
    ] == ["RECORDED", "POSTED", "SUCCEEDED"]


def test_production_runtime_factory_reads_dedicated_signer_and_two_db_identities(
    monkeypatch,
) -> None:
    service = _load_script("canonical_phase9_runtime_factory_test")
    observed = []
    monkeypatch.setattr(
        service,
        "_phase9_database_url",
        lambda capability: observed.append(("database", capability))
        or f"postgresql+psycopg://{capability}@127.0.0.1/freqtrade_ai_v13",
    )
    monkeypatch.setattr(
        "app.canonical_v13.phase9_keychain.read_canonical_service_secret",
        lambda key: observed.append(("signer", key)) or "s" * 64,
    )
    factory = service._production_runtime_worker_factory()
    assert factory._signing_key == "s" * 64
    assert observed == [
        ("signer", service.RUNTIME_SIGNAL_SIGNER_KEYCHAIN_SERVICE),
        ("database", "canonical_runtime_reader"),
        ("database", "canonical_signal_writer"),
    ]


def test_probe_saga_commits_deployment_before_approval_fk_insert(monkeypatch) -> None:
    service = _load_script("canonical_phase9_probe_transaction_order_test")
    events = []

    @contextmanager
    def deployment_factory():
        events.append("deployment-open")
        yield object()
        events.append("deployment-commit")

    @contextmanager
    def approval_factory():
        events.append("approval-open")
        yield object()
        events.append("approval-commit")

    factories = iter((deployment_factory, approval_factory))
    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "_phase9_database_url", lambda _capability: "dsn")
    monkeypatch.setattr(service, "_connection_factory", lambda _dsn: next(factories))
    monkeypatch.setattr(service, "_production_okx_session_factory", lambda: object())
    probe = SimpleNamespace(instrument="BTC-USDT-SWAP")
    monkeypatch.setattr(
        service, "_sealed_probe_for_saga", lambda *_args, **_kwargs: probe
    )
    attestation = SimpleNamespace(
        attestation_id=ATTESTATION_ID,
        attestation_digest=ATTESTATION_DIGEST,
        repeat_noop=False,
    )
    receipt = SimpleNamespace(
        probe_receipt_id=UUID("00000000-0000-4000-8000-000000000095"),
        receipt_digest="f" * 64,
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        repeat_noop=False,
    )
    monkeypatch.setattr(
        service,
        "record_current_canary_attestation",
        lambda *_args, **_kwargs: events.append("attestation-insert")
        or (probe, attestation),
    )
    monkeypatch.setattr(
        service,
        "record_current_canary_probe_receipt",
        lambda *_args, **_kwargs: events.append("probe-receipt-insert") or receipt,
    )
    assert service.probe_canary(DEPLOYMENT_ID)["status"] == "READY"
    assert events == [
        "deployment-open",
        "attestation-insert",
        "deployment-commit",
        "approval-open",
        "probe-receipt-insert",
        "approval-commit",
    ]


def test_expired_orphan_probe_saga_is_reprobed_without_execution_write(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_expired_probe_saga_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path)
    expired = _redacted_probe(
        observed_at=NOW - timedelta(seconds=40),
        expires_at=NOW - timedelta(seconds=10),
    )
    fresh = _redacted_probe(
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    probes = iter((expired, fresh))
    events = []

    @contextmanager
    def session_factory():
        probe = next(probes)
        events.append("authenticated-get")
        yield SimpleNamespace(probe=lambda **_kwargs: probe)

    first = service._sealed_probe_for_saga(
        DEPLOYMENT_ID,
        session_factory,
        evaluated_at=NOW - timedelta(seconds=20),
        linked_probe_receipt_exists=lambda: False,
    )
    assert first is expired
    recovered = service._sealed_probe_for_saga(
        DEPLOYMENT_ID,
        session_factory,
        evaluated_at=NOW,
        linked_probe_receipt_exists=lambda: False,
    )
    assert recovered is fresh
    assert events == ["authenticated-get", "authenticated-get"]
    persisted = json.loads(
        service._probe_saga_path(DEPLOYMENT_ID).read_text(encoding="utf-8")
    )
    assert persisted["probe"]["expires_at"] == fresh.expires_at.isoformat()


def test_expired_linked_probe_saga_blocks_without_reprobe(monkeypatch, tmp_path) -> None:
    service = _load_script("canonical_phase9_linked_probe_saga_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path)
    expired = _redacted_probe(
        observed_at=NOW - timedelta(seconds=40),
        expires_at=NOW - timedelta(seconds=10),
    )
    events = []

    @contextmanager
    def session_factory():
        events.append("authenticated-get")
        yield SimpleNamespace(probe=lambda **_kwargs: expired)

    service._sealed_probe_for_saga(
        DEPLOYMENT_ID,
        session_factory,
        evaluated_at=NOW - timedelta(seconds=20),
        linked_probe_receipt_exists=lambda: False,
    )
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_PHASE9_PROBE_SAGA_LINKED_EXPIRED",
    ):
        service._sealed_probe_for_saga(
            DEPLOYMENT_ID,
            session_factory,
            evaluated_at=NOW,
            linked_probe_receipt_exists=lambda: True,
        )
    assert events == ["authenticated-get"]


def test_phase9_service_preserves_real_sqlalchemy_connection_and_rejects_raw_wrapper(
) -> None:
    from sqlalchemy import create_engine

    service = _load_script("canonical_phase9_sqlalchemy_connection_test")
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        assert service._sqlalchemy_connection(connection) is connection
        assert (
            service._sqlalchemy_connection(SimpleNamespace(connection=connection))
            is connection
        )
        with pytest.raises(
            CanonicalPhase9SupervisorBlocked,
            match="BLOCKED_PHASE9_DATABASE_CONNECTION",
        ):
            service._sqlalchemy_connection(
                SimpleNamespace(connection=connection.connection)
            )


def test_phase9_service_json_default_preserves_decimal_exactly() -> None:
    service = _load_script("canonical_phase9_json_default_test")

    assert service._json_default(Decimal("12.000000000000000000")) == (
        "12.000000000000000000"
    )
    with pytest.raises(TypeError, match="object is not JSON serializable"):
        service._json_default(object())


def test_phase9_service_connection_helper_covers_lock_and_probe_lookups() -> None:
    service = _load_script("canonical_phase9_connection_lookup_test")
    probe_id = UUID("00000000-0000-4000-8000-000000000097")
    attestation_id = UUID("00000000-0000-4000-8000-000000000096")

    class Result:
        def __init__(self, row):
            self.row = row

        def first(self):
            return self.row

        def one_or_none(self):
            return self.row

    class SimulatedSqlAlchemyConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self, row):
            self.row = row
            self.calls = []

        def execute(self, statement, parameters=None):
            self.calls.append((statement, parameters))
            return Result(self.row)

    linked_connection = SimulatedSqlAlchemyConnection((probe_id,))

    @contextmanager
    def linked_factory():
        yield linked_connection

    assert service._linked_probe_receipt_exists(linked_factory, DEPLOYMENT_ID) is True
    service.lock_execution_boundary(
        service._sqlalchemy_connection(linked_connection),
        key="canary-risk-policy:test",
    )
    assert len(linked_connection.calls) == 2

    existing_connection = SimulatedSqlAlchemyConnection(
        (probe_id, attestation_id)
    )
    assert service._existing_policy_probe_identity(
        existing_connection,
        idempotency_key="phase9-policy-atomic-test",
    ) == (probe_id, attestation_id)
    assert len(existing_connection.calls) == 1
    replay_lookup = str(existing_connection.calls[0][0])
    assert "idempotency_key" in replay_lookup
    assert "qualification_decision_id =" not in replay_lookup
    assert "deployment_approval_id =" not in replay_lookup


def test_expired_policy_probe_saga_rotates_without_rewriting_history(
    monkeypatch, tmp_path
) -> None:
    service = _load_script("canonical_phase9_policy_probe_saga_test")
    monkeypatch.setattr(service, "SUPPORT_ROOT", tmp_path)
    expired = _redacted_probe(
        observed_at=NOW - timedelta(seconds=40),
        expires_at=NOW - timedelta(seconds=10),
    )
    fresh = _redacted_probe(
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    probes = iter((expired, fresh))
    events = []

    @contextmanager
    def session_factory():
        probe = next(probes)
        events.append("authenticated-get")
        yield SimpleNamespace(probe=lambda **_kwargs: probe)

    request_digest = "7" * 64
    first = service._sealed_policy_probe(
        DEPLOYMENT_ID,
        session_factory,
        request_digest=request_digest,
        evaluated_at=NOW - timedelta(seconds=20),
    )
    rotated = service._sealed_policy_probe(
        DEPLOYMENT_ID,
        session_factory,
        request_digest=request_digest,
        evaluated_at=NOW,
    )

    assert first is expired
    assert rotated is fresh
    assert events == ["authenticated-get", "authenticated-get"]
    persisted = json.loads(
        service._policy_probe_saga_path(
            DEPLOYMENT_ID, request_digest
        ).read_text(encoding="utf-8")
    )
    assert persisted["request_digest"] == request_digest
    assert persisted["probe"]["expires_at"] == fresh.expires_at.isoformat()


def test_probe_and_policy_commits_receipt_and_policy_in_one_approval_transaction(
    monkeypatch,
) -> None:
    service = _load_script("canonical_phase9_probe_policy_atomic_test")
    qualification_id = UUID("00000000-0000-4000-8000-000000000099")
    approval_id = UUID("00000000-0000-4000-8000-000000000098")
    probe_id = UUID("00000000-0000-4000-8000-000000000097")
    attestation_id = UUID("00000000-0000-4000-8000-000000000096")
    events = []
    approval_connection = object()
    deployment_connection = object()

    @contextmanager
    def connection_factory(name):
        events.append((name, "open"))
        yield approval_connection if name == "approval" else deployment_connection
        events.append((name, "commit"))

    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(
        service,
        "_phase9_database_url",
        lambda identity: "approval" if identity == "canonical_approval_writer" else "deployment",
    )
    monkeypatch.setattr(service, "_connection_factory", lambda name: lambda: connection_factory(name))
    monkeypatch.setattr(service, "_sqlalchemy_connection", lambda value: value)
    monkeypatch.setattr(service, "lock_execution_boundary", lambda *_args, **_kwargs: events.append("lock"))
    monkeypatch.setattr(service, "_existing_policy_probe_identity", lambda *_args, **_kwargs: None)
    probe = _redacted_probe(observed_at=NOW, expires_at=NOW + timedelta(seconds=30))
    monkeypatch.setattr(service, "_sealed_policy_probe", lambda *_args, **_kwargs: probe)
    monkeypatch.setattr(service, "_production_okx_session_factory", lambda: object())
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(
        service,
        "record_current_canary_attestation",
        lambda *_args, **_kwargs: (
            events.append("attestation") or probe,
            SimpleNamespace(
                attestation_id=attestation_id,
                attestation_digest="8" * 64,
            ),
        ),
    )
    monkeypatch.setattr(
        service,
        "record_current_canary_probe_receipt",
        lambda *_args, **_kwargs: (
            events.append("probe-receipt")
            or SimpleNamespace(
                probe_receipt_id=probe_id,
                receipt_digest="9" * 64,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "authorize_canary_risk_policy",
        lambda *_args, **_kwargs: (
            events.append("policy")
            or CanaryRiskPolicyResult(
                UUID("00000000-0000-4000-8000-000000000095"),
                "a" * 64,
                "b" * 64,
                "c" * 64,
                1,
                12,
                NOW,
                NOW + timedelta(minutes=30),
                False,
            )
        ),
    )

    result = service.probe_and_authorize_canary_policy(
        deployment_id=DEPLOYMENT_ID,
        qualification_decision_id=qualification_id,
        deployment_approval_id=approval_id,
        actor_identity="operator:test",
        idempotency_key="phase9-policy-atomic-test",
        reason="acceptance only",
    )

    assert result["status"] == "READY"
    assert result["probe_receipt_id"] == str(probe_id)
    assert events == [
        ("approval", "open"),
        "lock",
        ("deployment", "open"),
        "attestation",
        ("deployment", "commit"),
        "probe-receipt",
        "policy",
        ("approval", "commit"),
    ]


def test_probe_and_policy_exact_replay_does_not_reprobe_exchange(monkeypatch) -> None:
    service = _load_script("canonical_phase9_probe_policy_replay_test")
    qualification_id = UUID("00000000-0000-4000-8000-000000000099")
    approval_id = UUID("00000000-0000-4000-8000-000000000098")
    probe_id = UUID("00000000-0000-4000-8000-000000000097")
    attestation_id = UUID("00000000-0000-4000-8000-000000000096")

    @contextmanager
    def approval_factory():
        yield object()

    monkeypatch.setattr(service, "_require_release_checkout", lambda: None)
    monkeypatch.setattr(service, "_phase9_database_url", lambda identity: identity)
    monkeypatch.setattr(service, "_connection_factory", lambda _name: approval_factory)
    monkeypatch.setattr(service, "_sqlalchemy_connection", lambda value: value)
    monkeypatch.setattr(service, "lock_execution_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_existing_policy_probe_identity",
        lambda *_args, **_kwargs: (probe_id, attestation_id),
    )
    monkeypatch.setattr(
        service,
        "_production_okx_session_factory",
        lambda: pytest.fail("exact replay must not construct a private session"),
    )
    monkeypatch.setattr(
        service,
        "authorize_canary_risk_policy",
        lambda *_args, **_kwargs: CanaryRiskPolicyResult(
            UUID("00000000-0000-4000-8000-000000000095"),
            "a" * 64,
            "b" * 64,
            "c" * 64,
            1,
            12,
            NOW,
            NOW + timedelta(minutes=30),
            True,
        ),
    )

    result = service.probe_and_authorize_canary_policy(
        deployment_id=DEPLOYMENT_ID,
        qualification_decision_id=qualification_id,
        deployment_approval_id=approval_id,
        actor_identity="operator:test",
        idempotency_key="phase9-policy-atomic-test",
        reason="acceptance only",
    )

    assert result["repeat_noop"] is True
    assert result["probe_receipt_id"] == str(probe_id)
