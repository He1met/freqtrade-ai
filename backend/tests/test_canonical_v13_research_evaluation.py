from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4

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
from app.canonical_v13.models import (
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    OPTIMIZATION_RUNS_TABLE,
    OPTIMIZATION_TRIALS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    QUALIFICATION_WINDOW_EVIDENCE_TABLE,
    RESEARCH_TARGETS_TABLE,
    TARGET_SCORES_TABLE,
    VALIDATION_ATTEMPTS_TABLE,
    VALIDATION_PLANS_TABLE,
    VALIDATION_PLAN_WINDOWS_TABLE,
    VALIDATION_WINDOW_RESULTS_TABLE,
)
from app.canonical_v13.research_evaluation import (
    CanonicalEvaluationBlocked,
    gate_optimization,
    qualify_target,
    score_target,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
SCHEMA = {"type": "object", "additionalProperties": False}
ADAPTER_DIGEST = "a" * 64
MANIFEST_DIGEST = "b" * 64
EXECUTOR_DIGEST = "c" * 64
ATTEMPT_REQUEST_DIGEST = "d" * 64
ATTEMPT_RECEIPT_DIGEST = "e" * 64


@pytest.fixture
def canonical_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="evaluation-test")
    connection = raw.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    try:
        yield connection
    finally:
        raw.close()
        engine.dispose()


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _draft(connection, kind, payload, *, dependencies=()):
    return create_configuration_draft(
        connection,
        profile_key=f"evaluation-{kind.lower()}",
        configuration_kind=kind,
        scope_key="isolated-evaluation",
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


def _submission(connection):
    result = controlled_submit_latest(
        connection,
        caller_identity="evaluation-fixture",
        idempotency_key="strategy-1",
        display_name="Evaluation Strategy",
        snapshot=ExternalSourceEntrySnapshot(
            archive_snapshot_digest="f" * 64,
            source_entry_key="strategies/evaluation.py",
            source_strategy_key="external-evaluation",
            current_version_id="external-v1",
            versions=(
                ExternalVersionSnapshot(
                    source_strategy_key="external-evaluation",
                    version_id="external-v1",
                    version_number=1,
                    artifact_bytes=(
                        b"from freqtrade.strategy import IStrategy\n"
                        b"class EvaluationStrategy(IStrategy):\n    pass\n"
                    ),
                ),
            ),
        ),
    )
    return result.strategy_version_id


def _bundle(connection, *, aggregation="MEAN", quality_threshold=50):
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
    windows_payload = {
        "windows": [
            {
                "window_key": "required-recent",
                "required": True,
                "start_at": NOW.isoformat(),
                "end_at": (NOW + timedelta(hours=6)).isoformat(),
                "coverage": {"minimum_closed_candles": 72},
            },
            {
                "window_key": "optional-context",
                "required": False,
                "start_at": (NOW - timedelta(hours=6)).isoformat(),
                "end_at": NOW.isoformat(),
                "coverage": {"minimum_closed_candles": 72},
            },
            {
                "window_key": "required-stress",
                "required": True,
                "start_at": (NOW - timedelta(hours=12)).isoformat(),
                "end_at": (NOW - timedelta(hours=6)).isoformat(),
                "coverage": {"minimum_closed_candles": 72},
            },
        ]
    }
    window = _draft(connection, "WINDOW", windows_payload)
    target_snapshot = validate_configuration_version(
        connection, version_id=target.version_id, adapter_manifest_digest=MANIFEST_DIGEST
    )
    window_snapshot = validate_configuration_version(
        connection, version_id=window.version_id, adapter_manifest_digest=MANIFEST_DIGEST
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
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    payloads = {
        "DIVERSITY": {
            "rules": [
                {
                    "rule_key": "correlation",
                    "algorithm": "correlation-v1",
                    "metric": "return_correlation",
                    "operator": "<=",
                    "threshold": 0.8,
                }
            ]
        },
        "QUALITY_QUALIFICATION": {
            "minimum_score": quality_threshold,
            "required_window_gates": [
                {
                    "gate_key": "minimum-trades",
                    "metric": "trade_count",
                    "operator": ">=",
                    "threshold": 10,
                }
            ],
        },
        "SCORING": {
            "window_aggregation": aggregation,
            "components": [
                {
                    "component_key": "return",
                    "metric": "net_return",
                    "weight": 0.75,
                    "direction": "maximize",
                    "minimum": -1,
                    "maximum": 1,
                },
                {
                    "component_key": "drawdown",
                    "metric": "drawdown",
                    "weight": 0.25,
                    "direction": "minimize",
                    "minimum": 0,
                    "maximum": 1,
                },
            ],
        },
    }
    drafts = {"TARGET": target, "WINDOW": window, "GENERATION": generation}
    snapshots = {
        "TARGET": target_snapshot,
        "WINDOW": window_snapshot,
        "GENERATION": generation_snapshot,
    }
    for kind in ("DIVERSITY", "QUALITY_QUALIFICATION", "SCORING"):
        draft = _draft(connection, kind, payloads[kind])
        drafts[kind] = draft
        snapshots[kind] = validate_configuration_version(
            connection,
            version_id=draft.version_id,
            adapter_manifest_digest=MANIFEST_DIGEST,
        )
    aggregate = _draft(
        connection,
        "RESEARCH_AGGREGATE",
        {"assembly_key": "evaluation-explicit"},
        dependencies=tuple(_dependency(drafts[kind], kind) for kind in drafts),
    )
    snapshots["RESEARCH_AGGREGATE"] = validate_configuration_version(
        connection,
        version_id=aggregate.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )
    snapshot_ids = {kind: snapshots[kind].snapshot_id for kind in P0_CONFIGURATION_KINDS}
    target_id = connection.execute(
        select(RESEARCH_TARGETS_TABLE.c.id).where(
            RESEARCH_TARGETS_TABLE.c.target_snapshot_id == target_snapshot.snapshot_id
        )
    ).scalar_one()
    _profile_id, market_version_id, _payload_digest = create_market_profile_draft(
        connection,
        profile_key="evaluation-market",
        scope_key="isolated-evaluation",
        payload={"source": "isolated", "network_access": "NONE"},
    )
    validate_market_profile(connection, version_id=market_version_id)
    evidence = accept_market_artifact(
        connection,
        locator="fixtures/evaluation.parquet",
        content=b"isolated evaluation market artifact",
        media_type="application/x-parquet",
        inspector_identity="isolated-inspector-v1",
        facts=MarketInspectionFacts(
            row_count=216,
            first_open_at=NOW - timedelta(hours=12),
            last_close_at=NOW + timedelta(hours=6),
            gap_count=0,
            duplicate_count=0,
            null_count=0,
            monotonic=True,
        ),
    )
    market_snapshot = seal_market_snapshot(
        connection,
        market_profile_version_id=market_version_id,
        members=(
            (
                evidence.artifact_id,
                evidence.receipt_id,
                target_id,
                NOW - timedelta(hours=12),
                NOW + timedelta(hours=6),
            ),
        ),
    )
    preview = preview_research_bundle(
        connection,
        scope_key="isolated-evaluation",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot.snapshot_id,
    )
    assert preview.status == "READY"
    activated = activate_research_bundle(
        connection,
        scope_key="isolated-evaluation",
        workflow_key="RESEARCH",
        snapshot_ids=snapshot_ids,
        market_snapshot_id=market_snapshot.snapshot_id,
        actor_identity="isolated-control-writer",
        expected_bundle_digest=preview.bundle_digest,
        expected_bundle_id=preview.prospective_bundle_id,
    )
    return {
        "strategy_version_id": _submission(connection),
        "target_id": target_id,
        "bundle_id": activated.configuration_bundle_id,
        "bundle_digest": activated.bundle_digest,
        "market_snapshot_id": market_snapshot.snapshot_id,
        "market_snapshot_digest": preview.market_snapshot_digest,
        "window_snapshot_id": window_snapshot.snapshot_id,
    }


def _validated_attempt(connection, *, metrics_by_window, aggregation="MEAN"):
    lineage = _bundle(connection, aggregation=aggregation)
    window_members = {
        row["member_key"].removeprefix("window:"): dict(row)
        for row in connection.execute(
            select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == lineage["window_snapshot_id"],
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key.like("window:%"),
            )
        ).mappings()
    }
    window_definitions = {
        "required-recent": (True, NOW, NOW + timedelta(hours=6)),
        "optional-context": (False, NOW - timedelta(hours=6), NOW),
        "required-stress": (
            True,
            NOW - timedelta(hours=12),
            NOW - timedelta(hours=6),
        ),
    }
    plan_id = uuid4()
    plan_digest = _digest(
        {
            "strategy_version_id": str(lineage["strategy_version_id"]),
            "target_id": str(lineage["target_id"]),
            "bundle_digest": lineage["bundle_digest"],
            "market_snapshot_digest": lineage["market_snapshot_digest"],
            "window_snapshot_id": str(lineage["window_snapshot_id"]),
        }
    )
    connection.execute(
        VALIDATION_PLANS_TABLE.insert().values(
            id=plan_id,
            strategy_version_id=lineage["strategy_version_id"],
            research_target_id=lineage["target_id"],
            configuration_bundle_id=lineage["bundle_id"],
            configuration_bundle_digest=lineage["bundle_digest"],
            market_snapshot_id=lineage["market_snapshot_id"],
            market_snapshot_digest=lineage["market_snapshot_digest"],
            window_snapshot_id=lineage["window_snapshot_id"],
            validation_plan_digest=plan_digest,
            status="COMPLETE",
            created_at=NOW,
        )
    )
    plan_windows = {}
    for key, (required, start, end) in window_definitions.items():
        plan_window_id = uuid4()
        member = window_members[key]
        connection.execute(
            VALIDATION_PLAN_WINDOWS_TABLE.insert().values(
                id=plan_window_id,
                validation_plan_id=plan_id,
                window_snapshot_member_id=member["id"],
                window_key=key,
                window_member_digest=member["member_digest"],
                required=required,
                window_start=start,
                window_end=end,
            )
        )
        plan_windows[key] = plan_window_id
    attempt_id = uuid4()
    connection.execute(
        VALIDATION_ATTEMPTS_TABLE.insert().values(
            id=attempt_id,
            validation_plan_id=plan_id,
            attempt_number=1,
            status="SUCCEEDED",
            executor_identity="isolated-ephemeral-executor-v1",
            executor_image_digest=EXECUTOR_DIGEST,
            request_digest=ATTEMPT_REQUEST_DIGEST,
            receipt_digest=ATTEMPT_RECEIPT_DIGEST,
            created_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
        )
    )
    for key, metrics in metrics_by_window.items():
        connection.execute(
            VALIDATION_WINDOW_RESULTS_TABLE.insert().values(
                id=uuid4(),
                validation_attempt_id=attempt_id,
                validation_plan_window_id=plan_windows[key],
                metrics_json=metrics,
                metrics_digest=_digest(metrics),
                receipt_digest=_digest({"window": key, "metrics": metrics}),
                created_at=NOW + timedelta(minutes=1),
            )
        )
    return plan_id, attempt_id


def _passing_metrics():
    return {
        "required-recent": {
            "net_return": 0.8,
            "drawdown": 0.1,
            "trade_count": 20,
        },
        "required-stress": {
            "net_return": 0.4,
            "drawdown": 0.2,
            "trade_count": 12,
        },
    }


def test_score_target_dynamic_windows_isolated_and_idempotent(canonical_connection):
    with canonical_connection.begin():
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics(), aggregation="MEAN"
        )
        first = score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        repeated = score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
    assert first.overall_score == Decimal("81.25000000")
    assert first.required_window_count == 2
    assert first.repeat_noop is False
    assert repeated.target_score_id == first.target_score_id
    assert repeated.repeat_noop is True
    assert _count(canonical_connection, TARGET_SCORES_TABLE) == 1
    assert _count(canonical_connection, QUALIFICATION_DECISIONS_TABLE) == 0
    assert _count(canonical_connection, QUALIFICATION_WINDOW_EVIDENCE_TABLE) == 0
    assert _count(canonical_connection, OPTIMIZATION_RUNS_TABLE) == 0


