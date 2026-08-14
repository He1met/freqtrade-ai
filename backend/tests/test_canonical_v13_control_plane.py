from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.control_plane import (
    CanonicalControlPlaneBlocked,
    ConfigurationDependencyInput,
    assess_research_configuration_readiness,
    canonical_digest,
    create_configuration_draft,
    validate_configuration_version,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    P0_CONFIGURATION_KINDS,
)
from app.canonical_v13.models import (
    CONFIGURATION_DEPENDENCIES_TABLE,
    CONFIGURATION_PROFILES_TABLE,
    CONFIGURATION_SNAPSHOT_MEMBERS_TABLE,
    CONFIGURATION_SNAPSHOTS_TABLE,
    CONFIGURATION_VERSIONS_TABLE,
    RESEARCH_TARGET_ALLOCATIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
)


ADAPTER_DIGEST = "a" * 64
MANIFEST_DIGEST = "b" * 64
SCHEMA = {"type": "object", "additionalProperties": False}


@pytest.fixture
def canonical_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    raw = engine.connect()
    with raw.begin():
        install_canonical_genesis(raw, installer_identity="phase3-control-plane-test")
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


def _draft(
    connection,
    kind: str,
    payload: dict,
    *,
    suffix: str | None = None,
    dependencies: tuple[ConfigurationDependencyInput, ...] = (),
):
    key = (suffix or kind.lower()).replace("_", "-")
    return create_configuration_draft(
        connection,
        profile_key=f"phase3-{key}",
        configuration_kind=kind,
        scope_key="production-research",
        workflow_key="RESEARCH",
        schema_json=SCHEMA,
        payload_json=payload,
        adapter_identity=f"{kind.lower()}-adapter-v1",
        adapter_digest=ADAPTER_DIGEST,
        dependencies=dependencies,
    )


def _validate(connection, draft):
    return validate_configuration_version(
        connection,
        version_id=draft.version_id,
        adapter_manifest_digest=MANIFEST_DIGEST,
    )


def _target_payload() -> dict:
    return {
        "targets": [
            {
                "target_key": "btc-5m",
                "instrument": "BTC-USDT-SWAP",
                "pair": "BTC/USDT:USDT",
                "timeframe": "5m",
                "data_kind": "futures",
            },
            {
                "target_key": "eth-15m",
                "instrument": "ETH-USDT-SWAP",
                "pair": "ETH/USDT:USDT",
                "timeframe": "15m",
                "data_kind": "futures",
            },
        ]
    }


def _window_payload() -> dict:
    return {
        "windows": [
            {
                "window_key": "required-oos",
                "required": True,
                "start_at": "2025-01-01T00:00:00Z",
                "end_at": "2025-02-01T00:00:00Z",
                "coverage": {"minimum_closed_candles": 100},
            },
            {
                "window_key": "optional-regime",
                "required": False,
                "start_at": "2025-02-01T00:00:00Z",
                "end_at": "2025-03-01T00:00:00Z",
                "coverage": {"minimum_closed_candles": 80},
            },
        ]
    }


def _diversity_payload() -> dict:
    return {
        "rules": [
            {
                "rule_key": "strategy-family",
                "algorithm": "explicit-correlation-v1",
                "metric": "return_correlation",
                "operator": "<=",
                "threshold": 0.8,
            }
        ]
    }


def _quality_payload() -> dict:
    return {
        "minimum_score": 50,
        "required_window_gates": [
            {
                "gate_key": "minimum-trades",
                "metric": "trade_count",
                "operator": ">=",
                "threshold": 1,
            }
        ],
    }


def _scoring_payload() -> dict:
    return {
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
        ]
    }


def _dependency(draft, kind: str) -> ConfigurationDependencyInput:
    return ConfigurationDependencyInput(
        version_id=draft.version_id,
        expected_kind=kind,
        relation_key=f"snapshot:{kind.lower()}",
    )


def test_canonical_digests_ignore_mapping_order_and_drafts_have_no_defaults(
    canonical_connection,
) -> None:
    assert canonical_digest({"a": 1, "b": [2, 3]}) == canonical_digest(
        {"b": [2, 3], "a": 1}
    )
    with canonical_connection.begin():
        draft = _draft(canonical_connection, "DIVERSITY", {"rules": []})
    row = canonical_connection.execute(
        select(CONFIGURATION_VERSIONS_TABLE).where(
            CONFIGURATION_VERSIONS_TABLE.c.id == draft.version_id
        )
    ).mappings().one()
    assert row.lifecycle_status == "DRAFT"
    assert row.schema_digest == canonical_digest(SCHEMA)
    assert row.payload_digest == canonical_digest({"rules": []})
    assert "target_count" not in row.payload_json
    assert "candidate_count" not in row.payload_json


