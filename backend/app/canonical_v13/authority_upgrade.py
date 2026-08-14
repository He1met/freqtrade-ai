"""Transactional authority upgrade for the canonical research writer split.

The upgrade changes PostgreSQL roles and ACLs only.  It never creates, drops,
deletes, or rewrites a canonical business table.  Existing databases are eligible
only while all validation, scoring, qualification, and optimization tables are
empty.  The legacy broad research role is retained as a NOLOGIN rollback anchor,
but receives no schema or table privilege after a successful upgrade.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Final, Mapping
from uuid import uuid4

from sqlalchemy import Connection, func, select, text

from app.canonical_v13.genesis import (
    render_postgresql_acl_sql,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    CANONICAL_TABLE_NAMES,
    READER_IDENTITIES,
    READER_TABLE_ALLOWLIST,
    TABLE_MANIFEST_BY_NAME,
    WRITER_IDENTITIES,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
)
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    CANONICAL_TABLES,
    SCHEMA_METADATA_TABLE,
)
from app.canonical_v13.role_mapping import (
    POSTGRESQL_ROLE_PATTERN,
    CanonicalRoleMapping,
)


AUTHORITY_UPGRADE_KEY: Final = "canonical-v13-research-writer-split-v1"
AUTHORITY_UPGRADE_EVENT: Final = "CANONICAL_RESEARCH_AUTHORITY_UPGRADED"
AUTHORITY_ROLLBACK_EVENT: Final = "CANONICAL_RESEARCH_AUTHORITY_ROLLED_BACK"
PREVIOUS_CANONICAL_MANIFEST_DIGEST: Final = (
    "282a29277220c1626800356e37f121ed6e3800d72c49d0bb60573a9fb006f9e6"
)
LEGACY_RESEARCH_WRITER_IDENTITY: Final = "canonical_research_writer"
SPLIT_RESEARCH_WRITER_IDENTITIES: Final[tuple[str, ...]] = (
    "canonical_validation_writer",
    "canonical_scoring_writer",
    "canonical_qualification_writer",
    "canonical_optimization_writer",
)
RESEARCH_AUTHORITY_TABLES: Final[tuple[str, ...]] = (
    "validation_plans",
    "validation_plan_windows",
    "validation_attempts",
    "validation_window_results",
    "target_scores",
    "qualification_decisions",
    "qualification_window_evidence",
    "optimization_runs",
    "optimization_trials",
)

_PREVIOUS_WRITER_TABLE_ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = (
    MappingProxyType(
        {
            **{
                role: tables
                for role, tables in WRITER_TABLE_ALLOWLIST.items()
                if role not in SPLIT_RESEARCH_WRITER_IDENTITIES
            },
            LEGACY_RESEARCH_WRITER_IDENTITY: RESEARCH_AUTHORITY_TABLES,
        }
    )
)
_PREVIOUS_WRITER_READ_ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = (
    MappingProxyType(
        {
            **{
                role: tables
                for role, tables in WRITER_READ_ALLOWLIST.items()
                if role not in SPLIT_RESEARCH_WRITER_IDENTITIES
            },
            LEGACY_RESEARCH_WRITER_IDENTITY: (
                "schema_metadata",
                "strategy_artifacts",
                "strategy_versions",
                "configuration_activations",
                "configuration_snapshots",
                "configuration_snapshot_members",
                "configuration_bundles",
                "configuration_bundle_members",
                "research_targets",
                "research_target_allocations",
                "market_snapshots",
                "market_snapshot_members",
            ),
        }
    )
)


class CanonicalAuthorityUpgradeBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class AuthorityUpgradeVerification:
    accepted: bool
    state: str
    problems: tuple[str, ...]
    manifest_digest: str | None
    research_row_count: int | None
    capability_role_count: int
    explicit_acl_count: int


@dataclass(frozen=True)
class AuthorityUpgradeResult:
    status: str
    state: str
    generation: int
    previous_manifest_digest: str
    current_manifest_digest: str
    role_mapping_digest: str
    request_digest: str | None
    receipt_digest: str | None
    research_row_count: int
    legacy_role_retained: bool


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_actor(actor_identity: str) -> str:
    if (
        not actor_identity
        or actor_identity != actor_identity.strip()
        or len(actor_identity) > 160
    ):
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_ACTOR", "actor identity is invalid"
        )
    return actor_identity


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_TIMESTAMP",
            "authority timestamps require timezone",
        )
    return value.astimezone(timezone.utc)


def _require_legacy_role(role: str) -> str:
    if not POSTGRESQL_ROLE_PATTERN.fullmatch(role):
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_LEGACY_RESEARCH_ROLE", "legacy role name is invalid"
        )
    if role in set(CanonicalRoleMapping.identity().roles.values()):
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_LEGACY_RESEARCH_ROLE",
            "legacy role must be distinct from every current capability role",
        )
    return role


def _previous_physical_roles(
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> dict[str, str]:
    legacy = _require_legacy_role(legacy_research_writer_role)
    roles = {
        logical: role_mapping.physical(logical)
        for logical in (*WRITER_IDENTITIES, *READER_IDENTITIES)
        if logical not in SPLIT_RESEARCH_WRITER_IDENTITIES
    }
    roles[LEGACY_RESEARCH_WRITER_IDENTITY] = legacy
    return roles


def _expected_grants(
    *,
    physical_roles: Mapping[str, str],
    writer_tables: Mapping[str, tuple[str, ...]],
    writer_reads: Mapping[str, tuple[str, ...]],
) -> set[tuple[str, str, str]]:
    grants: dict[tuple[str, str], set[str]] = defaultdict(set)
    for writer, table_names in writer_tables.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            grants[(physical_roles[writer], table_name)].update(
                TABLE_MANIFEST_BY_NAME[table_name].writer_privileges
            )
    for writer, table_names in writer_reads.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            grants[(physical_roles[writer], table_name)].add("SELECT")
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            grants[(physical_roles[reader], table_name)].add("SELECT")
    return {
        (role, table_name, privilege)
        for (role, table_name), privileges in grants.items()
        for privilege in privileges
    }


def _current_physical_roles(role_mapping: CanonicalRoleMapping) -> dict[str, str]:
    return {
        logical: role_mapping.physical(logical)
        for logical in (*WRITER_IDENTITIES, *READER_IDENTITIES)
    }


def _actual_grants(
    connection: Connection, *, owner: str
) -> set[tuple[str, str, str]]:
    return {
        tuple(str(value) for value in row)
        for row in connection.execute(
            text(
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.table_privileges
                WHERE table_schema=:schema AND grantee <> :owner
                """
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "owner": owner,
            },
        )
    }