def test_missing_required_result_or_scoring_metric_is_fail_closed(canonical_connection):
    with canonical_connection.begin():
        metrics = _passing_metrics()
        metrics.pop("required-stress")
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=metrics
        )
        with pytest.raises(CanonicalEvaluationBlocked) as missing_window:
            score_target(
                canonical_connection,
                validation_plan_id=plan_id,
                validation_attempt_id=attempt_id,
                scorer_identity="isolated-scorer-v1",
            )
    assert missing_window.value.code == "BLOCKED_REQUIRED_WINDOW_RESULT_MISSING"
    assert _count(canonical_connection, TARGET_SCORES_TABLE) == 0


def test_qualifier_hard_gate_precedes_high_score_and_inserts_terminal_only(
    canonical_connection,
):
    with canonical_connection.begin():
        metrics = _passing_metrics()
        metrics["required-stress"]["trade_count"] = 9
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=metrics, aggregation="MAXIMUM"
        )
        score = score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        assert score.overall_score == Decimal("90.00000000")
        decision = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
        optimization = gate_optimization(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
        )
        repeated = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
    assert decision.status == "REJECTED"
    assert decision.reason_code == "REQUIRED_WINDOW_GATE_FAILED"
    assert optimization.status == "BLOCKED"
    assert optimization.projection_status == "PENDING_FIRST_BACKTEST"
    assert optimization.reason_code == "QUALIFIED_BASELINE_REQUIRED"
    assert decision.evidence_count == 2
    assert repeated.qualification_decision_id == decision.qualification_decision_id
    assert repeated.repeat_noop is True
    statuses = set(
        canonical_connection.execute(select(QUALIFICATION_DECISIONS_TABLE.c.status)).scalars()
    )
    assert statuses == {"REJECTED"}
    assert "PENDING" not in statuses
    assert _count(canonical_connection, QUALIFICATION_WINDOW_EVIDENCE_TABLE) == 2
    assert _count(canonical_connection, OPTIMIZATION_RUNS_TABLE) == 0