@pytest.mark.parametrize(
    "key",
    ["target_count", "candidate_count", "total_target_count", "total_candidate_count"],
)
def test_editable_or_persisted_totals_are_rejected(canonical_connection, key: str) -> None:
    with pytest.raises(CanonicalControlPlaneBlocked) as raised:
        with canonical_connection.begin():
            _draft(canonical_connection, "GENERATION", {"allocations": [], key: 60})
    assert raised.value.code == "BLOCKED_DERIVED_TOTAL_PERSISTENCE"
    assert _count(canonical_connection, CONFIGURATION_PROFILES_TABLE) == 0


def test_target_and_window_freeze_exact_normalized_members(canonical_connection) -> None:
    with canonical_connection.begin():
        target = _draft(canonical_connection, "TARGET", _target_payload())
        target_snapshot = _validate(canonical_connection, target)
        window = _draft(canonical_connection, "WINDOW", _window_payload())
        window_snapshot = _validate(canonical_connection, window)

    assert target_snapshot.target_count == 2
    assert target_snapshot.total_candidate_count == 0
    target_members = set(
        canonical_connection.execute(
            select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == target_snapshot.snapshot_id
            )
        ).scalars()
    )
    assert target_members == {"profile:target", "target:btc-5m", "target:eth-15m"}
    assert _count(canonical_connection, RESEARCH_TARGETS_TABLE) == 2

    window_members = dict(
        canonical_connection.execute(
            select(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key,
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_identity,
            ).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == window_snapshot.snapshot_id
            )
        ).all()
    )
    assert window_members["window:required-oos"].endswith("required=true")
    assert window_members["window:optional-regime"].endswith("required=false")
    snapshot_json = canonical_connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE.c.snapshot_json).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.id == window_snapshot.snapshot_id
        )
    ).scalar_one()
    required = [
        item["window_key"]
        for item in snapshot_json["payload_json"]["windows"]
        if item["required"]
    ]
    assert required == ["required-oos"]


def test_generation_persists_explicit_per_target_allocation_and_cap_only(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        target = _draft(canonical_connection, "TARGET", _target_payload())
        target_snapshot = _validate(canonical_connection, target)
        generation = _draft(
            canonical_connection,
            "GENERATION",
            {
                "allocations": [
                    {"target_key": "btc-5m", "allocation_count": 2, "candidate_cap": 4},
                    {"target_key": "eth-15m", "allocation_count": 3, "candidate_cap": 5},
                ],
                "provider": "explicit-provider-v1",
            },
            dependencies=(_dependency(target, "TARGET"),),
        )
        generation_snapshot = _validate(canonical_connection, generation)

    assert target_snapshot.target_count == 2
    assert generation_snapshot.target_count == 0
    assert generation_snapshot.total_candidate_count == 5
    allocations = canonical_connection.execute(
        select(
            RESEARCH_TARGET_ALLOCATIONS_TABLE.c.allocation_count,
            RESEARCH_TARGET_ALLOCATIONS_TABLE.c.candidate_cap,
        ).order_by(RESEARCH_TARGET_ALLOCATIONS_TABLE.c.allocation_count)
    ).all()
    assert allocations == [(2, 4), (3, 5)]
    members = set(
        canonical_connection.execute(
            select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == generation_snapshot.snapshot_id
            )
        ).scalars()
    )
    assert members == {
        "profile:generation",
        "allocation:btc-5m",
        "allocation:eth-15m",
    }
    payload = canonical_connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE.c.snapshot_json).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.id == generation_snapshot.snapshot_id
        )
    ).scalar_one()["payload_json"]
    assert not {"target_count", "candidate_count", "total_candidate_count"} & set(payload)


@pytest.mark.parametrize(
    "allocations",
    [
        [{"target_key": "btc-5m", "allocation_count": 2, "candidate_cap": 4}],
        [
            {"target_key": "btc-5m", "allocation_count": 2, "candidate_cap": None},
            {"target_key": "eth-15m", "allocation_count": 3, "candidate_cap": 5},
        ],
    ],
)
def test_missing_target_allocation_or_cap_is_blocked_without_snapshot(
    canonical_connection, allocations
) -> None:
    with canonical_connection.begin():
        target = _draft(canonical_connection, "TARGET", _target_payload())
        _validate(canonical_connection, target)
        generation = _draft(
            canonical_connection,
            "GENERATION",
            {"allocations": allocations},
            dependencies=(_dependency(target, "TARGET"),),
        )
    with pytest.raises(CanonicalControlPlaneBlocked) as raised:
        with canonical_connection.begin():
            _validate(canonical_connection, generation)
    assert raised.value.code in {
        "BLOCKED_TARGET_ALLOCATION_MISMATCH",
        "BLOCKED_ALLOCATION_OR_CAP_UNSET",
    }
    assert canonical_connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE.c.id).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.configuration_version_id
            == generation.version_id
        )
    ).scalar_one_or_none() is None


