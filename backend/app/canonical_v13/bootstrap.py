"""PostgreSQL bootstrap plan and live fail-closed acceptance checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy import Connection, text

from app.canonical_v13.acceptance_signal_trigger_upgrade import (
    ACCEPTANCE_CONTROL_WRITER_READ_DELTA,
    CanonicalAcceptanceSignalTriggerUpgradeBlocked,
    verify_acceptance_signal_trigger_upgrade,
)
from app.canonical_v13.genesis import (
    GATE_GUARD_FUNCTION_NAMES,
    verify_canonical_genesis,
)
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_TABLE_NAMES,
    READER_IDENTITIES,
    READER_TABLE_ALLOWLIST,
    TABLE_MANIFEST_BY_NAME,
    WRITER_IDENTITIES,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.shadow_risk_acl_upgrade import (
    SHADOW_RISK_WRITER_READ_DELTA,
    CanonicalShadowRiskAclUpgradeBlocked,
    verify_shadow_risk_acl_upgrade,
)


LOCAL_ROLE_PREFIX: Final = "freqtrade_ai_v13_"
LOCAL_DATABASE_NAME: Final = "freqtrade_ai_v13"
LOCAL_SERVICE_PRINCIPALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "freqtrade_ai_v13_api_login": "canonical_api_reader",
        "freqtrade_ai_v13_control_login": "canonical_control_writer",
    }
)
LOCAL_RESEARCH_SERVICE_PRINCIPALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "freqtrade_ai_v13_validation_login": "canonical_validation_writer",
        "freqtrade_ai_v13_scoring_login": "canonical_scoring_writer",
        "freqtrade_ai_v13_qualification_login": "canonical_qualification_writer",
        "freqtrade_ai_v13_optimization_login": "canonical_optimization_writer",
    }
)
LOCAL_PHASE9_SERVICE_PRINCIPALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "freqtrade_ai_v13_approval_login": "canonical_approval_writer",
        "freqtrade_ai_v13_deployment_login": "canonical_deployment_writer",
        "freqtrade_ai_v13_signal_login": "canonical_signal_writer",
        "freqtrade_ai_v13_risk_login": "canonical_risk_writer",
        "freqtrade_ai_v13_order_login": "canonical_order_writer",
        "freqtrade_ai_v13_fill_login": "canonical_fill_writer",
        "freqtrade_ai_v13_ledger_login": "canonical_ledger_writer",
        "freqtrade_ai_v13_reconciliation_login": "canonical_reconciliation_writer",
    }
)
LOCAL_RUNTIME_SERVICE_PRINCIPALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "freqtrade_ai_v13_runtime_login": "canonical_runtime_reader",
    }
)


@dataclass(frozen=True)
class BootstrapVerification:
    accepted: bool
    problems: tuple[str, ...]
    table_count: int
    business_row_count: int | None
    capability_role_count: int
    explicit_acl_count: int


def local_role_mapping() -> CanonicalRoleMapping:
    return CanonicalRoleMapping.from_prefix(LOCAL_ROLE_PREFIX)


def local_legacy_research_writer_role() -> str:
    """Physical rollback anchor used only by the authority upgrade contract."""

    return LOCAL_ROLE_PREFIX + "research_writer"


def _expected_table_grants(
    role_mapping: CanonicalRoleMapping,
) -> set[tuple[str, str, str]]:
    grants: dict[tuple[str, str], set[str]] = defaultdict(set)
    for writer, table_names in WRITER_TABLE_ALLOWLIST.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            grants[(role_mapping.physical(writer), table_name)].update(
                TABLE_MANIFEST_BY_NAME[table_name].writer_privileges
            )
    for writer, table_names in WRITER_READ_ALLOWLIST.items():
        if writer == "canonical_schema_owner":
            continue
        for table_name in table_names:
            grants[(role_mapping.physical(writer), table_name)].add("SELECT")
    for reader, table_names in READER_TABLE_ALLOWLIST.items():
        for table_name in table_names:
            grants[(role_mapping.physical(reader), table_name)].add("SELECT")
    return {
        (role, table_name, privilege)
        for (role, table_name), privileges in grants.items()
        for privilege in privileges
    }


def _accepted_additive_table_grants(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> set[tuple[str, str, str]]:
    """Compose only independently verified additive ACL receipts."""

    accepted: set[tuple[str, str, str]] = set()
    try:
        acceptance_trigger = verify_acceptance_signal_trigger_upgrade(
            connection, role_mapping=role_mapping
        )
    except CanonicalAcceptanceSignalTriggerUpgradeBlocked:
        acceptance_trigger = None
    if acceptance_trigger is not None and acceptance_trigger.status == "ACCEPTED":
        control_writer = role_mapping.physical("canonical_control_writer")
        accepted.update(
            (control_writer, table_name, "SELECT")
            for table_name in ACCEPTANCE_CONTROL_WRITER_READ_DELTA
        )
    try:
        shadow_risk_acl = verify_shadow_risk_acl_upgrade(
            connection, role_mapping=role_mapping
        )
    except CanonicalShadowRiskAclUpgradeBlocked:
        shadow_risk_acl = None
    if shadow_risk_acl is not None and shadow_risk_acl.status == "ACCEPTED":
        risk_writer = role_mapping.physical("canonical_risk_writer")
        accepted.update(
            (risk_writer, table_name, "SELECT")
            for table_name in SHADOW_RISK_WRITER_READ_DELTA
        )
    return accepted


def verify_postgresql_bootstrap(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    require_zero_business_rows: bool = True,
    service_principals: Mapping[str, str] | None = None,
    require_owner_table_grants: bool = True,
) -> BootstrapVerification:
    """Verify genesis, physical capabilities, owners, and exact grants."""

    if connection.dialect.name != "postgresql":
        return BootstrapVerification(
            accepted=False,
            problems=("BLOCKED_POSTGRESQL_REQUIRED",),
            table_count=0,
            business_row_count=None,
            capability_role_count=0,
            explicit_acl_count=0,
        )
    genesis = verify_canonical_genesis(
        connection,
        require_zero_business_rows=require_zero_business_rows,
    )
    problems = list(genesis.problems)
    logical_roles = (*WRITER_IDENTITIES, *READER_IDENTITIES)
    physical_roles = tuple(role_mapping.physical(role) for role in logical_roles)
    role_rows = connection.execute(
        text(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                   rolinherit, rolreplication, rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = ANY(:roles)
            """
        ),
        {"roles": list(physical_roles)},
    ).all()
    observed_roles = {str(row[0]) for row in role_rows}
    missing_roles = sorted(set(physical_roles) - observed_roles)
    if missing_roles:
        problems.append(f"missing physical capability roles: {missing_roles!r}")
    for row in role_rows:
        if any(bool(value) for value in row[1:]):
            problems.append(f"capability role is not isolated NOLOGIN: {row[0]}")

    membership_rows = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
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
            {"roles": list(physical_roles)},
        )
    }
    expected_memberships = {
        (role_mapping.physical(logical), principal)
        for principal, logical in (service_principals or {}).items()
    }
    missing_memberships = expected_memberships - membership_rows
    extra_memberships = membership_rows - expected_memberships
    if missing_memberships:
        problems.append(f"missing service memberships count={len(missing_memberships)}")
    if extra_memberships:
        problems.append(f"extra role memberships count={len(extra_memberships)}")

    if service_principals:
        service_rows = connection.execute(
            text(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolinherit, rolreplication, rolbypassrls
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(:roles)
                """
            ),
            {"roles": list(service_principals)},
        ).all()
        observed_services = {str(row[0]) for row in service_rows}
        missing_services = sorted(set(service_principals) - observed_services)
        if missing_services:
            problems.append(f"missing service principals: {missing_services!r}")
        for row in service_rows:
            if (
                not bool(row[1])
                or not bool(row[5])
                or any(
                    bool(value) for value in (row[2], row[3], row[4], row[6], row[7])
                )
            ):
                problems.append(f"service principal privilege drift: {row[0]}")
        expected_connect_roles = {
            role_mapping.physical(logical) for logical in service_principals.values()
        }
        relevant_database_roles = set(service_principals) | expected_connect_roles
        connect_roles = {
            str(value)
            for value in connection.execute(
                text(
                    """
                    SELECT grantee.rolname
                    FROM pg_catalog.pg_database database
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(
                            database.datacl,
                            acldefault('d', database.datdba)
                        )
                    ) database_acl
                    JOIN pg_catalog.pg_roles grantee
                      ON grantee.oid = database_acl.grantee
                    WHERE database.datname = current_database()
                      AND grantee.rolname = ANY(:roles)
                      AND database_acl.privilege_type = 'CONNECT'
                    """
                ),
                {"roles": list(relevant_database_roles)},
            ).scalars()
        }
        missing_connect = expected_connect_roles - connect_roles
        extra_connect = connect_roles - expected_connect_roles
        if missing_connect:
            problems.append(
                f"missing service database CONNECT count={len(missing_connect)}"
            )
        if extra_connect:
            problems.append(
                f"unexpected service database CONNECT count={len(extra_connect)}"
            )

    owner = role_mapping.physical("canonical_schema_owner")
    owner_drift = connection.execute(
        text(
            """
            SELECT c.relname, pg_get_userbyid(c.relowner)
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relkind IN ('r','p','i','S')
              AND pg_get_userbyid(c.relowner) <> :owner
            """
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA, "owner": owner},
    ).all()
    if owner_drift:
        problems.append(f"relation owner drift count={len(owner_drift)}")
    function_owner_drift = connection.execute(
        text(
            """
            SELECT procedure.proname, pg_get_userbyid(procedure.proowner)
            FROM pg_catalog.pg_proc procedure
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
            WHERE namespace.nspname=:schema AND procedure.proname = ANY(:function_names)
              AND pg_get_userbyid(procedure.proowner) <> :owner
            """
        ),
        {
            "schema": CANONICAL_BUSINESS_SCHEMA,
            "function_names": list(GATE_GUARD_FUNCTION_NAMES),
            "owner": owner,
        },
    ).all()
    if function_owner_drift:
        problems.append(f"guard function owner drift count={len(function_owner_drift)}")
    function_grants = {
        (str(row[0]), "PUBLIC" if row[1] is None else str(row[1]), str(row[2]))
        for row in connection.execute(
            text(
                """
                SELECT procedure.proname, grantee.rolname, acl.privilege_type
                FROM pg_catalog.pg_proc procedure
                JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                ) acl
                LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
                WHERE namespace.nspname=:schema AND procedure.proname = ANY(:function_names)
                """
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "function_names": list(GATE_GUARD_FUNCTION_NAMES),
            },
        )
    }
    expected_function_grants = {
        (function_name, owner, "EXECUTE") for function_name in GATE_GUARD_FUNCTION_NAMES
    }
    if function_grants != expected_function_grants:
        problems.append("guard function ACL drift")
    schema_owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace "
            "WHERE nspname=:schema"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA},
    ).scalar_one_or_none()
    if schema_owner != owner:
        problems.append(f"schema owner expected={owner!r} observed={schema_owner!r}")
    if require_owner_table_grants:
        problems.extend(
            postgresql_owner_table_grant_problems(
                connection,
                role_mapping=role_mapping,
            )
        )

    actual_grants = {
        tuple(str(value) for value in row)
        for row in connection.execute(
            text(
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema=:schema AND grantee = ANY(:roles)
                  AND grantee <> :owner
                """
            ),
            {
                "schema": CANONICAL_BUSINESS_SCHEMA,
                "roles": list(physical_roles),
                "owner": owner,
            },
        )
    }
    expected_grants = _expected_table_grants(role_mapping)
    expected_grants.update(
        _accepted_additive_table_grants(
            connection,
            role_mapping=role_mapping,
        )
    )
    missing_grants = expected_grants - actual_grants
    extra_grants = actual_grants - expected_grants
    if missing_grants:
        problems.append(f"missing table grants count={len(missing_grants)}")
    if extra_grants:
        problems.append(f"extra table grants count={len(extra_grants)}")

    return BootstrapVerification(
        accepted=not problems,
        problems=tuple(problems),
        table_count=len(genesis.table_names),
        business_row_count=genesis.business_row_count,
        capability_role_count=len(role_rows),
        explicit_acl_count=len(actual_grants),
    )


