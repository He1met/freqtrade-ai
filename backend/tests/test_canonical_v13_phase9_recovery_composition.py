from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.canonical_v13.phase9_recovery_acceptance import (
    CanonicalPhase9RecoveryAcceptanceBlocked,
)
from app.canonical_v13.phase9_recovery_composition import (
    RECOVERY_ACTOR_IDENTITY,
    accept_phase9_recovery_soak,
)
from app.canonical_v13.phase9_runtime_supervisor import build_lifecycle_receipt


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
QUALIFICATION_ID = UUID("00000000-0000-4000-8000-000000000001")
ORDER_ID = UUID("00000000-0000-4000-8000-000000000002")
RUNTIME_ID = UUID("00000000-0000-4000-8000-000000000003")
ORDER_DIGEST = "a" * 64
RUNTIME_DIGEST = "b" * 64
POLICY_ID = UUID("00000000-0000-4000-8000-000000000004")


class _Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def all(self):
        return self.value

    def one_or_none(self):
        return self.value


class _Connection:
    def __init__(self):
        self.results = [
            [
                {
                    "id": ORDER_ID,
                    "receipt_digest": ORDER_DIGEST,
                    "exchange_order_id": "redacted-demo-order",
                    "runtime_instance_id": RUNTIME_ID,
                }
            ],
            [{"id": POLICY_ID}],
            {
                "runtime_instance_id": RUNTIME_ID,
                "status": "HEALTHY",
                "evidence_class": "PRODUCTION_DEMO_RUNTIME",
                "order_writer_capability": False,
                "receipt_digest": RUNTIME_DIGEST,
                "observed_at": NOW - timedelta(seconds=1),
            },
        ]

    def execute(self, _statement):
        return _Result(self.results.pop(0))


def _receipt(service, action, status, observed_at, *, details=None):
    return build_lifecycle_receipt(
        service_key=service,
        action=action,
        status=status,
        generation=2 if service == "long_lived_runtime" else 1,
        observed_at=observed_at,
        plan_digest=("c" if service == "long_lived_runtime" else "d") * 64,
        details=details,
    )


class _Supervisor:
    def __init__(self, *, loaded=False, lease=None):
        self.loaded = loaded
        self.lease = lease
        self.receipts = {
            ("order_writer", "ORDER_REPLAY"): _receipt(
                "order_writer",
                "ORDER_REPLAY",
                "CONFIRMED",
                NOW - timedelta(seconds=5),
                details={
                    "order_id": str(ORDER_ID),
                    "order_receipt_digest": ORDER_DIGEST,
                    "repeat_noop": True,
                    "transport_mode": "GET_ONLY",
                },
            ),
            ("order_writer", "STOP"): _receipt(
                "order_writer", "STOP", "STOPPED", NOW - timedelta(seconds=4)
            ),
            ("long_lived_runtime", "RESTART"): _receipt(
                "long_lived_runtime",
                "RESTART",
                "CONFIRMED",
                NOW - timedelta(seconds=3),
            ),
            ("long_lived_runtime", "RECOVER"): _receipt(
                "long_lived_runtime",
                "RECOVER",
                "NO_OP",
                NOW - timedelta(seconds=2),
            ),
        }

    def latest_lifecycle(self, *, service_key, action):
        return self.receipts[(service_key, action)]

    def launch_agent_loaded(self, _service_key):
        return self.loaded

    def file_lease(self, _service_key):
        return self.lease

    def process_alive(self, _pid):
        return True


def test_acceptance_derives_exact_db_and_supervisor_evidence(monkeypatch) -> None:
    captured = {}

    def record(_connection, *, evidence, actor_identity):
        captured.update(evidence=evidence, actor_identity=actor_identity)
        return {"status": "ACCEPTED", "receipt_digest": "e" * 64, "repeat_noop": False}

    monkeypatch.setattr(
        "app.canonical_v13.phase9_recovery_composition.record_phase9_recovery_acceptance",
        record,
    )
    monkeypatch.setattr(
        "app.canonical_v13.phase9_recovery_composition.validate_terminated_canary_risk_policy",
        lambda _connection, *, policy_id: SimpleNamespace(
            policy_id=policy_id, termination_digest="f" * 64
        ),
    )
    result = accept_phase9_recovery_soak(
        _Connection(),
        qualification_decision_id=QUALIFICATION_ID,
        supervisor=_Supervisor(),
        observed_at=NOW,
    )
    assert result["status"] == "ACCEPTED"
    assert captured["actor_identity"] == RECOVERY_ACTOR_IDENTITY
    evidence = captured["evidence"]
    assert evidence.order_replay_receipt_digest == ORDER_DIGEST
    assert evidence.observability_receipt_digest == RUNTIME_DIGEST
    assert evidence.active_supervisor_lease_count == 0
    assert evidence.zombie_process_count == 0


@pytest.mark.parametrize("loaded,lease", [(True, None), (False, SimpleNamespace(pid=99))])
def test_acceptance_blocks_loaded_agent_or_remaining_file_lease(
    loaded, lease
) -> None:
    with pytest.raises(CanonicalPhase9RecoveryAcceptanceBlocked):
        accept_phase9_recovery_soak(
            _Connection(),
            qualification_decision_id=QUALIFICATION_ID,
            supervisor=_Supervisor(loaded=loaded, lease=lease),
            observed_at=NOW,
        )


def test_acceptance_blocks_non_noop_order_replay() -> None:
    supervisor = _Supervisor()
    supervisor.receipts[("order_writer", "ORDER_REPLAY")] = _receipt(
        "order_writer",
        "ORDER_REPLAY",
        "RECOVERED",
        NOW - timedelta(seconds=5),
        details={
            "order_id": str(ORDER_ID),
            "order_receipt_digest": ORDER_DIGEST,
            "repeat_noop": False,
            "transport_mode": "GET_ONLY",
        },
    )
    with pytest.raises(
        CanonicalPhase9RecoveryAcceptanceBlocked,
        match="BLOCKED_RECOVERY_SUPERVISOR_SEQUENCE",
    ):
        accept_phase9_recovery_soak(
            _Connection(),
            qualification_decision_id=QUALIFICATION_ID,
            supervisor=supervisor,
            observed_at=NOW,
        )