def _freeze_first_six(canonical_connection):
    target = _draft(canonical_connection, "TARGET", _target_payload())
    target_snapshot = _validate(canonical_connection, target)
    window = _draft(canonical_connection, "WINDOW", _window_payload())
    window_snapshot = _validate(canonical_connection, window)
    generation = _draft(
        canonical_connection,
        "GENERATION",
        {
            "allocations": [
                {"target_key": "btc-5m", "allocation_count": 2, "candidate_cap": 4},
                {"target_key": "eth-15m", "allocation_count": 3, "candidate_cap": 5},
            ]
        },
        dependencies=(_dependency(target, "TARGET"),),
    )
    generation_snapshot = _validate(canonical_connection, generation)
    drafts = {"TARGET": target, "WINDOW": window, "GENERATION": generation}
    snapshots = {
        "TARGET": target_snapshot,
        "WINDOW": window_snapshot,
        "GENERATION": generation_snapshot,
    }
    for kind in ("DIVERSITY", "QUALITY_QUALIFICATION", "SCORING"):
        payload = {
            "DIVERSITY": _diversity_payload(),
            "QUALITY_QUALIFICATION": _quality_payload(),
            "SCORING": _scoring_payload(),
        }[kind]
        draft = _draft(
            canonical_connection,
            kind,
            payload,
        )
        drafts[kind] = draft
        snapshots[kind] = _validate(canonical_connection, draft)
    return drafts, snapshots


def test_aggregate_requires_and_freezes_exact_first_six_snapshot_dependencies(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        drafts, snapshots = _freeze_first_six(canonical_connection)
        aggregate = _draft(
            canonical_connection,
            "RESEARCH_AGGREGATE",
            {"assembly_key": "explicit-phase3-assembly"},
            dependencies=tuple(_dependency(drafts[kind], kind) for kind in drafts),
        )
        aggregate_snapshot = _validate(canonical_connection, aggregate)
    assert aggregate_snapshot.configuration_kind == "RESEARCH_AGGREGATE"
    aggregate_members = set(
        canonical_connection.execute(
            select(CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.member_key).where(
                CONFIGURATION_SNAPSHOT_MEMBERS_TABLE.c.configuration_snapshot_id
                == aggregate_snapshot.snapshot_id
            )
        ).scalars()
    )
    assert aggregate_members == {
        "profile:research_aggregate",
        *(f"snapshot:{kind.lower()}" for kind in drafts),
    }
    dependencies = canonical_connection.execute(
        select(CONFIGURATION_DEPENDENCIES_TABLE.c.depends_on_version_id).where(
            CONFIGURATION_DEPENDENCIES_TABLE.c.configuration_version_id
            == aggregate.version_id
        )
    ).scalars().all()
    assert set(dependencies) == {draft.version_id for draft in drafts.values()}

    all_snapshots = {
        kind: snapshot.snapshot_id for kind, snapshot in snapshots.items()
    }
    all_snapshots["RESEARCH_AGGREGATE"] = aggregate_snapshot.snapshot_id
    readiness = assess_research_configuration_readiness(
        canonical_connection, snapshot_ids=all_snapshots
    )
    assert readiness.status == "READY"
    assert readiness.reason_codes == ()
    assert readiness.target_count == 2
    assert readiness.total_candidate_count == 5


def test_missing_p0_values_return_blocked_instead_of_defaults(canonical_connection) -> None:
    readiness = assess_research_configuration_readiness(
        canonical_connection, snapshot_ids={}
    )
    assert readiness.status == "BLOCKED"
    assert set(readiness.reason_codes) == {
        f"{kind}_SNAPSHOT_UNSET" for kind in P0_CONFIGURATION_KINDS
    }
    assert readiness.target_count == 0
    assert readiness.total_candidate_count == 0


def test_aggregate_missing_one_kind_is_blocked(canonical_connection) -> None:
    with canonical_connection.begin():
        drafts, _snapshots = _freeze_first_six(canonical_connection)
        dependencies = tuple(
            _dependency(draft, kind)
            for kind, draft in drafts.items()
            if kind != "SCORING"
        )
        aggregate = _draft(
            canonical_connection,
            "RESEARCH_AGGREGATE",
            {"assembly_key": "incomplete"},
            dependencies=dependencies,
        )
    with pytest.raises(CanonicalControlPlaneBlocked) as raised:
        with canonical_connection.begin():
            _validate(canonical_connection, aggregate)
    assert raised.value.code == "BLOCKED_AGGREGATE_DEPENDENCIES"


def test_dependency_type_mismatch_and_cycle_fail_closed(canonical_connection) -> None:
    with canonical_connection.begin():
        target = _draft(canonical_connection, "TARGET", _target_payload())
    with pytest.raises(CanonicalControlPlaneBlocked) as mismatch:
        with canonical_connection.begin():
            _draft(
                canonical_connection,
                "GENERATION",
                {"allocations": []},
                dependencies=(
                    ConfigurationDependencyInput(
                        version_id=target.version_id,
                        expected_kind="WINDOW",
                        relation_key="snapshot:target",
                    ),
                ),
            )
    assert mismatch.value.code == "BLOCKED_DEPENDENCY_TYPE_MISMATCH"

    with canonical_connection.begin():
        diversity = _draft(
            canonical_connection,
            "DIVERSITY",
            {"members": [{"key": "explicit"}]},
            dependencies=(_dependency(target, "TARGET"),),
        )
        canonical_connection.execute(
            CONFIGURATION_DEPENDENCIES_TABLE.insert().values(
                id=uuid4(),
                configuration_version_id=target.version_id,
                depends_on_version_id=diversity.version_id,
                relation_key="injected-cycle-for-contract-test",
            )
        )
    with pytest.raises(CanonicalControlPlaneBlocked) as cycle:
        with canonical_connection.begin():
            _validate(canonical_connection, target)
    assert cycle.value.code == "BLOCKED_DEPENDENCY_CYCLE"


def test_snapshot_validation_is_idempotent_and_does_not_mutate_frozen_rows(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        draft = _draft(canonical_connection, "DIVERSITY", _diversity_payload())
        first = _validate(canonical_connection, draft)
    before = canonical_connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.id == first.snapshot_id
        )
    ).mappings().one()
    canonical_connection.rollback()
    with canonical_connection.begin():
        repeated = _validate(canonical_connection, draft)
    after = canonical_connection.execute(
        select(CONFIGURATION_SNAPSHOTS_TABLE).where(
            CONFIGURATION_SNAPSHOTS_TABLE.c.id == first.snapshot_id
        )
    ).mappings().one()
    assert repeated.repeat_noop is True
    assert repeated.snapshot_id == first.snapshot_id
    assert dict(after) == dict(before)
    assert repeated.member_count == 2