def verify_postgresql_bootstrap_with_optional_service_principals(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    required_service_principals: Mapping[str, str],
    optional_service_principal_groups: Mapping[str, Mapping[str, str]],
    require_zero_business_rows: bool = True,
    require_owner_table_grants: bool = True,
) -> BootstrapVerification:
    """Compose only complete later-lifecycle LOGIN groups into verification."""

    optional_names = {
        principal
        for group in optional_service_principal_groups.values()
        for principal in group
    }
    observed_names = (
        {
            str(value)
            for value in connection.execute(
                text(
                    "SELECT rolname FROM pg_catalog.pg_roles "
                    "WHERE rolname = ANY(:roles)"
                ),
                {"roles": sorted(optional_names)},
            ).scalars()
        }
        if optional_names
        else set()
    )
    composed = dict(required_service_principals)
    composition_problems: list[str] = []
    for group_name, group in optional_service_principal_groups.items():
        group_names = set(group)
        observed_group = group_names & observed_names
        if observed_group == group_names:
            composed.update(group)
        elif observed_group:
            composition_problems.append(
                "partial optional service principal group "
                f"{group_name}: observed={len(observed_group)} "
                f"expected={len(group_names)}"
            )

    result = verify_postgresql_bootstrap(
        connection,
        role_mapping=role_mapping,
        require_zero_business_rows=require_zero_business_rows,
        service_principals=composed,
        require_owner_table_grants=require_owner_table_grants,
    )
    if not composition_problems:
        return result
    return BootstrapVerification(
        accepted=False,
        problems=(*result.problems, *composition_problems),
        table_count=result.table_count,
        business_row_count=result.business_row_count,
        capability_role_count=result.capability_role_count,
        explicit_acl_count=result.explicit_acl_count,
    )


