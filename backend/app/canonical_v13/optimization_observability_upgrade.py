"""Add canonical terminal observability to optimization runs.

The historical backfill is intentionally derived only from immutable run/trial
rows already stored in the canonical database. Local optimization artifacts are
not accepted as evidence by this upgrade.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.genesis import postgresql_acl_statements, verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
)
from app.canonical_v13.models import (
    OPTIMIZATION_RUNS_TABLE,
    OPTIMIZATION_TRIALS_TABLE,
    SCHEMA_METADATA_TABLE,
)
from app.canonical_v13.optimization import (
    CanonicalOptimizationBlocked,
    derive_optimization_terminal_reason_codes,
    optimization_selection_digest,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_OPTIMIZATION_OBSERVABILITY_MANIFEST_DIGEST: Final = (
    "1f1c79ad369f7e1c78d225b7d4de0acf8089b22a908be73c338dd72765855175"
)
OPTIMIZATION_OBSERVABILITY_UPGRADE_CONTRACT: Final = (
    "canonical-v13-optimization-observability-upgrade-v1"
)
OPTIMIZATION_OBSERVABILITY_COLUMNS: Final[tuple[str, ...]] = (
    "result_count",
    "result_digest",
    "submitted_strategy_count",
    "terminal_reason_codes",
    "trial_count",
)
OPTIMIZATION_OBSERVABILITY_CONSTRAINTS: Final[tuple[str, ...]] = (
    "ck_optimization_runs_optimization_runs_terminal_counts_valid",
    "ck_optimization_runs_optimization_runs_terminal_observa_7237",
    "ck_optimization_runs_optimization_runs_terminal_reasons_valid",
    "ck_optimization_runs_result_digest_digest_length",
)
OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION: Final = (
    "guard_optimization_runs_terminal_observability"
)
OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER: Final = (
    "optimization_runs_terminal_observability_guard"
)
_TERMINAL_STATUSES: Final = ("SUCCEEDED", "FAILED", "BLOCKED")


class CanonicalOptimizationObservabilityUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class OptimizationObservabilityUpgradeResult:
    contract: str
    status: str
    manifest_digest: str
    columns_present: tuple[str, ...]
    constraints_present: tuple[str, ...]
    trigger_present: bool
    terminal_run_count: int
    backfilled_run_count: int
    trial_count: int
    result_count: int
    submitted_strategy_count: int
    repeat_noop: bool
    receipt_digest: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def optimization_observability_trigger_statements() -> tuple[str, ...]:
    schema = CANONICAL_BUSINESS_SCHEMA
    return (
        f"""CREATE OR REPLACE FUNCTION {schema}.{OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          persisted_submission_count integer;
          persisted_trial_count integer;
          persisted_result_count integer;
          matching_selection_digest_count integer;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.status IN ('SUCCEEDED','FAILED','BLOCKED') THEN
              RAISE EXCEPTION 'canonical optimization terminal rows require lifecycle completion';
            END IF;
            RETURN NEW;
          END IF;
          IF OLD.id IS DISTINCT FROM NEW.id
             OR OLD.baseline_qualification_decision_id IS DISTINCT FROM NEW.baseline_qualification_decision_id
             OR OLD.actor_identity IS DISTINCT FROM NEW.actor_identity
             OR OLD.objective_json IS DISTINCT FROM NEW.objective_json
             OR OLD.request_digest IS DISTINCT FROM NEW.request_digest
             OR OLD.receipt_digest IS DISTINCT FROM NEW.receipt_digest
             OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'canonical optimization run lineage is immutable';
          END IF;
          IF OLD.status IN ('SUCCEEDED','FAILED','BLOCKED') THEN
            IF NEW.status IS DISTINCT FROM OLD.status
               OR NEW.terminal_reason_codes IS DISTINCT FROM OLD.terminal_reason_codes
               OR NEW.trial_count IS DISTINCT FROM OLD.trial_count
               OR NEW.result_count IS DISTINCT FROM OLD.result_count
               OR NEW.result_digest IS DISTINCT FROM OLD.result_digest
               OR NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
              RAISE EXCEPTION 'canonical optimization terminal evidence is immutable';
            END IF;
            IF NEW.submitted_strategy_count IS DISTINCT FROM OLD.submitted_strategy_count THEN
              SELECT count(*) INTO persisted_submission_count
              FROM {schema}.optimization_trials
              WHERE optimization_run_id = OLD.id
                AND submitted_strategy_version_id IS NOT NULL;
              IF NEW.submitted_strategy_count <> OLD.submitted_strategy_count + 1
                 OR NEW.submitted_strategy_count <> persisted_submission_count THEN
                RAISE EXCEPTION 'canonical optimization submission count drift';
              END IF;
            END IF;
          END IF;
          IF NEW.status IN ('SUCCEEDED','FAILED','BLOCKED') THEN
            IF NEW.terminal_reason_codes IS NULL
               OR NEW.trial_count IS NULL
               OR NEW.result_count IS NULL
               OR NEW.submitted_strategy_count IS NULL
               OR NEW.result_digest IS NULL
               OR NEW.completed_at IS NULL
               OR (NEW.status IN ('FAILED','BLOCKED') AND jsonb_array_length(NEW.terminal_reason_codes::jsonb) = 0)
               OR (NEW.status = 'SUCCEEDED' AND jsonb_array_length(NEW.terminal_reason_codes::jsonb) <> 0) THEN
              RAISE EXCEPTION 'canonical optimization terminal observability is incomplete';
            END IF;
            IF OLD.status NOT IN ('SUCCEEDED','FAILED','BLOCKED') THEN
              SELECT count(*), count(result_digest),
                     count(*) FILTER (WHERE submitted_strategy_version_id IS NOT NULL),
                     count(*) FILTER (WHERE metrics_json->>'selection_digest' = NEW.result_digest)
                INTO persisted_trial_count, persisted_result_count,
                     persisted_submission_count, matching_selection_digest_count
              FROM {schema}.optimization_trials
              WHERE optimization_run_id = NEW.id;
              IF NEW.trial_count <> persisted_trial_count
                 OR NEW.result_count <> persisted_result_count
                 OR NEW.submitted_strategy_count <> persisted_submission_count
                 OR (persisted_trial_count > 0 AND matching_selection_digest_count <> persisted_trial_count) THEN
                RAISE EXCEPTION 'canonical optimization terminal summary does not match trials';
              END IF;
            END IF;
          ELSIF NEW.terminal_reason_codes IS NOT NULL
             OR NEW.trial_count IS NOT NULL
             OR NEW.result_count IS NOT NULL
             OR NEW.submitted_strategy_count IS NOT NULL
             OR NEW.result_digest IS NOT NULL
             OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'canonical optimization evidence requires terminal status';
          END IF;
          RETURN NEW;
        END $$""",
        f"DROP TRIGGER IF EXISTS {OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER} ON {schema}.optimization_runs",
        f"""CREATE TRIGGER {OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER} BEFORE INSERT OR UPDATE
        ON {schema}.optimization_runs FOR EACH ROW
        EXECUTE FUNCTION {schema}.{OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION}()""",
    )


def install_optimization_observability_trigger(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in optimization_observability_trigger_statements():
        connection.execute(text(statement))


def _manifest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise CanonicalOptimizationObservabilityUpgradeBlocked(
            "BLOCKED_OPTIMIZATION_OBSERVABILITY_SCHEMA_METADATA"
        )
    return value


def _columns(connection: Connection) -> tuple[str, ...]:
    names = {
        str(column["name"])
        for column in inspect(connection).get_columns(
            "optimization_runs", schema=CANONICAL_BUSINESS_SCHEMA
        )
    }
    return tuple(sorted(names.intersection(OPTIMIZATION_OBSERVABILITY_COLUMNS)))


def _constraints(connection: Connection) -> tuple[str, ...]:
    names = {
        str(item["name"])
        for item in inspect(connection).get_check_constraints(
            "optimization_runs", schema=CANONICAL_BUSINESS_SCHEMA
        )
        if item.get("name")
    }
    return tuple(sorted(names.intersection(OPTIMIZATION_OBSERVABILITY_CONSTRAINTS)))


def _trigger_present(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger trigger "
                "JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace "
                "WHERE namespace.nspname=:schema AND relation.relname='optimization_runs' "
                "AND trigger.tgname=:trigger AND NOT trigger.tgisinternal)"
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "trigger": OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER,
            },
        ).scalar_one()
    )


def _counts(connection: Connection) -> tuple[int, int, int, int, int]:
    if _columns(connection) != OPTIMIZATION_OBSERVABILITY_COLUMNS:
        terminal = connection.execute(
            text(
                f"SELECT count(*) FROM {CANONICAL_BUSINESS_SCHEMA}.optimization_runs "
                "WHERE status IN ('SUCCEEDED','FAILED','BLOCKED')"
            )
        ).scalar_one()
        return int(terminal), 0, 0, 0, 0
    row = connection.execute(
        text(
            f"SELECT count(*) FILTER (WHERE status IN ('SUCCEEDED','FAILED','BLOCKED')), "
            "count(*) FILTER (WHERE status IN ('SUCCEEDED','FAILED','BLOCKED') "
            "AND terminal_reason_codes IS NOT NULL AND trial_count IS NOT NULL "
            "AND result_count IS NOT NULL AND submitted_strategy_count IS NOT NULL "
            "AND result_digest IS NOT NULL), "
            "coalesce(sum(trial_count) FILTER (WHERE status IN ('SUCCEEDED','FAILED','BLOCKED')),0), "
            "coalesce(sum(result_count) FILTER (WHERE status IN ('SUCCEEDED','FAILED','BLOCKED')),0), "
            "coalesce(sum(submitted_strategy_count) FILTER (WHERE status IN ('SUCCEEDED','FAILED','BLOCKED')),0) "
            f"FROM {CANONICAL_BUSINESS_SCHEMA}.optimization_runs"
        )
    ).one()
    return tuple(int(value) for value in row)  # type: ignore[return-value]


def _result(
    connection: Connection, *, status: str, repeat_noop: bool
) -> OptimizationObservabilityUpgradeResult:
    terminal, backfilled, trials, results, submitted = _counts(connection)
    payload = {
        "contract": OPTIMIZATION_OBSERVABILITY_UPGRADE_CONTRACT,
        "status": status,
        "manifest_digest": _manifest(connection),
        "columns_present": _columns(connection),
        "constraints_present": _constraints(connection),
        "trigger_present": _trigger_present(connection) if _columns(connection) else False,
        "terminal_run_count": terminal,
        "backfilled_run_count": backfilled,
        "trial_count": trials,
        "result_count": results,
        "submitted_strategy_count": submitted,
        "repeat_noop": repeat_noop,
    }
    return OptimizationObservabilityUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


def verify_optimization_observability_upgrade(
    connection: Connection,
) -> OptimizationObservabilityUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise CanonicalOptimizationObservabilityUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED"
        )
    columns = _columns(connection)
    manifest = _manifest(connection)
    if not columns and manifest == PREVIOUS_OPTIMIZATION_OBSERVABILITY_MANIFEST_DIGEST:
        return _result(connection, status="PREVIOUS_READY", repeat_noop=True)
    if (
        columns == OPTIMIZATION_OBSERVABILITY_COLUMNS
        and _constraints(connection) == OPTIMIZATION_OBSERVABILITY_CONSTRAINTS
        and _trigger_present(connection)
        and manifest == CANONICAL_MANIFEST_DIGEST
    ):
        terminal, backfilled, *_ = _counts(connection)
        verification = verify_canonical_genesis(connection)
        if terminal == backfilled and verification.accepted:
            return _result(connection, status="ACCEPTED", repeat_noop=True)
    raise CanonicalOptimizationObservabilityUpgradeBlocked(
        "BLOCKED_PARTIAL_OPTIMIZATION_OBSERVABILITY_UPGRADE"
    )


def _canonical_backfill(connection: Connection) -> None:
    schema = CANONICAL_BUSINESS_SCHEMA
    runs = connection.execute(
        text(
            f"SELECT id, status, actor_identity, request_digest FROM {schema}.optimization_runs "
            "WHERE status IN ('SUCCEEDED','FAILED','BLOCKED') ORDER BY id"
        )
    ).mappings().all()
    for run in runs:
        trials = connection.execute(
            text(
                f"SELECT trial_number, parameters_json, metrics_json, result_digest, "
                f"submitted_strategy_version_id FROM {schema}.optimization_trials "
                "WHERE optimization_run_id=:run_id ORDER BY trial_number"
            ),
            {"run_id": run["id"]},
        ).mappings().all()
        selected = tuple(
            int(row["trial_number"])
            for row in trials
            if isinstance(row["metrics_json"], dict)
            and row["metrics_json"].get("selected_finalist") is True
        )
        try:
            reasons = derive_optimization_terminal_reason_codes(
                terminal_status=str(run["status"]), trials=trials
            )
            result_digest = optimization_selection_digest(
                optimization_run_id=run["id"],
                run_request_digest=str(run["request_digest"]),
                actor_identity=str(run["actor_identity"]),
                selected_trial_numbers=selected,
                trials=trials,
            )
        except CanonicalOptimizationBlocked as exc:
            raise CanonicalOptimizationObservabilityUpgradeBlocked(
                f"BLOCKED_UNPROVABLE_OPTIMIZATION_RUN:{run['id']}:{exc.code}"
            ) from exc
        if any(
            not isinstance(row["metrics_json"], dict)
            or row["metrics_json"].get("selection_digest") != result_digest
            for row in trials
        ):
            raise CanonicalOptimizationObservabilityUpgradeBlocked(
                f"BLOCKED_OPTIMIZATION_SELECTION_DIGEST_DRIFT:{run['id']}"
            )
        connection.execute(
            text(
                f"UPDATE {schema}.optimization_runs SET terminal_reason_codes=CAST(:reasons AS jsonb), "
                "trial_count=:trials, result_count=:results, submitted_strategy_count=:submitted, "
                "result_digest=:result_digest WHERE id=:run_id"
            ),
            {
                "reasons": json.dumps(list(reasons), separators=(",", ":")),
                "trials": len(trials),
                "results": sum(row["result_digest"] is not None for row in trials),
                "submitted": sum(
                    row["submitted_strategy_version_id"] is not None for row in trials
                ),
                "result_digest": result_digest,
                "run_id": run["id"],
            },
        )


def apply_optimization_observability_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> OptimizationObservabilityUpgradeResult:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_230_814}
    )
    before = verify_optimization_observability_upgrade(connection)
    if before.status == "ACCEPTED":
        return before
    schema = CANONICAL_BUSINESS_SCHEMA
    connection.execute(
        text(
            f"ALTER TABLE {schema}.optimization_runs "
            "ADD COLUMN terminal_reason_codes JSONB, "
            "ADD COLUMN trial_count INTEGER, "
            "ADD COLUMN result_count INTEGER, "
            "ADD COLUMN submitted_strategy_count INTEGER, "
            "ADD COLUMN result_digest VARCHAR(64)"
        )
    )
    _canonical_backfill(connection)
    connection.execute(
        text(
            f"ALTER TABLE {schema}.optimization_runs "
            "ADD CONSTRAINT ck_optimization_runs_optimization_runs_terminal_observa_7237 CHECK ("
            "(status IN ('NOT_STARTED','PENDING_BASELINE','RUNNING') AND terminal_reason_codes IS NULL "
            "AND trial_count IS NULL AND result_count IS NULL AND submitted_strategy_count IS NULL "
            "AND result_digest IS NULL AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED','FAILED','BLOCKED') AND terminal_reason_codes IS NOT NULL "
            "AND trial_count IS NOT NULL AND result_count IS NOT NULL AND submitted_strategy_count IS NOT NULL "
            "AND result_digest IS NOT NULL AND completed_at IS NOT NULL)), "
            "ADD CONSTRAINT ck_optimization_runs_optimization_runs_terminal_counts_valid CHECK ("
            "trial_count IS NULL OR (trial_count >= 0 AND result_count >= 0 AND result_count <= trial_count "
            "AND submitted_strategy_count >= 0 AND submitted_strategy_count <= trial_count)), "
            "ADD CONSTRAINT ck_optimization_runs_optimization_runs_terminal_reasons_valid CHECK ("
            "terminal_reason_codes IS NULL OR "
            "(status = 'SUCCEEDED' AND jsonb_array_length(terminal_reason_codes) = 0) OR "
            "(status IN ('FAILED','BLOCKED') AND jsonb_array_length(terminal_reason_codes) > 0)), "
            "ADD CONSTRAINT ck_optimization_runs_result_digest_digest_length CHECK (length(result_digest) = 64)"
        )
    )
    install_optimization_observability_trigger(connection)
    owner = role_mapping.physical("canonical_schema_owner")
    connection.execute(
        text(
            f"ALTER FUNCTION {schema}.{OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION}() OWNER TO {owner}"
        )
    )
    for statement in postgresql_acl_statements(
        role_mapping,
        guard_function_names=(OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION,),
    ):
        connection.execute(text(statement))
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    verified = verify_optimization_observability_upgrade(connection)
    payload = asdict(verified)
    payload.update(status="UPGRADED", repeat_noop=False)
    payload.pop("receipt_digest")
    return OptimizationObservabilityUpgradeResult(
        **payload, receipt_digest=_digest(payload)
    )


__all__ = [
    "CanonicalOptimizationObservabilityUpgradeBlocked",
    "OPTIMIZATION_OBSERVABILITY_GUARD_FUNCTION",
    "OPTIMIZATION_OBSERVABILITY_GUARD_TRIGGER",
    "OPTIMIZATION_OBSERVABILITY_UPGRADE_CONTRACT",
    "OptimizationObservabilityUpgradeResult",
    "apply_optimization_observability_upgrade",
    "install_optimization_observability_trigger",
    "optimization_observability_trigger_statements",
    "verify_optimization_observability_upgrade",
]
