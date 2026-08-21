from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from app.canonical_v13.phase9_schema_upgrade import (
    PHASE9_DATABASE_CONNECT_DELTA,
    PHASE9_EXTENSION_TABLE_NAMES,
    PHASE9_UNIQUE_CONSTRAINTS,
    CanonicalPhase9SchemaUpgradeBlocked,
    apply_phase9_schema_upgrade,
    rollback_phase9_schema_upgrade,
    verify_phase9_schema_upgrade,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping
from app.canonical_v13.runtime_image_upgrade import (
    CanonicalRuntimeImageUpgradeBlocked,
    apply_runtime_image_upgrade,
    rollback_runtime_image_upgrade,
)
from app.canonical_v13.runtime_image_authority import (
    RUNTIME_IMAGE_BASE_DIGEST,
    RUNTIME_IMAGE_TITLE,
    RuntimeImageInspection,
    accept_runtime_image,
)
from app.canonical_v13.models import RUNTIME_IMAGE_ACCEPTANCES_TABLE
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

DATABASE_URL = os.environ.get("CANONICAL_V13_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CANONICAL_V13_POSTGRES_URL is required for the isolated contract",
)


def test_phase9_postgresql_upgrade_rollback_and_exact_replay() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            initial = verify_phase9_schema_upgrade(connection)
            assert initial.status == "ACCEPTED"
            assert initial.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert initial.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert set(initial.affected_row_counts.values()) == {0}

            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            for role in PHASE9_DATABASE_CONNECT_DELTA:
                connection.exec_driver_sql(
                    f'GRANT CONNECT ON DATABASE "{database_name}" '
                    f"TO {mapping.physical(role)}"
                )

            image_rolled_back = rollback_runtime_image_upgrade(
                connection, role_mapping=mapping
            )
            assert image_rolled_back.status == "ROLLED_BACK"
            assert rollback_runtime_image_upgrade(
                connection, role_mapping=mapping
            ).status == "PREVIOUS_READY"

            rolled_back = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rolled_back.status == "ROLLED_BACK"
            assert rolled_back.present_constraints == ()
            assert rolled_back.present_extension_tables == ()
            assert rolled_back.destructive_row_operations == 0

            rollback_replay = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert rollback_replay.status == "PREVIOUS_READY"
            assert rollback_replay.repeat_noop is True

            upgraded = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert upgraded.status == "UPGRADED"
            assert upgraded.present_constraints == tuple(
                sorted(PHASE9_UNIQUE_CONSTRAINTS)
            )
            assert upgraded.present_extension_tables == tuple(
                sorted(PHASE9_EXTENSION_TABLE_NAMES)
            )
            assert upgraded.destructive_row_operations == 0

            repeated = apply_phase9_schema_upgrade(
                connection,
                role_mapping=mapping,
            )
            assert repeated.status == "ACCEPTED"
            assert repeated.repeat_noop is True
            assert (
                repeated.receipt_digest
                == verify_phase9_schema_upgrade(connection).receipt_digest
            )
            image_upgraded = apply_runtime_image_upgrade(
                connection, role_mapping=mapping
            )
            assert image_upgraded.status == "UPGRADED"
            assert apply_runtime_image_upgrade(
                connection, role_mapping=mapping
            ).status == "ACCEPTED"
    finally:
        engine.dispose()


def test_phase9_previous_acl_and_connect_drift_fail_closed_atomically() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    extra_role = mapping.physical("canonical_approval_writer")
    try:
        with engine.begin() as connection:
            rollback_runtime_image_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                "GRANT DELETE ON TABLE strategy_platform_v13.signals "
                f"TO {extra_role}"
            )

        with pytest.raises(
            CanonicalPhase9SchemaUpgradeBlocked,
            match="BLOCKED_PREVIOUS_ACL_DRIFT",
        ):
            with engine.begin() as connection:
                rollback_phase9_schema_upgrade(connection, role_mapping=mapping)

        with engine.connect() as connection:
            current = verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            assert current.status == "ACCEPTED"

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "REVOKE DELETE ON TABLE strategy_platform_v13.signals "
                f"FROM {extra_role}"
            )
            previous = rollback_phase9_schema_upgrade(
                connection, role_mapping=mapping
            )
            assert previous.status == "ROLLED_BACK"
            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            connection.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{database_name}" TO {extra_role}'
            )
            with pytest.raises(
                CanonicalPhase9SchemaUpgradeBlocked,
                match="BLOCKED_PREVIOUS_DATABASE_CONNECT_DRIFT",
            ):
                verify_phase9_schema_upgrade(connection, role_mapping=mapping)
            connection.exec_driver_sql(
                f'REVOKE CONNECT ON DATABASE "{database_name}" FROM {extra_role}'
            )
            assert (
                verify_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "PREVIOUS_READY"
            )
            assert (
                apply_phase9_schema_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
            assert (
                apply_runtime_image_upgrade(connection, role_mapping=mapping).status
                == "UPGRADED"
            )
    finally:
        engine.dispose()


def test_runtime_image_acceptance_concurrency_append_only_and_rollback_cleanup() -> None:
    assert DATABASE_URL is not None
    mapping = CanonicalRoleMapping.from_prefix(
        os.environ.get("CANONICAL_V13_ROLE_PREFIX", "freqtrade_ai_v13_ci_")
    )
    engine = create_engine(DATABASE_URL)
    source_commit = "a" * 40
    source_tree_digest = "b" * 64
    recipe_digest = "c" * 64
    sbom_digest = "d" * 64
    image_manifest_digest = "e" * 64
    image_config_digest = "f" * 64

    class Inspector:
        def inspect(self, _reference: str) -> RuntimeImageInspection:
            return RuntimeImageInspection(
                image_manifest_digest=image_manifest_digest,
                image_config_digest=image_config_digest,
                platform="linux",
                architecture="arm64",
                labels={
                    "org.opencontainers.image.title": RUNTIME_IMAGE_TITLE,
                    "org.opencontainers.image.revision": source_commit,
                    "org.opencontainers.image.base.digest": f"sha256:{RUNTIME_IMAGE_BASE_DIGEST}",
                    "io.freqtrade-ai.source-tree-digest": source_tree_digest,
                    "io.freqtrade-ai.build-recipe-digest": recipe_digest,
                    "io.freqtrade-ai.sbom-digest": sbom_digest,
                    "io.freqtrade-ai.demo-only": "true",
                    "io.freqtrade-ai.allow-real-funds": "false",
                },
                entrypoint=("/opt/freqtrade-ai/bin/canonical-v13-runtime",),
                user="65532:65532",
                stop_signal="SIGTERM",
                builder_identity="isolated-postgresql-test",
            )

    def accept_once():
        with engine.begin() as connection:
            return accept_runtime_image(
                connection,
                inspector=Inspector(),
                immutable_reference=f"sha256:{image_config_digest}",
                source_commit=source_commit,
                source_tree_digest=source_tree_digest,
                build_recipe_digest=recipe_digest,
                sbom_digest=sbom_digest,
                accepted_by="canonical-runtime-image-postgresql-test",
                accepted_at=datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc),
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = tuple(executor.map(lambda _index: accept_once(), range(2)))
        assert accepted[0] == accepted[1]
        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(
                    RUNTIME_IMAGE_ACCEPTANCES_TABLE.update().values(
                        accepted_by="tampered"
                    )
                )
        with pytest.raises(
            CanonicalRuntimeImageUpgradeBlocked,
            match="BLOCKED_RUNTIME_IMAGE_ACCEPTANCES_NONZERO",
        ):
            with engine.begin() as connection:
                rollback_runtime_image_upgrade(connection, role_mapping=mapping)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE strategy_platform_v13.runtime_image_acceptances "
                "DISABLE TRIGGER runtime_image_acceptances_append_only"
            )
            connection.execute(RUNTIME_IMAGE_ACCEPTANCES_TABLE.delete())
            connection.exec_driver_sql(
                "ALTER TABLE strategy_platform_v13.runtime_image_acceptances "
                "ENABLE TRIGGER runtime_image_acceptances_append_only"
            )
            assert rollback_runtime_image_upgrade(
                connection, role_mapping=mapping
            ).status == "ROLLED_BACK"
            assert apply_runtime_image_upgrade(
                connection, role_mapping=mapping
            ).status == "UPGRADED"
        engine.dispose()