def postgresql_owner_table_grant_problems(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
) -> tuple[str, ...]:
    expected = expected_postgresql_owner_table_grants(role_mapping)
    actual = postgresql_owner_table_grants(
        connection,
        role_mapping=role_mapping,
    )
    missing = expected - actual
    extra = actual - expected
    problems: list[str] = []
    if missing:
        problems.append(f"missing owner table grants count={len(missing)}")
    if extra:
        problems.append(f"extra owner table grants count={len(extra)}")
    return tuple(problems)


def expected_postgresql_owner_table_grants(
    role_mapping: CanonicalRoleMapping,
) -> frozenset[tuple[str, str, str]]:
    owner = role_mapping.physical("canonical_schema_owner")
    privilege_types = (
        "DELETE",
        "INSERT",
        "REFERENCES",
        "SELECT",
        "TRIGGER",
        "TRUNCATE",
        "UPDATE",
    )
    return frozenset(
        (owner, table_name, privilege)
        for table_name in CANONICAL_TABLE_NAMES
        for privilege in privilege_types
    )


def postgresql_owner_table_grants(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
) -> frozenset[tuple[str, str, str]]:
    owner = role_mapping.physical("canonical_schema_owner")
    return frozenset(
        tuple(str(value) for value in row)
        for row in connection.execute(
            text(
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema=:schema AND grantee=:owner
                """
            ),
            {"schema": CANONICAL_BUSINESS_SCHEMA, "owner": owner},
        )
    )


__all__ = [
    "LOCAL_DATABASE_NAME",
    "LOCAL_PHASE9_SERVICE_PRINCIPALS",
    "LOCAL_RUNTIME_SERVICE_PRINCIPALS",
    "LOCAL_ROLE_PREFIX",
    "LOCAL_RESEARCH_SERVICE_PRINCIPALS",
    "LOCAL_SERVICE_PRINCIPALS",
    "BootstrapVerification",
    "expected_postgresql_owner_table_grants",
    "local_role_mapping",
    "local_legacy_research_writer_role",
    "postgresql_owner_table_grant_problems",
    "postgresql_owner_table_grants",
    "verify_postgresql_bootstrap",
    "verify_postgresql_bootstrap_with_optional_service_principals",
]
