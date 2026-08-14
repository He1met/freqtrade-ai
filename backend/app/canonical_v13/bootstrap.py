"""PostgreSQL bootstrap plan and live fail-closed acceptance checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from sqlalchemy import Connection, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    READER_IDENTITIES,
    READER_TABLE_ALLOWLIST,
    TABLE_MANIFEST_BY_NAME,
    WRITER_IDENTITIES,
    WRITER_READ_ALLOWLIST,
    WRITER_TABLE_ALLOWLIST,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


LOCAL_ROLE_PREFIX: Final = "freqtrade_ai_v13_"
LOCAL_DATABASE_NAME: Final = "freqtrade_ai_v13"
LOCAL_SERVICE_PRINCIPALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "freqtrade_ai_v13_api_login": "canonical_api_reader",
        "freqtrade_ai_v13_control_login": "canonical_control_writer",
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


def verify_postgresql_bootstrap(
    connection: Connection,
    *,
    role_mapping: CanonicalRoleMapping,
    require_zero_business_rows: bool = True,
    service_principals: Mapping[str, str] | None = None,
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
        problems.append(
            f"missing service memberships count={len(missing_memberships)}"
        )
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
            if not bool(row[1]) or not bool(row[5]) or any(
                bool(value) for value in (row[2], row[3], row[4], row[6], row[7])
            ):
                problems.append(
                    f"service principal privilege drift: {row[0]}"
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
    schema_owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace "
            "WHERE nspname=:schema"
        ),
        {"schema": CANONICAL_BUSINESS_SCHEMA},
    ).scalar_one_or_none()
    if schema_owner != owner:
        problems.append(
            f"schema owner expected={owner!r} observed={schema_owner!r}"
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


__all__ = [
    "LOCAL_DATABASE_NAME",
    "LOCAL_ROLE_PREFIX",
    "LOCAL_SERVICE_PRINCIPALS",
    "BootstrapVerification",
    "local_role_mapping",
    "verify_postgresql_bootstrap",
]