def test_qualified_baseline_gate_is_read_only(canonical_connection):
    with canonical_connection.begin():
        blocked_unset = gate_optimization(
            canonical_connection, baseline_qualification_decision_id=None
        )
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics()
        )
        score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="isolated-scorer-v1",
        )
        decision = qualify_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            qualifier_identity="isolated-qualifier-v1",
        )
        ready = gate_optimization(
            canonical_connection,
            baseline_qualification_decision_id=decision.qualification_decision_id,
        )
    assert blocked_unset.status == "BLOCKED"
    assert blocked_unset.projection_status == "PENDING_FIRST_BACKTEST"
    assert decision.status == "QUALIFIED"
    assert ready.status == "READY"
    assert ready.projection_status == "BASELINE_ACCEPTED"
    assert _count(canonical_connection, OPTIMIZATION_RUNS_TABLE) == 0
    assert _count(canonical_connection, OPTIMIZATION_TRIALS_TABLE) == 0


def test_scorer_and_qualifier_capabilities_cannot_share_identity(canonical_connection):
    with canonical_connection.begin():
        plan_id, attempt_id = _validated_attempt(
            canonical_connection, metrics_by_window=_passing_metrics()
        )
        score_target(
            canonical_connection,
            validation_plan_id=plan_id,
            validation_attempt_id=attempt_id,
            scorer_identity="same-capability",
        )
        with pytest.raises(CanonicalEvaluationBlocked) as overlap:
            qualify_target(
                canonical_connection,
                validation_plan_id=plan_id,
                validation_attempt_id=attempt_id,
                qualifier_identity="same-capability",
            )
    assert overlap.value.code == "BLOCKED_EVALUATION_CAPABILITY_OVERLAP"
    assert _count(canonical_connection, QUALIFICATION_DECISIONS_TABLE) == 0
    assert _count(canonical_connection, QUALIFICATION_WINDOW_EVIDENCE_TABLE) == 0
