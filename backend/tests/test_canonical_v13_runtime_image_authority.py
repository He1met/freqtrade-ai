from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import CanonicalBase, RUNTIME_IMAGE_ACCEPTANCES_TABLE
from app.canonical_v13.runtime_image_authority import (
    CanonicalRuntimeImageBlocked,
    RUNTIME_IMAGE_BASE_DIGEST,
    RUNTIME_IMAGE_TITLE,
    RuntimeImageInspection,
    accept_runtime_image,
    canonical_release_digest,
    load_accepted_runtime_image,
    runtime_image_recipe_digest,
    verify_accepted_runtime_image,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "a" * 40
SOURCE_TREE_DIGEST = "b" * 64
RECIPE_DIGEST = runtime_image_recipe_digest(ROOT)
SBOM_DIGEST = sha256(
    (ROOT / "containers/canonical-v13-runtime/sbom.spdx.json").read_bytes()
).hexdigest()
IMAGE_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


def _labels() -> dict[str, str]:
    return {
        "org.opencontainers.image.title": RUNTIME_IMAGE_TITLE,
        "org.opencontainers.image.revision": SOURCE_COMMIT,
        "org.opencontainers.image.base.digest": f"sha256:{RUNTIME_IMAGE_BASE_DIGEST}",
        "io.freqtrade-ai.source-tree-digest": SOURCE_TREE_DIGEST,
        "io.freqtrade-ai.build-recipe-digest": RECIPE_DIGEST,
        "io.freqtrade-ai.sbom-digest": SBOM_DIGEST,
        "io.freqtrade-ai.demo-only": "true",
        "io.freqtrade-ai.allow-real-funds": "false",
    }


class Inspector:
    def __init__(self, inspection: RuntimeImageInspection | None = None) -> None:
        self.inspection = inspection or RuntimeImageInspection(
            image_manifest_digest=IMAGE_DIGEST,
            image_config_digest=CONFIG_DIGEST,
            platform="linux",
            architecture="arm64",
            labels=_labels(),
            entrypoint=("/opt/freqtrade-ai/bin/canonical-v13-runtime",),
            user="65532:65532",
            stop_signal="SIGTERM",
            builder_identity="podman/buildah-test",
        )
        self.calls: list[str] = []

    def inspect(self, immutable_reference: str) -> RuntimeImageInspection:
        self.calls.append(immutable_reference)
        return self.inspection


@pytest.fixture()
def connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as raw:
        effective = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        CanonicalBase.metadata.create_all(effective)
        yield effective
    engine.dispose()


def _accept(connection, inspector: Inspector | None = None):
    return accept_runtime_image(
        connection,
        inspector=inspector or Inspector(),
        immutable_reference=f"sha256:{IMAGE_DIGEST}",
        source_commit=SOURCE_COMMIT,
        source_tree_digest=SOURCE_TREE_DIGEST,
        build_recipe_digest=RECIPE_DIGEST,
        sbom_digest=SBOM_DIGEST,
        accepted_by="canonical-runtime-image-operator",
        accepted_at=NOW,
    )


def test_server_inspection_acceptance_is_immutable_and_exact_replay_is_noop(connection) -> None:
    inspector = Inspector()
    first = _accept(connection, inspector)
    second = _accept(connection, inspector)

    assert first == second == load_accepted_runtime_image(connection, first.acceptance_id)
    assert first.release_digest == canonical_release_digest(SOURCE_COMMIT)
    assert first.image_manifest_digest == IMAGE_DIGEST
    assert first.image_config_digest == CONFIG_DIGEST
    assert first.demo_only is True
    assert first.allow_real_funds is False
    assert inspector.calls == [f"sha256:{IMAGE_DIGEST}"] * 2
    assert connection.execute(
        select(func.count()).select_from(RUNTIME_IMAGE_ACCEPTANCES_TABLE)
    ).scalar_one() == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"architecture": "s390x"}, "BLOCKED_RUNTIME_IMAGE_SECURITY_PROFILE"),
        ({"user": "0:0"}, "BLOCKED_RUNTIME_IMAGE_SECURITY_PROFILE"),
        ({"stop_signal": "SIGKILL"}, "BLOCKED_RUNTIME_IMAGE_SECURITY_PROFILE"),
        ({"entrypoint": ("/bin/sh",)}, "BLOCKED_RUNTIME_IMAGE_SECURITY_PROFILE"),
        ({"labels": {**_labels(), "org.opencontainers.image.revision": "e" * 40}}, "BLOCKED_RUNTIME_IMAGE_PROVENANCE"),
        ({"labels": {**_labels(), "org.opencontainers.image.title": "canonical research executor"}}, "BLOCKED_RESEARCH_EXECUTOR_IMAGE_FORBIDDEN"),
    ],
)
def test_acceptance_rejects_security_provenance_and_research_drift(connection, change, reason) -> None:
    inspection = replace(Inspector().inspection, **change)
    with pytest.raises(CanonicalRuntimeImageBlocked, match=reason):
        _accept(connection, Inspector(inspection))


def test_mutable_reference_is_rejected_before_inspector_or_database(connection) -> None:
    inspector = Inspector()
    with pytest.raises(
        CanonicalRuntimeImageBlocked, match="BLOCKED_RUNTIME_IMAGE_MUTABLE_REFERENCE"
    ):
        accept_runtime_image(
            connection,
            inspector=inspector,
            immutable_reference="localhost/canonical-v13-runtime:latest",
            source_commit=SOURCE_COMMIT,
            source_tree_digest=SOURCE_TREE_DIGEST,
            build_recipe_digest=RECIPE_DIGEST,
            sbom_digest=SBOM_DIGEST,
            accepted_by="canonical-runtime-image-operator",
            accepted_at=NOW,
        )
    assert inspector.calls == []


def test_persisted_receipt_tampering_fails_closed(connection) -> None:
    accepted = _accept(connection)
    with pytest.raises(
        CanonicalRuntimeImageBlocked, match="BLOCKED_RUNTIME_IMAGE_ACCEPTANCE_DRIFT"
    ):
        verify_accepted_runtime_image(replace(accepted, receipt_digest="f" * 64))


def test_runtime_container_recipe_is_distinct_nonroot_and_secret_free() -> None:
    containerfile = (
        ROOT / "containers/canonical-v13-runtime/Containerfile"
    ).read_text(encoding="utf-8")
    worker = (
        ROOT / "containers/canonical-v13-runtime/canonical_v13_runtime.py"
    ).read_text(encoding="utf-8")
    assert f"@sha256:{RUNTIME_IMAGE_BASE_DIGEST}" in containerfile
    assert "USER 65532:65532" in containerfile
    assert "COPY --chmod=0555 canonical_v13_runtime.py" in containerfile
    assert "COPY ." not in containerfile
    assert 'ENTRYPOINT ["/opt/freqtrade-ai/bin/canonical-v13-runtime"]' in containerfile
    assert "STOPSIGNAL SIGTERM" in containerfile
    assert "canonical-v13-research" not in containerfile
    assert "OKX_DEMO_API_SECRET" not in containerfile + worker
    assert "allow_real_funds\": False" in worker
