from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.canonical_v13.acceptance_signal_trigger import (
    SOURCE_KIND,
    build_acceptance_worker_receipt,
    issue_acceptance_signal_trigger,
    persist_acceptance_signal,
    read_acceptance_signal_execution,
)
from app.canonical_v13.deployment_approval import approve_demo_deployment
from app.canonical_v13.deployment_control import (
    confirm_production_demo_runtime_observation,
    create_demo_deployment,
)
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked
from app.canonical_v13.models import (
    ACCEPTANCE_SIGNAL_TRIGGERS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RUNTIME_IMAGE_ACCEPTANCES_TABLE,
    SIGNALS_TABLE,
    TARGET_SCORES_TABLE,
)
from app.canonical_v13.phase9_execution_authority import (
    decide_signal_risk_shadow,
    shadow_signal_source_accepted,
)
from app.canonical_v13.phase9_production_runtime import ReleaseBoundReceiptSeal
from app.canonical_v13.phase9_runtime_worker import _digest as runtime_worker_digest
from app.canonical_v13.risk_service import create_production_demo_intent
from app.canonical_v13.runtime_contract import build_runtime_observation_receipt
from tests.test_canonical_v13_phase9_execution_authority import _runtime_spec
from tests.test_canonical_v13_research_evaluation import (
    NOW,
    canonical_connection,  # noqa: F401 - fixture registration
)
from tests.test_canonical_v13_runtime_chain import _qualified


def _seed_runtime(connection):
    _plan_id, decision = _qualified(connection)
    approval = approve_demo_deployment(
        connection,
        qualification_decision_id=decision.qualification_decision_id,
        actor_identity="acceptance-trigger-human",
        reason="explicit isolated acceptance-only authorization",
    )
    deployment = create_demo_deployment(
        connection, deployment_approval_id=approval.deployment_approval_id
    )
    spec = _runtime_spec(connection, approval, deployment)
    runtime_id = uuid4()
    observation = build_runtime_observation_receipt(
        runtime_instance_id=runtime_id,
        launch_spec=spec,
        status="HEALTHY",
        observed_at=NOW,
        evidence_class="PRODUCTION_DEMO_RUNTIME",
    )
    confirm_production_demo_runtime_observation(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity=spec.runtime_identity,
        image_digest=spec.image_digest,
        credential_reference=spec.credential_reference,
        receipt=observation,
        evaluated_at=NOW,
    )
    image_id = uuid4()
    connection.execute(
        RUNTIME_IMAGE_ACCEPTANCES_TABLE.insert().values(
            id=image_id,
            source_commit="a" * 40,
            release_digest="1" * 64,
            source_tree_digest="2" * 64,
            build_recipe_digest="3" * 64,
            base_image_digest="4" * 64,
            platform="linux",
            architecture="arm64",
            image_manifest_digest=spec.image_digest,
            image_config_digest="5" * 64,
            entrypoint_digest="6" * 64,
            security_profile_digest="7" * 64,
            sbom_digest="8" * 64,
            provenance_digest="9" * 64,
            builder_identity="isolated-builder",
            provenance_json={"network": "none", "secret_count": 0},
            request_digest="b" * 64,
            receipt_digest="c" * 64,
            accepted_by="isolated-runtime-image-authority",
            accepted_at=NOW,
            demo_only=True,
            allow_real_funds=False,
        )
    )
    qualification = connection.execute(
        select(QUALIFICATION_DECISIONS_TABLE).where(
            QUALIFICATION_DECISIONS_TABLE.c.id
            == decision.qualification_decision_id
        )
    ).mappings().one()
    return approval, deployment, spec, runtime_id, image_id, qualification


def _refresh_runtime(connection, *, deployment, spec, runtime_id, observed_at):
    receipt = build_runtime_observation_receipt(
        runtime_instance_id=runtime_id,
        launch_spec=spec,
        status="HEALTHY",
        observed_at=observed_at,
        evidence_class="PRODUCTION_DEMO_RUNTIME",
    )
    confirm_production_demo_runtime_observation(
        connection,
        deployment_id=deployment.deployment_id,
        runtime_identity=spec.runtime_identity,
        image_digest=spec.image_digest,
        credential_reference=spec.credential_reference,
        receipt=receipt,
        evaluated_at=observed_at,
    )