def _actual_schema_grants(
    connection: Connection,
) -> set[tuple[str, str]]:
    return {
        (str(grantee), str(privilege))
        for grantee, privilege in connection.execute(
            text(
                """
                SELECT COALESCE(role.rolname, 'PUBLIC'), acl.privilege_type
                FROM pg_catalog.pg_namespace namespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        namespace.nspacl,
                        pg_catalog.acldefault('n', namespace.nspowner)
                    )
                ) acl
                LEFT JOIN pg_catalog.pg_roles role ON role.oid=acl.grantee
                WHERE namespace.nspname=:schema
                """
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA},
        )
    }


def _role_problems(
    connection: Connection,
    *,
    required_roles: tuple[str, ...],
    optional_roles: tuple[str, ...],
    isolated_roles: tuple[str, ...],
) -> tuple[list[str], int]:
    all_roles = tuple(dict.fromkeys((*required_roles, *optional_roles)))
    rows = connection.execute(
        text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                   rolinherit, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = ANY(:roles)
            """
        ),
        {"roles": list(all_roles)},
    ).all()
    observed = {str(row[0]) for row in rows}
    problems: list[str] = []
    missing = sorted(set(required_roles) - observed)
    if missing:
        problems.append(f"missing physical capability roles: {missing!r}")
    for row in rows:
        if str(row[0]) in isolated_roles and any(bool(value) for value in row[1:]):
            problems.append(f"capability role is not isolated NOLOGIN: {row[0]}")

    memberships = connection.execute(
        text(
            """
            SELECT parent.rolname, member.rolname
            FROM pg_catalog.pg_auth_members membership
            JOIN pg_catalog.pg_roles member ON member.oid=membership.member
            JOIN pg_catalog.pg_roles parent ON parent.oid=membership.roleid
            WHERE member.rolname = ANY(:roles)
               OR parent.rolname = ANY(:roles)
            """
        ),
        {"roles": list(isolated_roles)},
    ).all()
    if memberships:
        problems.append(
            "legacy/split research roles must have zero memberships "
            f"count={len(memberships)}"
        )
    return problems, len(rows)


def _research_row_counts(connection: Connection) -> dict[str, int]:
    return {
        table_name: int(
            connection.execute(
                select(func.count()).select_from(CANONICAL_TABLES[table_name])
            ).scalar_one()
        )
        for table_name in RESEARCH_AUTHORITY_TABLES
    }


def _event_history(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> tuple[int, int, tuple[str, ...]]:
    rows = connection.execute(
        select(AUDIT_EVENTS_TABLE).where(
            AUDIT_EVENTS_TABLE.c.aggregate_type == "canonical_authority_upgrade",
            AUDIT_EVENTS_TABLE.c.aggregate_id.like(AUTHORITY_UPGRADE_KEY + ":%"),
        )
    ).mappings().all()
    problems: list[str] = []
    observed: set[tuple[str, int]] = set()
    upgrades = 0
    rollbacks = 0
    expected_split_roles = [
        role_mapping.physical(role) for role in SPLIT_RESEARCH_WRITER_IDENTITIES
    ]
    expected_evidence_keys = {
        "contract",
        "direction",
        "generation",
        "previous_manifest_digest",
        "current_manifest_digest",
        "role_mapping_digest",
        "legacy_role",
        "split_roles",
        "research_tables",
        "verified_research_row_count",
        "actor_identity",
        "applied_at",
        "destructive_table_operations",
    }
    for row in rows:
        event_type = str(row["event_type"])
        if event_type == AUTHORITY_UPGRADE_EVENT:
            direction = "UPGRADE"
            upgrades += 1
        elif event_type == AUTHORITY_ROLLBACK_EVENT:
            direction = "ROLLBACK"
            rollbacks += 1
        else:
            problems.append(f"unknown authority event type: {event_type}")
            continue
        evidence = row["evidence_json"]
        if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
            problems.append(f"authority event evidence shape drift: {event_type}")
            continue
        generation = evidence.get("generation")
        if not isinstance(generation, int) or generation <= 0:
            problems.append(f"authority event generation drift: {event_type}")
            continue
        event_key = (event_type, generation)
        if event_key in observed:
            problems.append(f"duplicate authority event generation: {event_key!r}")
        observed.add(event_key)
        aggregate_id = f"{AUTHORITY_UPGRADE_KEY}:{generation}"
        applied_at = evidence.get("applied_at")
        expected_fields = {
            "contract": AUTHORITY_UPGRADE_KEY,
            "direction": direction,
            "generation": generation,
            "previous_manifest_digest": PREVIOUS_CANONICAL_MANIFEST_DIGEST,
            "current_manifest_digest": CANONICAL_MANIFEST_DIGEST,
            "role_mapping_digest": role_mapping.mapping_digest,
            "legacy_role": legacy_research_writer_role,
            "split_roles": expected_split_roles,
            "research_tables": list(RESEARCH_AUTHORITY_TABLES),
            "verified_research_row_count": 0,
            "actor_identity": row["actor_identity"],
            "applied_at": applied_at,
            "destructive_table_operations": [],
        }
        request_digest = _digest({**expected_fields, "applied_at": None})
        receipt_digest = _digest(
            {
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "request_digest": request_digest,
                "applied_at": applied_at,
            }
        )
        created_at = row["created_at"]
        created_at_text = (
            created_at.astimezone(timezone.utc).isoformat()
            if isinstance(created_at, datetime) and created_at.tzinfo is not None
            else None
        )
        if (
            evidence != expected_fields
            or row["aggregate_id"] != aggregate_id
            or row["request_digest"] != request_digest
            or row["receipt_digest"] != receipt_digest
            or created_at_text != applied_at
        ):
            problems.append(
                f"authority event receipt drift: {event_type}:{generation}"
            )
    upgrade_generations = {
        generation
        for event, generation in observed
        if event == AUTHORITY_UPGRADE_EVENT
    }
    if upgrade_generations != set(range(1, upgrades + 1)):
        problems.append("authority upgrade generations are not contiguous")
    rollback_generations = {
        generation
        for event, generation in observed
        if event == AUTHORITY_ROLLBACK_EVENT
    }
    if rollback_generations != set(range(1, rollbacks + 1)):
        problems.append("authority rollback generations are not contiguous")
    return upgrades, rollbacks, tuple(problems)


def verify_authority_upgrade_state(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
    require_no_research_rows: bool = True,
) -> AuthorityUpgradeVerification:
    """Classify an exact previous/current ACL state without mutating it."""

    if connection.dialect.name != "postgresql":
        return AuthorityUpgradeVerification(
            accepted=False,
            state="BLOCKED",
            problems=("BLOCKED_POSTGRESQL_REQUIRED",),
            manifest_digest=None,
            research_row_count=None,
            capability_role_count=0,
            explicit_acl_count=0,
        )
    previous_roles = _previous_physical_roles(
        role_mapping, legacy_research_writer_role
    )
    current_roles = _current_physical_roles(role_mapping)
    verification = verify_canonical_genesis(
        connection,
        accepted_manifest_digests=(
            PREVIOUS_CANONICAL_MANIFEST_DIGEST,
            CANONICAL_MANIFEST_DIGEST,
        ),
    )
    problems = list(verification.problems)
    digest = verification.manifest_digest
    if digest == PREVIOUS_CANONICAL_MANIFEST_DIGEST:
        state = "PREVIOUS_READY"
        required_roles = tuple(previous_roles.values())
        optional_roles = tuple(
            current_roles[role] for role in SPLIT_RESEARCH_WRITER_IDENTITIES
        )
        expected = _expected_grants(
            physical_roles=previous_roles,
            writer_tables=_PREVIOUS_WRITER_TABLE_ALLOWLIST,
            writer_reads=_PREVIOUS_WRITER_READ_ALLOWLIST,
        )
    elif digest == CANONICAL_MANIFEST_DIGEST:
        state = "CURRENT"
        required_roles = tuple(current_roles.values())
        optional_roles = (legacy_research_writer_role,)
        expected = _expected_grants(
            physical_roles=current_roles,
            writer_tables=WRITER_TABLE_ALLOWLIST,
            writer_reads=WRITER_READ_ALLOWLIST,
        )
    else:
        state = "BLOCKED"
        required_roles = ()
        optional_roles = ()
        expected = set()
        problems.append(
            "manifest digest is neither the reviewed previous nor current authority"
        )

    role_problems, role_count = _role_problems(
        connection,
        required_roles=required_roles,
        optional_roles=optional_roles,
        isolated_roles=(
            legacy_research_writer_role,
            *(current_roles[role] for role in SPLIT_RESEARCH_WRITER_IDENTITIES),
        ),
    )
    problems.extend(role_problems)
    owner = role_mapping.physical("canonical_schema_owner")
    actual = _actual_grants(connection, owner=owner)
    missing = expected - actual
    extra = actual - expected
    if missing:
        problems.append(f"missing table grants count={len(missing)}")
    if extra:
        problems.append(f"extra table grants count={len(extra)}")
    expected_schema_grants = {
        (role, privilege)
        for role, privileges in {
            **{role: ("USAGE",) for role in required_roles},
            owner: ("CREATE", "USAGE"),
        }.items()
        for privilege in privileges
    }
    actual_schema_grants = _actual_schema_grants(connection)
    missing_schema = expected_schema_grants - actual_schema_grants
    extra_schema = actual_schema_grants - expected_schema_grants
    if missing_schema:
        problems.append(f"missing schema grants count={len(missing_schema)}")
    if extra_schema:
        problems.append(f"extra schema grants count={len(extra_schema)}")

    upgrades, rollbacks, history_problems = _event_history(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy_research_writer_role,
    )
    problems.extend(history_problems)
    if state == "PREVIOUS_READY" and upgrades != rollbacks:
        problems.append(
            "previous authority requires balanced upgrade/rollback receipts"
        )
    if state == "CURRENT" and not (
        (upgrades == 0 and rollbacks == 0) or upgrades == rollbacks + 1
    ):
        problems.append("current authority receipt history is inconsistent")

    row_count: int | None = None
    if not verification.problems:
        counts = _research_row_counts(connection)
        row_count = sum(counts.values())
        if require_no_research_rows and row_count:
            nonzero = {key: value for key, value in counts.items() if value}
            problems.append(
                "research authority tables are not empty: "
                + _canonical(nonzero)
            )
    return AuthorityUpgradeVerification(
        accepted=not problems,
        state=state if not problems else "BLOCKED",
        problems=tuple(problems),
        manifest_digest=digest,
        research_row_count=row_count,
        capability_role_count=role_count,
        explicit_acl_count=len(actual),
    )


def _execute_acl_sql(connection: Connection, sql: str) -> None:
    for statement in sql.rstrip(";\n").split(";\n"):
        if statement.strip():
            connection.exec_driver_sql(statement)


def render_previous_authority_acl_sql(
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> str:
    physical = _previous_physical_roles(role_mapping, legacy_research_writer_role)
    roles = tuple(physical.values())
    statements: list[str] = [
        f"REVOKE ALL PRIVILEGES ON SCHEMA {CANONICAL_BUSINESS_SCHEMA} FROM PUBLIC"
    ]
    statements.extend(
        f"GRANT USAGE ON SCHEMA {CANONICAL_BUSINESS_SCHEMA} TO {role}"
        for role in roles
    )
    for table_name in CANONICAL_TABLE_NAMES:
        qualified = f"{CANONICAL_BUSINESS_SCHEMA}.{table_name}"
        statements.append(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM PUBLIC")
        statements.extend(
            f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {role}"
            for role in roles
        )
    for writer, table_names in _PREVIOUS_WRITER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            privileges = TABLE_MANIFEST_BY_NAME[table_name].writer_privileges
            statements.append(
                "GRANT "
                + ", ".join(privileges)
                + f" ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
                + f"TO {physical[writer]}"
            )
    for writer, table_names in _PREVIOUS_WRITER_READ_ALLOWLIST.items():
        statements.extend(
            f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            f"TO {physical[writer]}"
            for table_name in table_names
        )
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        statements.extend(
            f"GRANT SELECT ON TABLE {CANONICAL_BUSINESS_SCHEMA}.{table_name} "
            f"TO {physical[reader]}"
            for table_name in table_names
        )
    return ";\n".join(statements) + ";\n"


def _render_revoke_sql(roles: tuple[str, ...]) -> str:
    statements: list[str] = []
    for role in roles:
        statements.append(
            f"REVOKE ALL PRIVILEGES ON SCHEMA {CANONICAL_BUSINESS_SCHEMA} FROM {role}"
        )
        statements.extend(
            "REVOKE ALL PRIVILEGES ON TABLE "
            f"{CANONICAL_BUSINESS_SCHEMA}.{table_name} FROM {role}"
            for table_name in CANONICAL_TABLE_NAMES
        )
    return ";\n".join(statements) + (";\n" if statements else "")


def render_authority_upgrade_acl_sql(
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> str:
    legacy = _require_legacy_role(legacy_research_writer_role)
    return _render_revoke_sql((legacy,)) + render_postgresql_acl_sql(role_mapping)


def render_authority_rollback_acl_sql(
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> str:
    split_roles = tuple(
        role_mapping.physical(role) for role in SPLIT_RESEARCH_WRITER_IDENTITIES
    )
    return _render_revoke_sql(split_roles) + render_previous_authority_acl_sql(
        role_mapping, legacy_research_writer_role
    )


def render_authority_upgrade_plan(
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
) -> dict[str, object]:
    """Return offline, secret-free plan evidence; no database is consulted."""

    legacy = _require_legacy_role(legacy_research_writer_role)
    new_roles = tuple(
        role_mapping.physical(role) for role in SPLIT_RESEARCH_WRITER_IDENTITIES
    )
    acl = render_authority_upgrade_acl_sql(
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
    )
    rollback_acl = render_authority_rollback_acl_sql(
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
    )
    return {
        "contract": AUTHORITY_UPGRADE_KEY,
        "status": "READY",
        "previous_manifest_digest": PREVIOUS_CANONICAL_MANIFEST_DIGEST,
        "current_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "role_mapping_digest": role_mapping.mapping_digest,
        "legacy_role": legacy,
        "new_roles": list(new_roles),
        "research_tables": list(RESEARCH_AUTHORITY_TABLES),
        "table_count": len(CANONICAL_TABLE_NAMES),
        "upgrade_acl_digest": sha256(acl.encode("utf-8")).hexdigest(),
        "rollback_acl_digest": sha256(rollback_acl.encode("utf-8")).hexdigest(),
        "destructive_table_operations": [],
        "requires_zero_research_rows": True,
    }


def _lock_identity(connection: Connection) -> None:
    connection.execute(
        select(SCHEMA_METADATA_TABLE.c.metadata_key)
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .with_for_update()
    ).scalar_one()


def _write_event(
    connection: Connection,
    *,
    event_type: str,
    generation: int,
    actor_identity: str,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
    applied_at: datetime,
) -> tuple[str, str]:
    direction = "UPGRADE" if event_type == AUTHORITY_UPGRADE_EVENT else "ROLLBACK"
    evidence = {
        "contract": AUTHORITY_UPGRADE_KEY,
        "direction": direction,
        "generation": generation,
        "previous_manifest_digest": PREVIOUS_CANONICAL_MANIFEST_DIGEST,
        "current_manifest_digest": CANONICAL_MANIFEST_DIGEST,
        "role_mapping_digest": role_mapping.mapping_digest,
        "legacy_role": legacy_research_writer_role,
        "split_roles": [
            role_mapping.physical(role)
            for role in SPLIT_RESEARCH_WRITER_IDENTITIES
        ],
        "research_tables": list(RESEARCH_AUTHORITY_TABLES),
        "verified_research_row_count": 0,
        "actor_identity": actor_identity,
        "applied_at": applied_at.isoformat(),
        "destructive_table_operations": [],
    }
    request_digest = _digest({**evidence, "applied_at": None})
    aggregate_id = f"{AUTHORITY_UPGRADE_KEY}:{generation}"
    receipt_digest = _digest(
        {
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "request_digest": request_digest,
            "applied_at": applied_at.isoformat(),
        }
    )
    connection.execute(
        AUDIT_EVENTS_TABLE.insert().values(
            id=uuid4(),
            event_type=event_type,
            aggregate_type="canonical_authority_upgrade",
            aggregate_id=aggregate_id,
            actor_identity=actor_identity,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            evidence_json=evidence,
            created_at=applied_at,
        )
    )
    return request_digest, receipt_digest


def _create_missing_split_roles(
    connection: Connection, role_mapping: CanonicalRoleMapping
) -> None:
    roles = tuple(
        role_mapping.physical(role) for role in SPLIT_RESEARCH_WRITER_IDENTITIES
    )
    existing = {
        str(value)
        for value in connection.execute(
            text("SELECT rolname FROM pg_catalog.pg_roles WHERE rolname=ANY(:roles)"),
            {"roles": list(roles)},
        ).scalars()
    }
    for role in roles:
        if role not in existing:
            connection.exec_driver_sql(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            )


def apply_authority_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
    actor_identity: str,
    applied_at: datetime | None = None,
) -> AuthorityUpgradeResult:
    """Apply the reviewed split in the caller-owned PostgreSQL transaction."""

    actor = _require_actor(actor_identity)
    legacy = _require_legacy_role(legacy_research_writer_role)
    if connection.dialect.name != "postgresql":
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED", "authority upgrade requires PostgreSQL"
        )
    _lock_identity(connection)
    before = verify_authority_upgrade_state(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        require_no_research_rows=True,
    )
    if not before.accepted:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_PREFLIGHT", "; ".join(before.problems)
        )
    upgrades, rollbacks, history_problems = _event_history(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
    )
    if history_problems:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_HISTORY", "; ".join(history_problems)
        )
    if before.state == "CURRENT":
        return AuthorityUpgradeResult(
            status="NO_OP_ALREADY_CURRENT",
            state="CURRENT",
            generation=max(upgrades, 0),
            previous_manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST,
            current_manifest_digest=CANONICAL_MANIFEST_DIGEST,
            role_mapping_digest=role_mapping.mapping_digest,
            request_digest=None,
            receipt_digest=None,
            research_row_count=before.research_row_count or 0,
            legacy_role_retained=True,
        )
    if upgrades != rollbacks:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_HISTORY",
            "previous digest requires balanced upgrade/rollback receipts",
        )

    _create_missing_split_roles(connection, role_mapping)
    _execute_acl_sql(
        connection,
        render_authority_upgrade_acl_sql(
            role_mapping=role_mapping,
            legacy_research_writer_role=legacy,
        ),
    )
    updated = connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis",
            SCHEMA_METADATA_TABLE.c.manifest_digest
            == PREVIOUS_CANONICAL_MANIFEST_DIGEST,
        )
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    if updated.rowcount != 1:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_RACE", "manifest identity changed"
        )
    generation = upgrades + 1
    timestamp = _utc(applied_at or datetime.now(timezone.utc))
    request_digest, receipt_digest = _write_event(
        connection,
        event_type=AUTHORITY_UPGRADE_EVENT,
        generation=generation,
        actor_identity=actor,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        applied_at=timestamp,
    )
    after = verify_authority_upgrade_state(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        require_no_research_rows=True,
    )
    if not after.accepted or after.state != "CURRENT":
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_POSTVERIFY", "; ".join(after.problems)
        )
    return AuthorityUpgradeResult(
        status="UPGRADED",
        state=after.state,
        generation=generation,
        previous_manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST,
        current_manifest_digest=CANONICAL_MANIFEST_DIGEST,
        role_mapping_digest=role_mapping.mapping_digest,
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        research_row_count=after.research_row_count or 0,
        legacy_role_retained=True,
    )


def rollback_authority_upgrade(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    legacy_research_writer_role: str,
    actor_identity: str,
    applied_at: datetime | None = None,
) -> AuthorityUpgradeResult:
    """Restore the reviewed previous ACL while preserving all audit receipts."""

    actor = _require_actor(actor_identity)
    legacy = _require_legacy_role(legacy_research_writer_role)
    if connection.dialect.name != "postgresql":
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_POSTGRESQL_REQUIRED", "authority rollback requires PostgreSQL"
        )
    _lock_identity(connection)
    before = verify_authority_upgrade_state(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        require_no_research_rows=True,
    )
    if not before.accepted or before.state != "CURRENT":
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_ROLLBACK_PREFLIGHT", "; ".join(before.problems)
        )
    upgrades, rollbacks, history_problems = _event_history(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
    )
    if history_problems:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_HISTORY", "; ".join(history_problems)
        )
    if upgrades != rollbacks + 1:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_HISTORY",
            "rollback requires one unmatched upgrade receipt",
        )

    _execute_acl_sql(
        connection,
        render_authority_rollback_acl_sql(
            role_mapping=role_mapping,
            legacy_research_writer_role=legacy,
        ),
    )
    updated = connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis",
            SCHEMA_METADATA_TABLE.c.manifest_digest == CANONICAL_MANIFEST_DIGEST,
        )
        .values(manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST)
    )
    if updated.rowcount != 1:
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_UPGRADE_RACE", "manifest identity changed"
        )
    timestamp = _utc(applied_at or datetime.now(timezone.utc))
    request_digest, receipt_digest = _write_event(
        connection,
        event_type=AUTHORITY_ROLLBACK_EVENT,
        generation=upgrades,
        actor_identity=actor,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        applied_at=timestamp,
    )
    after = verify_authority_upgrade_state(
        connection,
        role_mapping=role_mapping,
        legacy_research_writer_role=legacy,
        require_no_research_rows=True,
    )
    if not after.accepted or after.state != "PREVIOUS_READY":
        raise CanonicalAuthorityUpgradeBlocked(
            "BLOCKED_AUTHORITY_ROLLBACK_POSTVERIFY", "; ".join(after.problems)
        )
    return AuthorityUpgradeResult(
        status="ROLLED_BACK",
        state=after.state,
        generation=upgrades,
        previous_manifest_digest=PREVIOUS_CANONICAL_MANIFEST_DIGEST,
        current_manifest_digest=CANONICAL_MANIFEST_DIGEST,
        role_mapping_digest=role_mapping.mapping_digest,
        request_digest=request_digest,
        receipt_digest=receipt_digest,
        research_row_count=after.research_row_count or 0,
        legacy_role_retained=True,
    )


__all__ = [
    "AUTHORITY_ROLLBACK_EVENT",
    "AUTHORITY_UPGRADE_EVENT",
    "AUTHORITY_UPGRADE_KEY",
    "LEGACY_RESEARCH_WRITER_IDENTITY",
    "PREVIOUS_CANONICAL_MANIFEST_DIGEST",
    "RESEARCH_AUTHORITY_TABLES",
    "SPLIT_RESEARCH_WRITER_IDENTITIES",
    "AuthorityUpgradeResult",
    "AuthorityUpgradeVerification",
    "CanonicalAuthorityUpgradeBlocked",
    "apply_authority_upgrade",
    "render_authority_rollback_acl_sql",
    "render_authority_upgrade_acl_sql",
    "render_authority_upgrade_plan",
    "render_previous_authority_acl_sql",
    "rollback_authority_upgrade",
    "verify_authority_upgrade_state",
]
