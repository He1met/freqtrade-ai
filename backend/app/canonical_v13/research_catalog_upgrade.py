"""One-table additive upgrade for the minimal offline research catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import Connection, inspect, select, text

from app.canonical_v13.genesis import verify_canonical_genesis
from app.canonical_v13.manifest import (
    CANONICAL_BUSINESS_SCHEMA,
    CANONICAL_MANIFEST_DIGEST,
    READER_IDENTITIES,
    WRITER_IDENTITIES,
)
from app.canonical_v13.models import (
    RESEARCH_RUN_CATALOG_TABLE,
    SCHEMA_METADATA_TABLE,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST: Final = (
    "636055388dcb182c5e29cb329997c4f34a1a39438e7ebfaaddd8ed2987840eb8"
)
RESEARCH_CATALOG_UPGRADE_CONTRACT: Final = (
    "canonical-v13-minimal-research-catalog-upgrade-v1"
)


class ResearchCatalogUpgradeBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchCatalogUpgradeResult:
    contract: str
    status: str
    table_present: bool
    manifest_digest: str
    repeat_noop: bool


def _manifest_digest(connection: Connection) -> str:
    value = connection.execute(
        select(SCHEMA_METADATA_TABLE.c.manifest_digest).where(
            SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis"
        )
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise ResearchCatalogUpgradeBlocked("BLOCKED_RESEARCH_CATALOG_METADATA")
    return value


def _table_present(connection: Connection) -> bool:
    return inspect(connection).has_table(
        RESEARCH_RUN_CATALOG_TABLE.name, schema=CANONICAL_BUSINESS_SCHEMA
    )


def verify_research_catalog_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ResearchCatalogUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise ResearchCatalogUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    present = _table_present(connection)
    manifest_digest = _manifest_digest(connection)
    if not present and manifest_digest == PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST:
        predecessor = verify_canonical_genesis(
            connection,
            accepted_manifest_digests=(PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST,),
            allowed_missing_tables=(RESEARCH_RUN_CATALOG_TABLE.name,),
        )
        if predecessor.accepted:
            return ResearchCatalogUpgradeResult(
                contract=RESEARCH_CATALOG_UPGRADE_CONTRACT,
                status="PREVIOUS_READY",
                table_present=False,
                manifest_digest=manifest_digest,
                repeat_noop=True,
            )
    if present and manifest_digest == CANONICAL_MANIFEST_DIGEST:
        verification = verify_canonical_genesis(connection)
        owner = role_mapping.physical("canonical_schema_owner")
        control = role_mapping.physical("canonical_control_writer")
        api_reader = role_mapping.physical("canonical_api_reader")
        research_reader = role_mapping.physical("canonical_research_reader")
        acl_ok = bool(
            connection.execute(
                text(
                    "SELECT has_table_privilege(:owner, :table, 'SELECT,INSERT,UPDATE,DELETE'), "
                    "has_table_privilege(:control, :table, 'SELECT,INSERT'), "
                    "has_table_privilege(:api_reader, :table, 'SELECT'), "
                    "has_table_privilege(:research_reader, :table, 'SELECT')"
                ),
                {
                    "owner": owner,
                    "control": control,
                    "api_reader": api_reader,
                    "research_reader": research_reader,
                    "table": (
                        f"{CANONICAL_BUSINESS_SCHEMA}."
                        f"{RESEARCH_RUN_CATALOG_TABLE.name}"
                    ),
                },
            ).one()
            == (True, True, True, True)
        )
        if verification.accepted and acl_ok:
            return ResearchCatalogUpgradeResult(
                contract=RESEARCH_CATALOG_UPGRADE_CONTRACT,
                status="ACCEPTED",
                table_present=True,
                manifest_digest=manifest_digest,
                repeat_noop=True,
            )
    raise ResearchCatalogUpgradeBlocked("BLOCKED_PARTIAL_RESEARCH_CATALOG_UPGRADE")


def apply_research_catalog_upgrade(
    connection: Connection, *, role_mapping: CanonicalRoleMapping
) -> ResearchCatalogUpgradeResult:
    if connection.dialect.name != "postgresql":
        raise ResearchCatalogUpgradeBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 1_308_202_608_270_001}
    )
    before = verify_research_catalog_upgrade(connection, role_mapping=role_mapping)
    if before.status == "ACCEPTED":
        return before

    RESEARCH_RUN_CATALOG_TABLE.create(connection, checkfirst=False)
    qualified = (
        f"{CANONICAL_BUSINESS_SCHEMA}.{RESEARCH_RUN_CATALOG_TABLE.name}"
    )
    owner = role_mapping.physical("canonical_schema_owner")
    connection.execute(text(f"ALTER TABLE {qualified} OWNER TO {owner}"))
    connection.execute(text(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM PUBLIC"))
    for role in (*WRITER_IDENTITIES, *READER_IDENTITIES):
        connection.execute(
            text(
                f"REVOKE ALL PRIVILEGES ON TABLE {qualified} "
                f"FROM {role_mapping.physical(role)}"
            )
        )
    connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            f"ON TABLE {qualified} TO {owner}"
        )
    )
    connection.execute(
        text(
            f"GRANT SELECT, INSERT ON TABLE {qualified} TO "
            f"{role_mapping.physical('canonical_control_writer')}"
        )
    )
    connection.execute(
        text(
            f"GRANT SELECT ON TABLE {qualified} TO "
            f"{role_mapping.physical('canonical_api_reader')}"
        )
    )
    connection.execute(
        text(
            f"GRANT SELECT ON TABLE {qualified} TO "
            f"{role_mapping.physical('canonical_research_reader')}"
        )
    )
    connection.execute(
        SCHEMA_METADATA_TABLE.update()
        .where(SCHEMA_METADATA_TABLE.c.metadata_key == "canonical-v13-genesis")
        .values(manifest_digest=CANONICAL_MANIFEST_DIGEST)
    )
    verified = verify_research_catalog_upgrade(
        connection, role_mapping=role_mapping
    )
    return ResearchCatalogUpgradeResult(
        contract=verified.contract,
        status="UPGRADED",
        table_present=True,
        manifest_digest=verified.manifest_digest,
        repeat_noop=False,
    )


__all__ = [
    "PREVIOUS_RESEARCH_CATALOG_MANIFEST_DIGEST",
    "RESEARCH_CATALOG_UPGRADE_CONTRACT",
    "ResearchCatalogUpgradeBlocked",
    "ResearchCatalogUpgradeResult",
    "apply_research_catalog_upgrade",
    "verify_research_catalog_upgrade",
]