def test_one_shot_acceptance_trigger_signed_replay_and_shadow_chain(
    canonical_connection,
) -> None:
    approval, deployment, spec, runtime_id, image_id, qualification = _seed_runtime(
        canonical_connection
    )
    research_before = {
        "scores": canonical_connection.execute(
            select(func.count()).select_from(TARGET_SCORES_TABLE)
        ).scalar_one(),
        "qualifications": canonical_connection.execute(
            select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
        ).scalar_one(),
        "optimizations": canonical_connection.execute(
            select(func.count()).select_from(OPTIMIZATION_RUNS_TABLE)
        ).scalar_one(),
    }
    issued = issue_acceptance_signal_trigger(
        canonical_connection,
        qualification_decision_id=qualification["id"],
        deployment_approval_id=approval.deployment_approval_id,
        deployment_id=deployment.deployment_id,
        runtime_instance_id=runtime_id,
        runtime_image_acceptance_id=image_id,
        actor_identity="operator:isolated",
        idempotency_key="acceptance-trigger-exact-once",
        issued_at=NOW + timedelta(seconds=30),
    )
    assert issued.source_kind == SOURCE_KIND
    assert issued.scheduled_at.minute % 15 == 0
    assert issued.expires_at - issued.scheduled_at == timedelta(minutes=2)
    replay = issue_acceptance_signal_trigger(
        canonical_connection,
        qualification_decision_id=qualification["id"],
        deployment_approval_id=approval.deployment_approval_id,
        deployment_id=deployment.deployment_id,
        runtime_instance_id=runtime_id,
        runtime_image_acceptance_id=image_id,
        actor_identity="operator:isolated",
        idempotency_key="acceptance-trigger-exact-once",
        issued_at=NOW + timedelta(minutes=1),
    )
    assert replay.trigger_id == issued.trigger_id
    assert replay.receipt_digest == issued.receipt_digest
    assert replay.repeat_noop is True
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_TRIGGER_ALREADY_ISSUED",
    ):
        issue_acceptance_signal_trigger(
            canonical_connection,
            qualification_decision_id=qualification["id"],
            deployment_approval_id=approval.deployment_approval_id,
            deployment_id=deployment.deployment_id,
            runtime_instance_id=runtime_id,
            runtime_image_acceptance_id=image_id,
            actor_identity="operator:isolated",
            idempotency_key="acceptance-trigger-forbidden-reset",
            issued_at=NOW + timedelta(minutes=1),
        )

    seal = ReleaseBoundReceiptSeal("d" * 64, "secret-safe-signing-key-" + "x" * 48)
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_TRIGGER_NOT_EXECUTABLE",
    ):
        build_acceptance_worker_receipt(
            canonical_connection,
            trigger_id=issued.trigger_id,
            plan_digest="e" * 64,
            observed_at=issued.scheduled_at - timedelta(microseconds=1),
            signer=seal,
        )
    _refresh_runtime(
        canonical_connection,
        deployment=deployment,
        spec=spec,
        runtime_id=runtime_id,
        observed_at=issued.scheduled_at,
    )
    worker = build_acceptance_worker_receipt(
        canonical_connection,
        trigger_id=issued.trigger_id,
        plan_digest="e" * 64,
        observed_at=issued.scheduled_at,
        signer=seal,
    )
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_WORKER_RECEIPT",
    ):
        persist_acceptance_signal(
            canonical_connection,
            trigger_id=issued.trigger_id,
            worker_receipt=replace(worker, signature="0" * 64),
            verifier=seal,
            persisted_at=issued.scheduled_at,
        )
    assert worker.signal_candidate is not None
    forged_deployment_id = uuid4()
    forged_signal_json = dict(worker.signal_candidate.signal_json)
    forged_signal_json["deployment_id"] = str(forged_deployment_id)
    forged_candidate_digest = runtime_worker_digest(
        {
            "contract": worker.signal_candidate.contract,
            **forged_signal_json,
        }
    )
    forged_candidate = replace(
        worker.signal_candidate,
        deployment_id=forged_deployment_id,
        signal_json=forged_signal_json,
        candidate_digest=forged_candidate_digest,
    )
    forged_unsigned = {
        "contract": worker.contract,
        "stage": worker.stage,
        "status": worker.status,
        "reason_code": worker.reason_code,
        "plan_digest": worker.plan_digest,
        "runtime_instance_id": worker.runtime_instance_id,
        "runtime_receipt_digest": worker.runtime_receipt_digest,
        "observed_at": worker.observed_at,
        "order_submission_enabled": worker.order_submission_enabled,
        "persistence_target": worker.persistence_target,
        "signal_candidate_digest": forged_candidate_digest,
        "signer_key_id": worker.signer_key_id,
        "signature_algorithm": worker.signature_algorithm,
    }
    forged_receipt_digest = runtime_worker_digest(forged_unsigned)
    forged_worker = replace(
        worker,
        signal_candidate=forged_candidate,
        signal_candidate_digest=forged_candidate_digest,
        receipt_digest=forged_receipt_digest,
        signature=seal.sign_digest(forged_receipt_digest),
    )
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_SIGNAL_CONTRACT",
    ):
        persist_acceptance_signal(
            canonical_connection,
            trigger_id=issued.trigger_id,
            worker_receipt=forged_worker,
            verifier=seal,
            persisted_at=issued.scheduled_at,
        )
    persisted = persist_acceptance_signal(
        canonical_connection,
        trigger_id=issued.trigger_id,
        worker_receipt=worker,
        verifier=seal,
        persisted_at=issued.scheduled_at,
    )
    duplicate = persist_acceptance_signal(
        canonical_connection,
        trigger_id=issued.trigger_id,
        worker_receipt=worker,
        verifier=seal,
        persisted_at=issued.scheduled_at + timedelta(seconds=1),
    )
    assert duplicate.signal_id == persisted.signal_id
    assert duplicate.signal_digest == persisted.signal_digest
    assert duplicate.repeat_noop is True
    signal = canonical_connection.execute(
        select(SIGNALS_TABLE).where(SIGNALS_TABLE.c.id == persisted.signal_id)
    ).mappings().one()
    assert signal["source_kind"] == SOURCE_KIND
    assert signal["signal_json"]["natural_signal"] is False
    assert signal["worker_receipt_digest"] == worker.receipt_digest
    assert shadow_signal_source_accepted(signal), signal
    forged = dict(signal)
    forged["signal_json"] = deepcopy(signal["signal_json"])
    forged["signal_json"]["natural_signal"] = True
    assert shadow_signal_source_accepted(forged) is False
    savepoint = canonical_connection.begin_nested()
    canonical_connection.execute(
        SIGNALS_TABLE.update()
        .where(SIGNALS_TABLE.c.id == persisted.signal_id)
        .values(signal_json=forged["signal_json"])
    )
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_SIGNAL_REPLAY_DRIFT",
    ):
        read_acceptance_signal_execution(
            canonical_connection, trigger_id=issued.trigger_id
        )
    savepoint.rollback()
    intent_id = create_production_demo_intent(
        canonical_connection,
        signal_id=persisted.signal_id,
        intent_json={
            "contract": "canonical-v13-demo-trade-intent-v1",
            "execution_target": "OKX_DEMO",
            "allow_real_funds": False,
            "acceptance_only": True,
            "source_kind": SOURCE_KIND,
            "signal_digest": persisted.signal_digest,
            "instrument": "BTC-USDT-SWAP",
            "notional": "1",
            "exchange_body": {
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "side": "buy",
                "posSide": "long",
            },
        },
    )
    shadow = decide_signal_risk_shadow(
        canonical_connection,
        trade_intent_id=intent_id,
        evaluated_at=issued.scheduled_at + timedelta(seconds=1),
    )
    assert shadow.status == "RISK_ACCEPTED"
    assert shadow.repeat_noop is False
    assert canonical_connection.execute(
        select(func.count()).select_from(ACCEPTANCE_SIGNAL_TRIGGERS_TABLE)
    ).scalar_one() == 1
    assert research_before == {
        "scores": canonical_connection.execute(
            select(func.count()).select_from(TARGET_SCORES_TABLE)
        ).scalar_one(),
        "qualifications": canonical_connection.execute(
            select(func.count()).select_from(QUALIFICATION_DECISIONS_TABLE)
        ).scalar_one(),
        "optimizations": canonical_connection.execute(
            select(func.count()).select_from(OPTIMIZATION_RUNS_TABLE)
        ).scalar_one(),
    }