def test_revalidation_rejects_snapshot_or_adapter_manifest_drift(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        draft = _draft(canonical_connection, "DIVERSITY", _diversity_payload())
        snapshot = _validate(canonical_connection, draft)
    with pytest.raises(CanonicalControlPlaneBlocked) as adapter_drift:
        with canonical_connection.begin():
            validate_configuration_version(
                canonical_connection,
                version_id=draft.version_id,
                adapter_manifest_digest="c" * 64,
            )
    assert adapter_drift.value.code == "BLOCKED_ADAPTER_MANIFEST_DRIFT"

    with canonical_connection.begin():
        canonical_connection.execute(
            CONFIGURATION_SNAPSHOTS_TABLE.update()
            .where(CONFIGURATION_SNAPSHOTS_TABLE.c.id == snapshot.snapshot_id)
            .values(snapshot_json={"tampered": True})
        )
    with pytest.raises(CanonicalControlPlaneBlocked) as snapshot_drift:
        with canonical_connection.begin():
            _validate(canonical_connection, draft)
    assert snapshot_drift.value.code == "BLOCKED_SNAPSHOT_DIGEST_DRIFT"


@pytest.mark.parametrize(
    ("kind", "payload", "code"),
    [
        ("DIVERSITY", {"rules": []}, "BLOCKED_CONFIGURATION_VALUE_UNSET"),
        (
            "QUALITY_QUALIFICATION",
            {"required_window_gates": []},
            "BLOCKED_INVALID_CONFIGURATION_PAYLOAD",
        ),
        (
            "SCORING",
            {
                "window_aggregation": "MINIMUM",
                "components": [
                    {
                        "component_key": "bad-weight",
                        "metric": "profit_factor",
                        "weight": 0.5,
                        "direction": "maximize",
                        "minimum": 0.0,
                        "maximum": 3.0,
                    }
                ]
            },
            "BLOCKED_SCORING_WEIGHT_TOTAL",
        ),
    ],
)
def test_rule_configuration_missing_values_cannot_freeze_or_fake_readiness(
    canonical_connection, kind: str, payload: dict, code: str
) -> None:
    with canonical_connection.begin():
        draft = _draft(canonical_connection, kind, payload, suffix=f"invalid-{kind}")
    with pytest.raises(CanonicalControlPlaneBlocked) as raised:
        with canonical_connection.begin():
            _validate(canonical_connection, draft)
    assert raised.value.code == code
    assert _count(canonical_connection, CONFIGURATION_SNAPSHOTS_TABLE) == 0
