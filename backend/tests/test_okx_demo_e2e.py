from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.services.okx_demo_e2e import (
    AcceptanceMode,
    CleanupResult,
    DeterministicOfflineGateway,
    EvidenceReference,
    OFFLINE_GATEWAY_KIND,
    Preflight,
    REAL_GATEWAY_KIND,
    REQUIRED_SCENARIOS,
    ScenarioResult,
    StateSnapshot,
    SurfaceVerification,
    database_fingerprint,
    run_acceptance,
)


class OfflineGateway(DeterministicOfflineGateway):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_called = False

    def cleanup(self, baseline: StateSnapshot) -> CleanupResult:
        self.cleanup_called = True
        return super().cleanup(baseline)


class FakeControlledGateway(OfflineGateway):
    gateway_kind = REAL_GATEWAY_KIND

    def preflight(self, mode):
        return Preflight(ready=True)


def test_offline_ci_covers_every_declared_scenario_without_real_demo() -> None:
    gateway = DeterministicOfflineGateway()

    report = run_acceptance(gateway, mode=AcceptanceMode.OFFLINE_CI)

    assert report.status == "PASSED"
    assert report.real_demo_executed is False
    assert tuple(gateway.scenarios_seen) == REQUIRED_SCENARIOS
    assert len(report.scenarios) == len(REQUIRED_SCENARIOS)
    assert all(len(result.assertions) >= 3 for result in report.scenarios)
    assert gateway._posts["offline-duplicate-request"] == 1
    assert report.cleanup is not None
    assert report.cleanup.final_snapshot.open_order_ids == ()
    assert report.cleanup.final_snapshot.positions == {}
    assert report.database_fingerprint


def test_real_mode_is_not_run_without_explicit_authorization() -> None:
    gateway = FakeControlledGateway()

    report = run_acceptance(gateway, mode=AcceptanceMode.CONTROLLED_REAL)

    assert report.status == "NOT_RUN"
    assert report.reason == "EXPLICIT_REAL_DEMO_AUTHORIZATION_REQUIRED"
    assert gateway.scenarios_seen == []
    assert gateway.cleanup_called is False


def test_real_mode_rejects_offline_or_second_writer_gateway() -> None:
    gateway = DeterministicOfflineGateway()

    report = run_acceptance(
        gateway,
        mode=AcceptanceMode.CONTROLLED_REAL,
        allow_real_demo=True,
    )

    assert report.status == "BLOCKED"
    assert report.reason == "GATEWAY_MUST_USE_NORMAL_PIPELINE"
    assert gateway.scenarios_seen == []


def test_preflight_blocker_stops_before_baseline_and_scenarios() -> None:
    class BlockedGateway(OfflineGateway):
        def preflight(self, mode):
            return Preflight(ready=False, blockers=("DEMO_CREDENTIALS_UNAVAILABLE",))

    gateway = BlockedGateway()
    report = run_acceptance(
        gateway,
        mode=AcceptanceMode.OFFLINE_CI,
    )

    assert report.status == "BLOCKED"
    assert report.reason == "DEMO_CREDENTIALS_UNAVAILABLE"
    assert report.real_demo_executed is False


def test_fake_normal_pipeline_marker_cannot_authorize_real_demo() -> None:
    gateway = FakeControlledGateway()
    report = run_acceptance(
        gateway,
        mode=AcceptanceMode.CONTROLLED_REAL,
        allow_real_demo=True,
    )

    assert report.status == "BLOCKED"
    assert report.reason == "NORMAL_PIPELINE_GATEWAY_NOT_INTEGRATED"
    assert gateway.scenarios_seen == []
    assert gateway.cleanup_called is False


def test_offline_scenario_invariant_failure_cannot_pass() -> None:
    class BrokenIdempotencyGateway(OfflineGateway):
        def _post_once(self, client_id: str, state: str) -> None:
            self._posts[client_id] = self._posts.get(client_id, 0) + 1
            self._orders[client_id] = state

    gateway = BrokenIdempotencyGateway()
    report = run_acceptance(gateway, mode=AcceptanceMode.OFFLINE_CI)

    assert report.status == "FAILED"
    assert report.reason == "FRAMEWORK_EXCEPTION_RUNTIMEERROR"
    assert gateway.cleanup_called is True