def test_acceptance_trigger_expires_without_signal(canonical_connection) -> None:
    approval, deployment, _spec, runtime_id, image_id, qualification = _seed_runtime(
        canonical_connection
    )
    issued = issue_acceptance_signal_trigger(
        canonical_connection,
        qualification_decision_id=qualification["id"],
        deployment_approval_id=approval.deployment_approval_id,
        deployment_id=deployment.deployment_id,
        runtime_instance_id=runtime_id,
        runtime_image_acceptance_id=image_id,
        actor_identity="operator:isolated",
        idempotency_key="acceptance-trigger-expiry",
        issued_at=NOW + timedelta(seconds=30),
    )
    seal = ReleaseBoundReceiptSeal("d" * 64, "secret-safe-signing-key-" + "x" * 48)
    with pytest.raises(
        CanonicalExecutionChainBlocked,
        match="BLOCKED_ACCEPTANCE_TRIGGER_NOT_EXECUTABLE",
    ):
        build_acceptance_worker_receipt(
            canonical_connection,
            trigger_id=issued.trigger_id,
            plan_digest="e" * 64,
            observed_at=issued.expires_at,
            signer=seal,
        )
    assert canonical_connection.execute(
        select(func.count()).select_from(SIGNALS_TABLE)
    ).scalar_one() == 0
