#!/usr/bin/env python3
"""Plan or execute the explicit V1.3 owner research activation transaction."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.owner_research_activation import (
    ACTIVATION_SCOPE_KEY,
    ACTIVATION_SCOPE_TYPE,
    ExistingResearchBindings,
    build_owner_research_activation_plan,
    execute_owner_research_activation,
)
from app.services.owner_research_activation_postgresql import (
    PostgreSQLOwnerResearchActivationPort,
)
from app.services.owner_research_activation_prerequisites import (
    discover_existing_research_source,
    discover_profile_bound_metric_versions,
    ensure_required_v2_metric_versions,
    evolve_activation_configuration_schemas,
    reconcile_installed_adapter_registry,
    record_owner_activation_registry_audit,
    PROFILE_BOUND_SCORE_METRIC_KEYS,
    assert_owner_activation_fence,
)


_DATABASE_NAME = "freqtrade_ai_design_lab"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SCOPE_TYPE = "WORKFLOW"
_SOURCE_SCOPE_KEY = "production-research"
_ACTOR = "owner:issue-709-v13-activation"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Build the profile-bound V1.3 owner bundle in one transaction. "
            "The default is rollback-only dry-run."
        )
    )
    result.add_argument("--candidates-per-target", type=int, required=True)
    result.add_argument("--apply", action="store_true")
    result.add_argument("--expected-input-digest")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.candidates_per_target <= 0:
        parser().error("--candidates-per-target must be a positive integer")
    if args.apply and (
        not isinstance(args.expected_input_digest, str)
        or not _SHA256.fullmatch(args.expected_input_digest)
    ):
        parser().error("--apply requires --expected-input-digest from a dry-run")
    if not args.apply and args.expected_input_digest is not None:
        parser().error("--expected-input-digest is only valid with --apply")

    engine = create_engine(
        f"postgresql+psycopg:///{_DATABASE_NAME}",
        pool_pre_ping=True,
        connect_args={"options": "-c search_path=public"},
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, autoflush=False) as db:
                    assert_owner_activation_fence(db)
                    source = discover_existing_research_source(
                        db,
                        scope_type=_SOURCE_SCOPE_TYPE,
                        scope_key=_SOURCE_SCOPE_KEY,
                    )
                    semantic_bindings = _bindings(
                        source,
                        metric_ids={
                            key: ordinal
                            for ordinal, key in enumerate(
                                PROFILE_BOUND_SCORE_METRIC_KEYS,
                                start=1,
                            )
                        },
                    )
                    semantic_plan = build_owner_research_activation_plan(
                        semantic_bindings,
                        candidates_per_target=args.candidates_per_target,
                        target_count=source.target_count,
                        candidate_count=(
                            args.candidates_per_target * source.target_count
                        ),
                    )
                    input_digest = semantic_plan.plan_digest
                    if args.apply and args.expected_input_digest != input_digest:
                        raise RuntimeError(
                            "ACTIVATION_INPUT_DIGEST_MISMATCH: dry-run and apply differ"
                        )
                    reconcile_installed_adapter_registry(db)
                    ensure_required_v2_metric_versions(
                        db,
                        actor=_ACTOR,
                        scope_type=ACTIVATION_SCOPE_TYPE,
                        scope_key=ACTIVATION_SCOPE_KEY,
                        request_prefix="issue-709:v2-metrics",
                    )
                    metric_ids = discover_profile_bound_metric_versions(db)
                    bindings = _bindings(source, metric_ids=metric_ids)
                    plan = build_owner_research_activation_plan(
                        bindings,
                        candidates_per_target=args.candidates_per_target,
                        target_count=source.target_count,
                        candidate_count=(
                            args.candidates_per_target * source.target_count
                        ),
                    )
                    evolve_activation_configuration_schemas(
                        db,
                        plan,
                        actor=_ACTOR,
                    )
                    activation = execute_owner_research_activation(
                        plan,
                        PostgreSQLOwnerResearchActivationPort(db, actor=_ACTOR),
                    )
                    record_owner_activation_registry_audit(
                        db,
                        plan,
                        activation,
                        actor=_ACTOR,
                    )
                    db.flush()
                    report = {
                        "schema_version": "strategy-platform-v13-owner-activation-report-v1",
                        "database": _DATABASE_NAME,
                        "mode": "APPLY" if args.apply else "DRY_RUN_ROLLBACK",
                        "committed": bool(args.apply),
                        "scope_type": plan.scope_type,
                        "scope_key": plan.scope_key,
                        "source_research_profile_version_id": (
                            source.research_profile_version_id
                        ),
                        "target_count": source.target_count,
                        "candidates_per_target": args.candidates_per_target,
                        "candidate_count": (
                            source.target_count * args.candidates_per_target
                        ),
                        "plan_digest": plan.plan_digest,
                        "input_digest": input_digest,
                        "installed_adapter_manifest_digest": (
                            plan.installed_adapter_manifest_digest
                        ),
                        "version_ids": dict(activation.version_ids),
                        "bundle_id": activation.bundle_id,
                        "bundle_digest": activation.bundle_digest,
                        "repeat_noop": activation.repeat_noop,
                        "historical_revalidation_started": False,
                        "backtest_or_worker_started": False,
                        "signal_or_order_created": False,
                        "credentials_accessed": False,
                    }
                if args.apply:
                    transaction.commit()
                else:
                    transaction.rollback()
                print(json.dumps(report, allow_nan=False, sort_keys=True))
                return 0
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
    finally:
        engine.dispose()


def _bindings(source, *, metric_ids: dict[str, int]) -> ExistingResearchBindings:
    return ExistingResearchBindings(
        provider_model_config_version_id=source.provider_model_config_version_id,
        research_target_config_set_id=source.research_target_config_set_id,
        validation_window_config_set_id=source.validation_window_config_set_id,
        market_data_policy_version_id=source.market_data_policy_version_id,
        evidence_freshness_profile_version_id=(
            source.evidence_freshness_profile_version_id
        ),
        scheduler_profile_version_id=source.scheduler_profile_version_id,
        worker_execution_profile_version_id=(
            source.worker_execution_profile_version_id
        ),
        strategy_family_version_ids=source.strategy_family_version_ids,
        metric_version_ids=metric_ids,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        context = exc.context if isinstance(exc, StrategyPlatformReadError) else {}
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                    "context": context,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