def test_failed_scenario_stays_failed_after_successful_cleanup() -> None:
    class FailedScenarioGateway(OfflineGateway):
        def run_scenario(self, name: str) -> ScenarioResult:
            result = super().run_scenario(name)
            if name == "BUSINESS_REJECTION":
                return replace(result, passed=False, reason="SCODE_FAILURE_ACCEPTED")
            return result

    report = run_acceptance(
        FailedScenarioGateway(),
        mode=AcceptanceMode.OFFLINE_CI,
    )

    assert report.status == "FAILED"
    assert report.reason == "ONE_OR_MORE_SCENARIOS_FAILED"


def test_surface_mismatch_fails_even_when_scenarios_pass() -> None:
    class MismatchGateway(OfflineGateway):
        def verify_surfaces(self):
            return SurfaceVerification(
                consistent=False,
                reason="API_DATABASE_MISMATCH",
                evidence=(
                    EvidenceReference(
                        "okx_e2e_runs",
                        1,
                        source="OFFLINE_FIXTURE",
                    ),
                ),
            )

    report = run_acceptance(
        MismatchGateway(),
        mode=AcceptanceMode.OFFLINE_CI,
    )

    assert report.status == "FAILED"
    assert report.reason == "PAGE_API_DATABASE_ARTIFACT_EXCHANGE_MISMATCH"


@pytest.mark.parametrize(
    "cleanup_mutator,expected_reason",
    (
        (
            lambda baseline: CleanupResult(
                verified=False,
                reason="OPEN_ORDER_REMAINS",
                final_snapshot=baseline,
                evidence=(),
            ),
            "OPEN_ORDER_REMAINS",
        ),
        (
            lambda baseline: CleanupResult(
                verified=True,
                reason="DONE",
                final_snapshot=replace(
                    baseline,
                    database_fingerprint=database_fingerprint("another-database"),
                ),
                evidence=(),
            ),
            "DATABASE_FINGERPRINT_CHANGED",
        ),
        (
            lambda baseline: CleanupResult(
                verified=True,
                reason="DONE",
                final_snapshot=replace(
                    baseline,
                    open_order_ids=("unexpected-open-order",),
                ),
                evidence=(),
            ),
            "FINAL_STATE_DIFFERS_FROM_BASELINE",
        ),
    ),
)
def test_cleanup_uncertainty_requires_recovery(
    cleanup_mutator,
    expected_reason: str,
) -> None:
    class DirtyGateway(OfflineGateway):
        def cleanup(self, baseline):
            self.cleanup_called = True
            return cleanup_mutator(baseline)

    report = run_acceptance(
        DirtyGateway(),
        mode=AcceptanceMode.OFFLINE_CI,
    )

    expected_status = (
        "DRIFTED"
        if expected_reason
        in {"DATABASE_FINGERPRINT_CHANGED", "FINAL_STATE_DIFFERS_FROM_BASELINE"}
        else "RECOVERY_REQUIRED"
    )
    assert report.status == expected_status
    assert report.reason == expected_reason


def test_pass_requires_scenario_database_lineage() -> None:
    class MissingEvidenceGateway(OfflineGateway):
        def run_scenario(self, name: str) -> ScenarioResult:
            result = super().run_scenario(name)
            return replace(
                result,
                evidence=(),
            )

    report = run_acceptance(
        MissingEvidenceGateway(),
        mode=AcceptanceMode.OFFLINE_CI,
    )

    assert report.status == "FAILED"
    assert report.reason == "FRAMEWORK_EXCEPTION_RUNTIMEERROR"


def test_artifact_must_stay_in_managed_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="managed E2E root"):
        run_acceptance(
            DeterministicOfflineGateway(),
            mode=AcceptanceMode.OFFLINE_CI,
            artifact_path=tmp_path / "report.json",
        )


def test_database_fingerprint_never_accepts_a_database_url() -> None:
    with pytest.raises(ValueError, match="safe opaque identifier"):
        database_fingerprint("postgresql://user:password@localhost/freqtrade_ai")
