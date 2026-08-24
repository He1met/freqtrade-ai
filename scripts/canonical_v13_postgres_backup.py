#!/usr/bin/env python3
"""Backup and independently restore the exact canonical V1.3 PostgreSQL data.

The archive is data-only.  A restore target must first be provisioned with the
reviewed canonical schema/owners/ACL, then this tool verifies that it is empty,
restores in one transaction, and verifies exact per-table row counts.  Only the
``strategy_platform_v13`` schema is included, so legacy secret tables and
Keychain material are outside the archive boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.bootstrap import (  # noqa: E402
    LOCAL_DATABASE_NAME,
    LOCAL_PHASE9_SERVICE_PRINCIPALS,
    LOCAL_RESEARCH_SERVICE_PRINCIPALS,
    LOCAL_RUNTIME_SERVICE_PRINCIPALS,
    LOCAL_SERVICE_PRINCIPALS,
    local_role_mapping,
    verify_postgresql_bootstrap,
)
from app.canonical_v13.gate_receipt_upgrade import (  # noqa: E402
    CanonicalGateReceiptUpgradeBlocked,
    verify_gate_receipt_upgrade,
)
from app.canonical_v13.acceptance_signal_trigger_upgrade import (  # noqa: E402
    CanonicalAcceptanceSignalTriggerUpgradeBlocked,
    verify_acceptance_signal_trigger_upgrade,
)
from app.canonical_v13.deployment_rollover_upgrade import (  # noqa: E402
    CanonicalDeploymentRolloverUpgradeBlocked,
    verify_deployment_rollover_upgrade,
)
from app.canonical_v13.manifest import (  # noqa: E402
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_TABLE_NAMES,
)
from app.canonical_v13.models import CANONICAL_TABLES  # noqa: E402
from app.canonical_v13.optimization_observability_upgrade import (  # noqa: E402
    CanonicalOptimizationObservabilityUpgradeBlocked,
    verify_optimization_observability_upgrade,
)
from app.canonical_v13.order_dispatch_status_upgrade import (  # noqa: E402
    CanonicalOrderDispatchStatusUpgradeBlocked,
    verify_order_dispatch_status_upgrade,
)
from app.canonical_v13.order_dispatch_recovery_upgrade import (  # noqa: E402
    CanonicalOrderDispatchRecoveryUpgradeBlocked,
    verify_order_dispatch_recovery_upgrade,
)
from app.canonical_v13.phase9_transition_upgrade import (  # noqa: E402
    DECISION_MODE_GUARD_TRIGGER,
    INTENT_MODE_GUARD_TRIGGER,
)
from app.canonical_v13.runtime_image_upgrade import (  # noqa: E402
    CanonicalRuntimeImageUpgradeBlocked,
    verify_runtime_image_upgrade,
)
from app.canonical_v13.shadow_risk_acl_upgrade import (  # noqa: E402
    CanonicalShadowRiskAclUpgradeBlocked,
    verify_shadow_risk_acl_upgrade,
)

BACKUP_CONTRACT: Final = "canonical-v13-postgres-data-backup-v2"
EXPECTED_TABLE_COUNT: Final = 58
IDENTITY_TABLE: Final = "schema_metadata"
RESTORE_NAME_PATTERN: Final = re.compile(
    rf"{re.escape(LOCAL_DATABASE_NAME)}_restore_[a-z0-9][a-z0-9_]*"
)
KNOWN_EXTERNAL_EXCLUDED_TABLES: Final[tuple[str, ...]] = (
    "public.okx_demo_attestation_secrets",
    "public.okx_demo_operator_consent_secrets",
)
SAFE_SENSITIVE_METADATA_COLUMNS: Final[tuple[str, ...]] = (
    "execution_attestations.credential_generation_digest",
    "execution_canary_probe_receipts.credential_generation_digest",
    "order_dispatch_receipts.credential_generation_digest",
    "order_dispatch_receipts.holder_token_digest",
    "order_writer_leases.holder_token_digest",
    "research_gate_attempts.lease_token_digest",
    "runtime_instances.credential_reference",
)
PUBLIC_MARKET_RUNTIME_CREDENTIAL_REFERENCE: Final = "none:public-okx-market-only"
EXPECTED_LIFECYCLE_TRIGGERS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "acceptance_signal_triggers",
        "acceptance_signal_triggers_immutable",
        "O",
    ),
    ("deployments", "deployments_disable_evidence_guard", "O"),
    (
        "optimization_runs",
        "optimization_runs_terminal_observability_guard",
        "O",
    ),
    ("research_gate_attempts", "research_gate_attempts_lifecycle", "O"),
    ("research_gate_receipts", "research_gate_receipts_append_only", "O"),
    ("risk_decisions", DECISION_MODE_GUARD_TRIGGER, "O"),
    (
        "runtime_image_acceptances",
        "runtime_image_acceptances_append_only",
        "O",
    ),
    ("signals", "acceptance_signals_immutable", "O"),
    ("trade_intents", INTENT_MODE_GUARD_TRIGGER, "O"),
    ("validation_plans", "validation_plans_gate_receipts", "O"),
)
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[object]]


class CanonicalBackupBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _unsafe_runtime_credential_reference_predicate(
    table_reference: str = '"strategy_platform_v13"."runtime_instances"',
) -> str:
    """Return the exact fail-closed predicate for runtime credential metadata."""

    def column(name: str) -> str:
        return f"{table_reference}.{name}"

    return (
        f"NOT ({column('credential_reference')} LIKE 'keychain:%' OR ("
        f"{column('credential_reference')} = "
        f"'{PUBLIC_MARKET_RUNTIME_CREDENTIAL_REFERENCE}' AND "
        f"{column('service_account')} = 'canonical_runtime_reader' AND "
        f"{column('network_policy')} = 'DEMO_EXCHANGE_ONLY' AND "
        f"{column('runtime_class')} = 'LONG_LIVED_TRADING_RUNTIME' AND "
        f"{column('filesystem_mode')} = 'READ_ONLY' AND "
        f"{column('research_executor_capability')} IS FALSE AND "
        f"{column('order_writer_capability')} IS FALSE))"
    )


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_manifest() -> None:
    if (
        len(CANONICAL_TABLE_NAMES) != EXPECTED_TABLE_COUNT
        or len(set(CANONICAL_TABLE_NAMES)) != EXPECTED_TABLE_COUNT
        or IDENTITY_TABLE not in CANONICAL_TABLE_NAMES
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_TABLE_MANIFEST",
            "the reviewed canonical manifest must contain exactly 58 unique tables",
        )
    forbidden = tuple(
        name
        for name in CANONICAL_TABLE_NAMES
        if any(token in name.lower() for token in ("secret", "credential", "keychain"))
    )
    if forbidden:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_SECRET_TABLE",
            f"credential-bearing table names are forbidden count={len(forbidden)}",
        )
    observed_sensitive_columns = tuple(
        sorted(
            f"{table_name}.{column.name}"
            for table_name, table in CANONICAL_TABLES.items()
            for column in table.columns
            if any(
                token in column.name.lower()
                for token in (
                    "secret",
                    "credential",
                    "password",
                    "api_key",
                    "passphrase",
                    "token",
                )
            )
        )
    )
    if observed_sensitive_columns != SAFE_SENSITIVE_METADATA_COLUMNS:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_SECRET_COLUMN",
            "credential-shaped columns differ from the reviewed digest/reference set",
        )


def _database_url(raw: str, *, expected_database: str, restore_target: bool) -> URL:
    try:
        parsed = make_url(raw)
    except Exception as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_DATABASE_URL", "database URL is invalid"
        ) from exc
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.database != expected_database
        or parsed.password is not None
        or parsed.host not in {None, "localhost", "127.0.0.1", "::1"}
        or bool(parsed.query)
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_DATABASE_TARGET",
            "an exact local passwordless postgresql+psycopg database URL is required",
        )
    if restore_target:
        if RESTORE_NAME_PATTERN.fullmatch(expected_database) is None:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_RESTORE_DATABASE_NAME",
                "restore target must be an explicit isolated canonical restore "
                "database",
            )
    elif expected_database != LOCAL_DATABASE_NAME:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_SOURCE_DATABASE",
            "backup source must be the exact canonical production database",
        )
    return parsed


def _libpq_url(parsed: URL) -> str:
    """Render a passwordless URL safe for a local process argument."""

    if parsed.password is not None:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_CREDENTIAL_URL",
            "database credentials must not appear in process arguments",
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _git(
    command: Sequence[str], *, repo_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )


def require_clean_release_checkout(
    *,
    repo_root: Path = REPO_ROOT,
    git_runner: Callable[..., subprocess.CompletedProcess[str]] = _git,
) -> str:
    if ".codex/worktrees" in str(repo_root):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED",
            "backup/restore must run from the accepted release checkout",
        )
    status = git_runner(("git", "status", "--porcelain"), repo_root=repo_root)
    head = git_runner(("git", "rev-parse", "HEAD"), repo_root=repo_root)
    main = git_runner(("git", "rev-parse", "origin/main"), repo_root=repo_root)
    head_sha = head.stdout.strip()
    if (
        status.returncode != 0
        or status.stdout.strip()
        or head.returncode != 0
        or main.returncode != 0
        or head_sha != main.stdout.strip()
        or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED",
            "release must be clean and exactly match origin/main",
        )
    return head_sha


def dump_command(
    *, binary: str, database_url: URL, output_path: Path
) -> tuple[str, ...]:
    _require_exact_manifest()
    command = [
        binary,
        "--format=custom",
        "--data-only",
        "--no-owner",
        "--no-privileges",
        "--strict-names",
        f"--schema={CANONICAL_BUSINESS_SCHEMA}",
        f"--exclude-table-data={CANONICAL_BUSINESS_SCHEMA}.{IDENTITY_TABLE}",
        f"--file={output_path}",
    ]
    command.extend(
        f"--table={CANONICAL_BUSINESS_SCHEMA}.{name}" for name in CANONICAL_TABLE_NAMES
    )
    command.append(_libpq_url(database_url))
    return tuple(command)


def restore_command(
    *, binary: str, database_url: URL, archive_path: Path
) -> tuple[str, ...]:
    return (
        binary,
        "--single-transaction",
        "--disable-triggers",
        "--exit-on-error",
        "--data-only",
        "--no-owner",
        "--no-privileges",
        f"--dbname={_libpq_url(database_url)}",
        str(archive_path),
    )


def build_manifest(
    *,
    archive_name: str,
    archive_sha256: str,
    release_sha: str,
    row_counts: Mapping[str, int],
    created_at: datetime,
) -> dict[str, object]:
    _require_exact_manifest()
    if created_at.tzinfo is None:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_TIMESTAMP", "created_at must be timezone-aware"
        )
    counts = dict(row_counts)
    if set(counts) != set(CANONICAL_TABLE_NAMES) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_ROW_COUNTS",
            "row counts must cover every canonical table with non-negative integers",
        )
    for value, field, pattern in (
        (archive_sha256, "archive_sha256", r"[0-9a-f]{64}"),
        (release_sha, "release_sha", r"[0-9a-f]{40}"),
    ):
        if re.fullmatch(pattern, value) is None:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_BACKUP_DIGEST", f"{field} is invalid"
            )
    return {
        "contract": BACKUP_CONTRACT,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "release_sha": release_sha,
        "source_database": LOCAL_DATABASE_NAME,
        "business_schema": CANONICAL_BUSINESS_SCHEMA,
        "archive_file": archive_name,
        "archive_sha256": archive_sha256,
        "archive_format": "postgresql-custom-data-only",
        "canonical_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "authority_mapping_digest": local_role_mapping().mapping_digest,
        "table_count": EXPECTED_TABLE_COUNT,
        "tables": list(CANONICAL_TABLE_NAMES),
        "row_counts": {name: counts[name] for name in CANONICAL_TABLE_NAMES},
        "total_row_count": sum(counts.values()),
        "identity_tables_preserved_on_restore": [IDENTITY_TABLE],
        "secret_exclusion_policy": {
            "included_schemas": [CANONICAL_BUSINESS_SCHEMA],
            "excluded_external_tables": list(KNOWN_EXTERNAL_EXCLUDED_TABLES),
            "safe_digest_or_reference_columns": list(SAFE_SENSITIVE_METADATA_COLUMNS),
            "credential_values_recorded": False,
            "keychain_accessed": False,
            "database_password_in_process_arguments": False,
        },
        "restore_contract": {
            "explicit_new_isolated_database_required": True,
            "exact_empty_genesis_manifest_acl_required": True,
            "single_transaction": True,
            "superuser_restore_required": True,
            "triggers_disabled_only_inside_restore_transaction": True,
            "exact_enabled_trigger_state_verified_before_and_after": True,
            "gate_trigger_contract_verified_before_and_after": True,
            "exact_post_restore_row_counts_required": True,
        },
    }


def validate_manifest(
    payload: object, *, archive_path: Path | None = None
) -> dict[str, object]:
    _require_exact_manifest()
    if not isinstance(payload, dict):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_MANIFEST", "manifest must be an object"
        )
    expected = {
        "contract": BACKUP_CONTRACT,
        "source_database": LOCAL_DATABASE_NAME,
        "business_schema": CANONICAL_BUSINESS_SCHEMA,
        "archive_format": "postgresql-custom-data-only",
        "canonical_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "authority_mapping_digest": local_role_mapping().mapping_digest,
        "table_count": EXPECTED_TABLE_COUNT,
        "tables": list(CANONICAL_TABLE_NAMES),
        "identity_tables_preserved_on_restore": [IDENTITY_TABLE],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_MANIFEST_DRIFT",
            "table, authority, or manifest identity differs from this release",
        )
    policy = payload.get("secret_exclusion_policy")
    restore = payload.get("restore_contract")
    if policy != {
        "included_schemas": [CANONICAL_BUSINESS_SCHEMA],
        "excluded_external_tables": list(KNOWN_EXTERNAL_EXCLUDED_TABLES),
        "safe_digest_or_reference_columns": list(SAFE_SENSITIVE_METADATA_COLUMNS),
        "credential_values_recorded": False,
        "keychain_accessed": False,
        "database_password_in_process_arguments": False,
    } or restore != {
        "explicit_new_isolated_database_required": True,
        "exact_empty_genesis_manifest_acl_required": True,
        "single_transaction": True,
        "superuser_restore_required": True,
        "triggers_disabled_only_inside_restore_transaction": True,
        "exact_enabled_trigger_state_verified_before_and_after": True,
        "gate_trigger_contract_verified_before_and_after": True,
        "exact_post_restore_row_counts_required": True,
    }:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_SAFETY_POLICY",
            "secret exclusion or restore safety policy drifted",
        )
    counts = payload.get("row_counts")
    if not isinstance(counts, dict) or set(counts) != set(CANONICAL_TABLE_NAMES):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_ROW_COUNTS", "manifest row counts are incomplete"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ) or payload.get("total_row_count") != sum(counts.values()):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_ROW_COUNTS", "manifest row counts are invalid"
        )
    archive_digest = payload.get("archive_sha256")
    if (
        not isinstance(archive_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", archive_digest) is None
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_DIGEST", "archive digest is invalid"
        )
    release_sha = payload.get("release_sha")
    archive_file = payload.get("archive_file")
    if (
        not isinstance(release_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", release_sha) is None
        or not isinstance(archive_file, str)
        or Path(archive_file).name != archive_file
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_MANIFEST",
            "release identity or archive filename is invalid",
        )
    if archive_path is not None:
        if payload.get("archive_file") != archive_path.name:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_BACKUP_ARCHIVE_NAME", "archive filename drifted"
            )
        if not archive_path.is_file() or _sha256_file(archive_path) != archive_digest:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_BACKUP_ARCHIVE_DIGEST", "archive bytes drifted"
            )
    return payload


def _service_principals() -> dict[str, str]:
    return {
        **LOCAL_SERVICE_PRINCIPALS,
        **LOCAL_RESEARCH_SERVICE_PRINCIPALS,
        **LOCAL_PHASE9_SERVICE_PRINCIPALS,
        **LOCAL_RUNTIME_SERVICE_PRINCIPALS,
    }


def _verify_restore_trigger_boundary(
    connection: Connection, *, require_superuser: bool
) -> None:
    if connection.dialect.name != "postgresql":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_POSTGRESQL_REQUIRED",
            "trigger-safe restore requires PostgreSQL",
        )
    if require_superuser:
        is_superuser = bool(
            connection.execute(
                text(
                    "SELECT roles.rolsuper FROM pg_catalog.pg_roles roles "
                    "WHERE roles.rolname=current_user"
                )
            ).scalar_one()
        )
        if not is_superuser:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_RESTORE_SUPERUSER_REQUIRED",
                "pg_restore --disable-triggers requires a local restore superuser",
            )
    observed_triggers = tuple(
        connection.execute(
            text(
                """
                SELECT relation.relname, trigger.tgname, trigger.tgenabled
                FROM pg_catalog.pg_trigger trigger
                JOIN pg_catalog.pg_class relation ON relation.oid=trigger.tgrelid
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname=:schema AND NOT trigger.tgisinternal
                ORDER BY relation.relname, trigger.tgname
                """
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA},
        )
    )
    if observed_triggers != EXPECTED_LIFECYCLE_TRIGGERS:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_TRIGGER_STATE",
            "canonical non-internal lifecycle triggers differ from the exact "
            f"normally-enabled contract count={len(observed_triggers)}",
        )
    try:
        gate = verify_gate_receipt_upgrade(connection)
    except CanonicalGateReceiptUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_GATE_TRIGGER_CONTRACT",
            f"gate trigger contract is not accepted: {exc.code}",
        ) from exc
    if gate.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_GATE_TRIGGER_CONTRACT",
            "gate trigger verifier did not return ACCEPTED",
        )
    try:
        runtime_image = verify_runtime_image_upgrade(connection)
    except CanonicalRuntimeImageUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_RUNTIME_IMAGE_TRIGGER_CONTRACT",
            "runtime image authority trigger contract is not accepted",
        ) from exc
    if runtime_image.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_RUNTIME_IMAGE_TRIGGER_CONTRACT",
            "runtime image authority verifier did not return ACCEPTED",
        )
    try:
        rollover = verify_deployment_rollover_upgrade(connection)
    except CanonicalDeploymentRolloverUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_DEPLOYMENT_ROLLOVER_TRIGGER_CONTRACT",
            "deployment rollover trigger contract is not accepted",
        ) from exc
    if rollover.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_DEPLOYMENT_ROLLOVER_TRIGGER_CONTRACT",
            "deployment rollover verifier did not return ACCEPTED",
        )
    try:
        acceptance_trigger = verify_acceptance_signal_trigger_upgrade(
            connection, role_mapping=local_role_mapping()
        )
    except CanonicalAcceptanceSignalTriggerUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ACCEPTANCE_TRIGGER_CONTRACT",
            "acceptance signal trigger contract is not accepted",
        ) from exc
    if acceptance_trigger.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ACCEPTANCE_TRIGGER_CONTRACT",
            "acceptance signal trigger verifier did not return ACCEPTED",
        )
    try:
        optimization = verify_optimization_observability_upgrade(connection)
    except CanonicalOptimizationObservabilityUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_OPTIMIZATION_OBSERVABILITY_CONTRACT",
            "optimization observability trigger contract is not accepted",
        ) from exc
    if optimization.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_OPTIMIZATION_OBSERVABILITY_CONTRACT",
            "optimization observability verifier did not return ACCEPTED",
        )
    try:
        shadow_risk_acl = verify_shadow_risk_acl_upgrade(
            connection, role_mapping=local_role_mapping()
        )
    except CanonicalShadowRiskAclUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_SHADOW_RISK_ACL_CONTRACT",
            "shadow risk ACL contract is not accepted",
        ) from exc
    if shadow_risk_acl.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_SHADOW_RISK_ACL_CONTRACT",
            "shadow risk ACL verifier did not return ACCEPTED",
        )
    try:
        dispatch_status = verify_order_dispatch_status_upgrade(connection)
    except CanonicalOrderDispatchStatusUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ORDER_DISPATCH_STATUS_CONTRACT",
            "order dispatch status contract is not accepted",
        ) from exc
    if dispatch_status.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ORDER_DISPATCH_STATUS_CONTRACT",
            "order dispatch status verifier did not return ACCEPTED",
        )
    try:
        dispatch_recovery = verify_order_dispatch_recovery_upgrade(connection)
    except CanonicalOrderDispatchRecoveryUpgradeBlocked as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ORDER_DISPATCH_RECOVERY_CONTRACT",
            "order dispatch recovery contract is not accepted",
        ) from exc
    if dispatch_recovery.status != "ACCEPTED":
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ORDER_DISPATCH_RECOVERY_CONTRACT",
            "order dispatch recovery verifier did not return ACCEPTED",
        )


def inspect_database(
    database_url: URL, *, require_zero: bool, require_superuser: bool = False
) -> dict[str, int]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                verification = verify_postgresql_bootstrap(
                    connection,
                    role_mapping=local_role_mapping(),
                    require_zero_business_rows=require_zero,
                    service_principals=_service_principals(),
                )
                if (
                    not verification.accepted
                    or verification.table_count != EXPECTED_TABLE_COUNT
                ):
                    raise CanonicalBackupBlocked(
                        "BLOCKED_CANONICAL_BACKUP_DATABASE_VERIFICATION",
                        "; ".join(verification.problems) or "table count drifted",
                    )
                _verify_restore_trigger_boundary(
                    connection, require_superuser=require_superuser
                )
                counts = {
                    name: int(
                        connection.execute(
                            text(
                                "SELECT count(*) FROM "
                                f'"{CANONICAL_BUSINESS_SCHEMA}"."{name}"'
                            )
                        ).scalar_one()
                    )
                    for name in CANONICAL_TABLE_NAMES
                }
                unsafe_credential_references = int(
                    connection.execute(
                        text(
                            'SELECT count(*) FROM "strategy_platform_v13".'
                            '"runtime_instances" WHERE '
                            + _unsafe_runtime_credential_reference_predicate()
                        )
                    ).scalar_one()
                )
                if unsafe_credential_references:
                    raise CanonicalBackupBlocked(
                        "BLOCKED_CANONICAL_BACKUP_CREDENTIAL_REFERENCE",
                        "runtime credential metadata must be an opaque Keychain "
                        "ref or the exact capability-free public market sentinel",
                    )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    if require_zero and any(
        count for name, count in counts.items() if name != IDENTITY_TABLE
    ):
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_TARGET_NONEMPTY",
            "restore target contains canonical business rows",
        )
    if require_zero and counts[IDENTITY_TABLE] != 1:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_IDENTITY",
            "restore target must contain exactly one reviewed genesis identity",
        )
    return counts


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[object]:
    return subprocess.run(list(command), check=False, capture_output=True, text=False)


def _binary(name: str, explicit: str | None) -> str:
    resolved = explicit or shutil.which(name)
    if not resolved or not Path(resolved).is_file():
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_BINARY", f"{name} is unavailable"
        )
    return str(Path(resolved).resolve())


def _safe_output_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_OUTPUT_DIRECTORY",
            "backup output must be outside the repository",
        )
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved, 0o700)
    return resolved


def create_backup(
    *,
    source_database_url: str,
    output_directory: Path,
    pg_dump_binary: str | None = None,
    runner: Runner = _default_runner,
    release_guard: Callable[[], str] = require_clean_release_checkout,
    database_inspector: Callable[..., dict[str, int]] = inspect_database,
) -> dict[str, object]:
    release_sha = release_guard()
    parsed = _database_url(
        source_database_url,
        expected_database=LOCAL_DATABASE_NAME,
        restore_target=False,
    )
    counts = database_inspector(parsed, require_zero=False)
    output = _safe_output_directory(output_directory)
    identity = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"
    archive = output / f"canonical-v13-{identity}.dump"
    manifest_path = output / f"canonical-v13-{identity}.manifest.json"
    archive_tmp = output / f".{archive.name}.tmp"
    manifest_tmp = output / f".{manifest_path.name}.tmp"
    try:
        descriptor = os.open(archive_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        completed = runner(
            dump_command(
                binary=_binary("pg_dump", pg_dump_binary),
                database_url=parsed,
                output_path=archive_tmp,
            )
        )
        if completed.returncode != 0 or archive_tmp.stat().st_size == 0:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_BACKUP_DUMP", "pg_dump did not create an archive"
            )
        manifest = build_manifest(
            archive_name=archive.name,
            archive_sha256=_sha256_file(archive_tmp),
            release_sha=release_sha,
            row_counts=counts,
            created_at=datetime.now(timezone.utc),
        )
        descriptor = os.open(manifest_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(manifest, target, ensure_ascii=True, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(archive_tmp, archive)
        os.replace(manifest_tmp, manifest_path)
        return {
            "status": "BACKED_UP",
            "release_sha": release_sha,
            "archive_path": str(archive),
            "manifest_path": str(manifest_path),
            "archive_sha256": manifest["archive_sha256"],
            "table_count": EXPECTED_TABLE_COUNT,
            "total_row_count": manifest["total_row_count"],
            "credential_values_recorded": False,
        }
    except Exception:
        archive_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
        raise


def restore_backup(
    *,
    restore_database_url: str,
    restore_database_name: str,
    archive_path: Path,
    manifest_path: Path,
    pg_restore_binary: str | None = None,
    runner: Runner = _default_runner,
    release_guard: Callable[[], str] = require_clean_release_checkout,
    database_inspector: Callable[..., dict[str, int]] = inspect_database,
) -> dict[str, object]:
    release_sha = release_guard()
    parsed = _database_url(
        restore_database_url,
        expected_database=restore_database_name,
        restore_target=True,
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_BACKUP_MANIFEST", "manifest is unreadable"
        ) from exc
    manifest = validate_manifest(payload, archive_path=archive_path)
    if manifest["release_sha"] != release_sha:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_RELEASE_DRIFT",
            "backup release does not match the accepted restore release",
        )
    database_inspector(parsed, require_zero=True, require_superuser=True)
    completed = runner(
        restore_command(
            binary=_binary("pg_restore", pg_restore_binary),
            database_url=parsed,
            archive_path=archive_path,
        )
    )
    if completed.returncode != 0:
        try:
            database_inspector(parsed, require_zero=True, require_superuser=True)
        except CanonicalBackupBlocked as exc:
            raise CanonicalBackupBlocked(
                "BLOCKED_CANONICAL_RESTORE_TRIGGER_RECOVERY",
                "failed restore did not preserve the exact enabled trigger boundary: "
                f"{exc.code}",
            ) from exc
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE", "pg_restore failed atomically"
        )
    observed = database_inspector(parsed, require_zero=False, require_superuser=True)
    expected_counts = manifest["row_counts"]
    if observed != expected_counts:
        raise CanonicalBackupBlocked(
            "BLOCKED_CANONICAL_RESTORE_ROW_COUNTS",
            "post-restore canonical row counts differ from the backup manifest",
        )
    return {
        "status": "RESTORED_AND_VERIFIED",
        "release_sha": release_sha,
        "restore_database": restore_database_name,
        "archive_sha256": manifest["archive_sha256"],
        "canonical_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "authority_mapping_digest": local_role_mapping().mapping_digest,
        "table_count": EXPECTED_TABLE_COUNT,
        "total_row_count": manifest["total_row_count"],
        "credential_values_recorded": False,
    }


def plan() -> dict[str, object]:
    _require_exact_manifest()
    return {
        "status": "READY",
        "contract": BACKUP_CONTRACT,
        "source_database": LOCAL_DATABASE_NAME,
        "restore_database_pattern": RESTORE_NAME_PATTERN.pattern,
        "business_schema": CANONICAL_BUSINESS_SCHEMA,
        "canonical_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "authority_mapping_digest": local_role_mapping().mapping_digest,
        "table_count": EXPECTED_TABLE_COUNT,
        "credential_values_recorded": False,
        "execution_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source-database-url", required=True)
    backup_parser.add_argument("--output-directory", type=Path, required=True)
    backup_parser.add_argument("--pg-dump-binary")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--restore-database-url", required=True)
    restore_parser.add_argument("--restore-database-name", required=True)
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--manifest", type=Path, required=True)
    restore_parser.add_argument("--pg-restore-binary")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan()
        elif args.command == "backup":
            result = create_backup(
                source_database_url=args.source_database_url,
                output_directory=args.output_directory,
                pg_dump_binary=args.pg_dump_binary,
            )
        else:
            result = restore_backup(
                restore_database_url=args.restore_database_url,
                restore_database_name=args.restore_database_name,
                archive_path=args.archive,
                manifest_path=args.manifest,
                pg_restore_binary=args.pg_restore_binary,
            )
    except CanonicalBackupBlocked as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": exc.code, "detail": exc.detail},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
