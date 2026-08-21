from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
from uuid import UUID

import pytest

from app.canonical_v13.phase9_runtime_supervisor import (
    CanonicalPhase9SupervisorBlocked,
    OrderWriterCanaryAuthority,
    Phase9Lease,
    build_launch_plan,
    build_order_writer_canary_authority,
    claim_lease,
    heartbeat_lease,
    release_lease,
)


NOW = datetime(2026, 8, 21, 1, 2, 3, tzinfo=timezone.utc)
PLAN_ID = UUID("10000000-0000-4000-8000-000000000001")
DEPLOYMENT_ID = UUID("30000000-0000-4000-8000-000000000003")
RELEASE_DIGEST = "a" * 64
CAPABILITY_DIGEST = "b" * 64
IMAGE_DIGEST = "c" * 64
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

    def verify(self, authority, *, observed_at):
        self.calls.append((authority, observed_at))
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
        "effective_leverage": "14",
        "position_policy": "LONG_ONLY",
    }
    return build_order_writer_canary_authority(**{**values, **changes})


def _writer_plan(*, authority=None):
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
        plan_id=UUID("20000000-0000-4000-8000-000000000002"),
    )


def _runtime_plan(*, generation: int = 1):
    return build_launch_plan(
        service_key="long_lived_runtime",
        stage="NO_ORDER_SOAK",
        generation=generation,
        prepared_at=NOW,
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        image_digest=IMAGE_DIGEST,
        plan_id=PLAN_ID,
    )


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure_roots(module, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(module, "SUPPORT_ROOT", tmp_path / "support")
    monkeypatch.setattr(module, "LAUNCH_AGENT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(module, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(module, "BACKEND_PYTHON", tmp_path / "venv" / "python")
    monkeypatch.setattr(module, "_require_release_checkout", lambda: RELEASE_DIGEST)
    monkeypatch.setattr(module, "_now", lambda: NOW)


def _prepare_runtime(service, stage: str = "NO_ORDER_SOAK"):
    return service.prepare(
        "long_lived_runtime",
        stage,
        release_digest=RELEASE_DIGEST,
        deployment_id=DEPLOYMENT_ID,
        deployment_capability_digest=CAPABILITY_DIGEST,
        image_digest=IMAGE_DIGEST,
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
        image_digest=None,
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
        ("effective_leverage", "10"),
    ),
)
def test_writer_plan_digest_binds_every_canary_authority_fact(field, value) -> None:
    baseline = _writer_plan()
    changed_authority = _writer_authority(**{field: value})
    changed = _writer_plan(authority=changed_authority)
    assert changed.plan_digest != baseline.plan_digest


def test_writer_plan_requires_exact_long_only_current_authority() -> None:
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
        _writer_authority(effective_leverage="14.1")
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        _writer_plan(authority=_writer_authority(expires_at=NOW))
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
        "image_digest": IMAGE_DIGEST,
        "plan_id": PLAN_ID,
    }
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
            image_digest=IMAGE_DIGEST,
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
            image_digest=None,
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


def test_writer_confirmation_blocks_expired_frozen_attestation_before_launchctl(
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
    with pytest.raises(
        CanonicalPhase9SupervisorBlocked,
        match="BLOCKED_ORDER_WRITER_CANARY_AUTHORITY",
    ):
        service.confirm(
            "order_writer",
            prepared["plan_digest"],
            authority_port=AuthorityPort(authority),
        )
    assert calls == []


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
    assert persisted["effective_leverage"] == "14"
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
    assert receipts[3]["details"] == {
        "bootstrap_required": False,
        "orphan_cleaned": False,
    }
    assert calls[-1] == (
        "launchctl",
        "bootout",
        service._launchctl_target("long_lived_runtime"),
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
        return subprocess.CompletedProcess(command, 0, "", "")

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
            image_digest=IMAGE_DIGEST,
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
    assert len(calls) == stop_call_count
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
                "--image-digest",
                IMAGE_DIGEST,
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
                "image_digest": IMAGE_DIGEST,
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
        "--effective-leverage",
        "14.0",
        "--position-policy",
        "LONG_ONLY",
    ]
    assert service.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PREPARED"
    authority = observed[0][2]["order_writer_canary_authority"]
    assert authority == _writer_authority()
    assert authority.effective_leverage == "14"
