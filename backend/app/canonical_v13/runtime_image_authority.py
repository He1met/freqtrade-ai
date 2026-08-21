"""Immutable, server-derived authority for the canonical Phase 9 runtime image."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from typing import Mapping, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.canonical_v13.models import RUNTIME_IMAGE_ACCEPTANCES_TABLE


RUNTIME_IMAGE_CONTRACT = "canonical-v13-runtime-image-acceptance-v1"
RUNTIME_IMAGE_TITLE = "Freqtrade Ai canonical V1.3 long-lived Demo runtime"
RUNTIME_IMAGE_BASE_DIGEST = (
    "c730f60992863baafc8f13469291731e4b5e0ada82d8a9b449dcf5db24d00f76"
)
RUNTIME_IMAGE_PLATFORM = "linux"
RUNTIME_IMAGE_ENTRYPOINT = ("/opt/freqtrade-ai/bin/canonical-v13-runtime",)
RUNTIME_IMAGE_SECURITY_PROFILE = {
    "allow_real_funds": False,
    "cap_drop": "ALL",
    "credential_layers": False,
    "demo_only": True,
    "network_policy": "DEMO_EXCHANGE_ONLY",
    "no_new_privileges": True,
    "read_only_rootfs": True,
    "run_as": "65532:65532",
    "stop_signal": "SIGTERM",
    "writable_paths": ["/run/canonical-v13-runtime", "/tmp"],
}
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class CanonicalRuntimeImageBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class RuntimeImageInspection:
    image_manifest_digest: str
    image_config_digest: str
    platform: str
    architecture: str
    labels: Mapping[str, str]
    entrypoint: tuple[str, ...]
    user: str
    stop_signal: str
    builder_identity: str


@dataclass(frozen=True)
class AcceptedRuntimeImage:
    acceptance_id: UUID
    source_commit: str
    release_digest: str
    source_tree_digest: str
    build_recipe_digest: str
    base_image_digest: str
    platform: str
    architecture: str
    image_manifest_digest: str
    image_config_digest: str
    entrypoint_digest: str
    security_profile_digest: str
    sbom_digest: str
    provenance_digest: str
    builder_identity: str
    request_digest: str
    receipt_digest: str
    accepted_by: str
    accepted_at: datetime
    demo_only: bool
    allow_real_funds: bool


class RuntimeImageInspector(Protocol):
    def inspect(self, immutable_reference: str) -> RuntimeImageInspection: ...


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _require_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_DIGEST", f"{field} is not lowercase sha256"
        )
    return value


def canonical_release_digest(source_commit: str) -> str:
    if _HEX_40.fullmatch(source_commit) is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_SOURCE_SHA", "source commit must be exact git SHA"
        )
    return sha256(f"canonical-v13-release:{source_commit}".encode("ascii")).hexdigest()


def runtime_image_recipe_digest(repository_root: Path) -> str:
    paths = (
        "containers/canonical-v13-runtime/Containerfile",
        "containers/canonical-v13-runtime/canonical_v13_runtime.py",
        "containers/canonical-v13-runtime/sbom.spdx.json",
    )
    entries = []
    for relative in paths:
        path = repository_root / relative
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_RECIPE", f"missing reviewed source {relative}"
            ) from exc
        entries.append({"path": relative, "sha256": sha256(content).hexdigest()})
    return _digest({"contract": "canonical-v13-runtime-image-recipe-v1", "files": entries})


def git_source_tree_digest(repository_root: Path, *, source_commit: str) -> str:
    if _HEX_40.fullmatch(source_commit) is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_SOURCE_SHA", "source commit must be exact git SHA"
        )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, capture_output=True, check=False
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repository_root, capture_output=True, check=False
    )
    if (
        head.returncode != 0
        or status.returncode != 0
        or head.stdout.decode("ascii", "replace").strip() != source_commit
        or status.stdout
    ):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_SOURCE_DRIFT", "build checkout must be clean exact source commit"
        )
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source_commit],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_SOURCE_DRIFT", "cannot derive exact source archive"
        )
    return sha256(archive.stdout).hexdigest()


class PodmanRuntimeImageInspector:
    """Inspect a local immutable Podman object without reading environment secrets."""

    def __init__(self, executable: str = "/opt/homebrew/bin/podman") -> None:
        self._executable = executable

    def inspect(self, immutable_reference: str) -> RuntimeImageInspection:
        if not immutable_reference.startswith("sha256:") or _HEX_64.fullmatch(
            immutable_reference.removeprefix("sha256:")
        ) is None:
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_MUTABLE_REFERENCE",
                "only an immutable sha256 image reference may be inspected",
            )
        result = subprocess.run(
            [self._executable, "image", "inspect", immutable_reference],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_INSPECTION", "immutable image is unavailable"
            )
        try:
            values = json.loads(result.stdout)
            row = values[0]
            config = row["Config"]
            labels = config.get("Labels") or {}
            manifest = str(row["Digest"]).removeprefix("sha256:")
            config_digest = str(row["Id"]).removeprefix("sha256:")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_INSPECTION", "Podman inspection shape drifted"
            ) from exc
        with tempfile.TemporaryDirectory(prefix="canonical-v13-runtime-image-") as directory:
            archive_path = Path(directory) / "image.oci.tar"
            saved = subprocess.run(
                [
                    self._executable,
                    "image",
                    "save",
                    "--format",
                    "oci-archive",
                    "--output",
                    str(archive_path),
                    immutable_reference,
                ],
                capture_output=True,
                check=False,
            )
            if saved.returncode != 0:
                raise CanonicalRuntimeImageBlocked(
                    "BLOCKED_RUNTIME_IMAGE_INSPECTION",
                    "cannot derive OCI manifest from immutable local image",
                )
            try:
                with tarfile.open(archive_path, "r") as archive:
                    index_member = archive.extractfile("index.json")
                    if index_member is None:
                        raise KeyError("index.json")
                    index = json.load(index_member)
                    descriptor = index["manifests"][0]
                    archive_manifest = str(descriptor["digest"]).removeprefix("sha256:")
                    manifest_member = archive.extractfile(
                        f"blobs/sha256/{archive_manifest}"
                    )
                    if manifest_member is None:
                        raise KeyError("manifest blob")
                    manifest_json = json.load(manifest_member)
                    archived_config_digest = str(
                        manifest_json["config"]["digest"]
                    ).removeprefix("sha256:")
            except (KeyError, IndexError, tarfile.TarError, json.JSONDecodeError) as exc:
                raise CanonicalRuntimeImageBlocked(
                    "BLOCKED_RUNTIME_IMAGE_INSPECTION",
                    "OCI archive provenance shape drifted",
                ) from exc
        if (
            _HEX_64.fullmatch(manifest) is None
            or _HEX_64.fullmatch(archive_manifest) is None
            or _HEX_64.fullmatch(config_digest) is None
            or archived_config_digest != config_digest
        ):
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_INSPECTION",
                "OCI manifest and local config identity do not match",
            )
        version_result = subprocess.run(
            [self._executable, "version", "--format", "json"],
            capture_output=True,
            check=False,
        )
        try:
            version_payload = json.loads(version_result.stdout)
            client_version = str(version_payload["Client"]["Version"])
            server_version = str(version_payload["Server"]["Version"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CanonicalRuntimeImageBlocked(
                "BLOCKED_RUNTIME_IMAGE_INSPECTION",
                "Podman builder version is unavailable",
            ) from exc
        builder_identity = (
            f"podman-client/{client_version};podman-server/{server_version}"
        )
        if row.get("BuildahVersion"):
            builder_identity += f";buildah/{row['BuildahVersion']}"
        entrypoint = tuple(config.get("Entrypoint") or ())
        return RuntimeImageInspection(
            image_manifest_digest=manifest,
            image_config_digest=config_digest,
            platform=str(row.get("Os") or ""),
            architecture=str(row.get("Architecture") or ""),
            labels={str(key): str(value) for key, value in labels.items()},
            entrypoint=entrypoint,
            user=str(config.get("User") or ""),
            stop_signal=str(config.get("StopSignal") or row.get("StopSignal") or ""),
            builder_identity=builder_identity,
        )


def _expected_label_values(
    *, source_commit: str, source_tree_digest: str, build_recipe_digest: str, sbom_digest: str
) -> dict[str, str]:
    return {
        "org.opencontainers.image.title": RUNTIME_IMAGE_TITLE,
        "org.opencontainers.image.revision": source_commit,
        "org.opencontainers.image.base.digest": f"sha256:{RUNTIME_IMAGE_BASE_DIGEST}",
        "io.freqtrade-ai.source-tree-digest": source_tree_digest,
        "io.freqtrade-ai.build-recipe-digest": build_recipe_digest,
        "io.freqtrade-ai.sbom-digest": sbom_digest,
        "io.freqtrade-ai.demo-only": "true",
        "io.freqtrade-ai.allow-real-funds": "false",
    }


def _row_to_authority(row: Mapping[str, object]) -> AcceptedRuntimeImage:
    return AcceptedRuntimeImage(
        acceptance_id=row["id"],  # type: ignore[arg-type]
        source_commit=str(row["source_commit"]),
        release_digest=str(row["release_digest"]),
        source_tree_digest=str(row["source_tree_digest"]),
        build_recipe_digest=str(row["build_recipe_digest"]),
        base_image_digest=str(row["base_image_digest"]),
        platform=str(row["platform"]),
        architecture=str(row["architecture"]),
        image_manifest_digest=str(row["image_manifest_digest"]),
        image_config_digest=str(row["image_config_digest"]),
        entrypoint_digest=str(row["entrypoint_digest"]),
        security_profile_digest=str(row["security_profile_digest"]),
        sbom_digest=str(row["sbom_digest"]),
        provenance_digest=str(row["provenance_digest"]),
        builder_identity=str(row["builder_identity"]),
        request_digest=str(row["request_digest"]),
        receipt_digest=str(row["receipt_digest"]),
        accepted_by=str(row["accepted_by"]),
        accepted_at=row["accepted_at"],  # type: ignore[arg-type]
        demo_only=row["demo_only"] is True,
        allow_real_funds=row["allow_real_funds"] is True,
    )


def accept_runtime_image(
    connection: Connection,
    *,
    inspector: RuntimeImageInspector,
    immutable_reference: str,
    source_commit: str,
    source_tree_digest: str,
    build_recipe_digest: str,
    sbom_digest: str,
    accepted_by: str,
    accepted_at: datetime,
) -> AcceptedRuntimeImage:
    """Inspect and accept an image; callers never submit manifest/config facts."""

    if not immutable_reference.startswith("sha256:") or _HEX_64.fullmatch(
        immutable_reference.removeprefix("sha256:")
    ) is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_MUTABLE_REFERENCE",
            "only an immutable sha256 image reference may be accepted",
        )
    for field, value in (
        ("source_tree_digest", source_tree_digest),
        ("build_recipe_digest", build_recipe_digest),
        ("sbom_digest", sbom_digest),
    ):
        _require_digest(value, field=field)
    if not accepted_by or accepted_by.strip() != accepted_by or accepted_at.tzinfo is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_ACCEPTOR", "explicit actor and timezone are required"
        )
    inspection = inspector.inspect(immutable_reference)
    for field, value in (
        ("image_manifest_digest", inspection.image_manifest_digest),
        ("image_config_digest", inspection.image_config_digest),
    ):
        _require_digest(value, field=field)
    expected_labels = _expected_label_values(
        source_commit=source_commit,
        source_tree_digest=source_tree_digest,
        build_recipe_digest=build_recipe_digest,
        sbom_digest=sbom_digest,
    )
    if "research" in inspection.labels.get("org.opencontainers.image.title", "").lower():
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RESEARCH_EXECUTOR_IMAGE_FORBIDDEN", "research executor is not runtime authority"
        )
    if any(inspection.labels.get(key) != value for key, value in expected_labels.items()):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_PROVENANCE", "reviewed image labels do not match exact source"
        )
    if (
        inspection.platform != RUNTIME_IMAGE_PLATFORM
        or inspection.architecture not in {"arm64", "amd64"}
        or inspection.entrypoint != RUNTIME_IMAGE_ENTRYPOINT
        or inspection.user != "65532:65532"
        or inspection.stop_signal != "SIGTERM"
    ):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_SECURITY_PROFILE", "platform, entrypoint, user, or stop signal drifted"
        )
    accepted = accepted_at.astimezone(timezone.utc)
    release_digest = canonical_release_digest(source_commit)
    entrypoint_digest = _digest(list(RUNTIME_IMAGE_ENTRYPOINT))
    security_profile_digest = _digest(RUNTIME_IMAGE_SECURITY_PROFILE)
    provenance = {
        "contract": "canonical-v13-runtime-image-provenance-v1",
        "source_commit": source_commit,
        "release_digest": release_digest,
        "source_tree_digest": source_tree_digest,
        "build_recipe_digest": build_recipe_digest,
        "base_image_digest": RUNTIME_IMAGE_BASE_DIGEST,
        "platform": inspection.platform,
        "architecture": inspection.architecture,
        "image_manifest_digest": inspection.image_manifest_digest,
        "image_config_digest": inspection.image_config_digest,
        "entrypoint_digest": entrypoint_digest,
        "security_profile_digest": security_profile_digest,
        "sbom_digest": sbom_digest,
        "builder_identity": inspection.builder_identity,
        "demo_only": True,
        "allow_real_funds": False,
    }
    provenance_digest = _digest(provenance)
    request = {"contract": RUNTIME_IMAGE_CONTRACT, **provenance, "accepted_by": accepted_by}
    request_digest = _digest(request)
    acceptance_id = uuid4()
    receipt_digest = _digest(
        {"contract": RUNTIME_IMAGE_CONTRACT, "acceptance_id": str(acceptance_id), "request_digest": request_digest}
    )
    values = {
        "id": acceptance_id,
        **{key: value for key, value in provenance.items() if key not in {"contract"}},
        "provenance_digest": provenance_digest,
        "provenance_json": provenance,
        "request_digest": request_digest,
        "receipt_digest": receipt_digest,
        "accepted_by": accepted_by,
        "accepted_at": accepted,
    }
    existing = connection.execute(
        select(RUNTIME_IMAGE_ACCEPTANCES_TABLE).where(
            RUNTIME_IMAGE_ACCEPTANCES_TABLE.c.request_digest == request_digest
        )
    ).mappings().one_or_none()
    if existing is not None:
        return verify_accepted_runtime_image(_row_to_authority(existing))
    if connection.dialect.name == "postgresql":
        statement = postgresql_insert(RUNTIME_IMAGE_ACCEPTANCES_TABLE).values(**values)
    elif connection.dialect.name == "sqlite":
        statement = sqlite_insert(RUNTIME_IMAGE_ACCEPTANCES_TABLE).values(**values)
    else:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_DATABASE", "PostgreSQL or isolated SQLite is required"
        )
    connection.execute(statement.on_conflict_do_nothing(index_elements=["request_digest"]))
    row = connection.execute(
        select(RUNTIME_IMAGE_ACCEPTANCES_TABLE).where(
            RUNTIME_IMAGE_ACCEPTANCES_TABLE.c.request_digest == request_digest
        )
    ).mappings().one()
    return verify_accepted_runtime_image(_row_to_authority(row))


def verify_accepted_runtime_image(authority: AcceptedRuntimeImage) -> AcceptedRuntimeImage:
    provenance = {
        "contract": "canonical-v13-runtime-image-provenance-v1",
        "source_commit": authority.source_commit,
        "release_digest": authority.release_digest,
        "source_tree_digest": authority.source_tree_digest,
        "build_recipe_digest": authority.build_recipe_digest,
        "base_image_digest": authority.base_image_digest,
        "platform": authority.platform,
        "architecture": authority.architecture,
        "image_manifest_digest": authority.image_manifest_digest,
        "image_config_digest": authority.image_config_digest,
        "entrypoint_digest": authority.entrypoint_digest,
        "security_profile_digest": authority.security_profile_digest,
        "sbom_digest": authority.sbom_digest,
        "builder_identity": authority.builder_identity,
        "demo_only": authority.demo_only,
        "allow_real_funds": authority.allow_real_funds,
    }
    request = {"contract": RUNTIME_IMAGE_CONTRACT, **provenance, "accepted_by": authority.accepted_by}
    if (
        authority.release_digest != canonical_release_digest(authority.source_commit)
        or authority.base_image_digest != RUNTIME_IMAGE_BASE_DIGEST
        or authority.platform != RUNTIME_IMAGE_PLATFORM
        or authority.architecture not in {"arm64", "amd64"}
        or authority.entrypoint_digest != _digest(list(RUNTIME_IMAGE_ENTRYPOINT))
        or authority.security_profile_digest != _digest(RUNTIME_IMAGE_SECURITY_PROFILE)
        or authority.provenance_digest != _digest(provenance)
        or authority.request_digest != _digest(request)
        or authority.receipt_digest
        != _digest({"contract": RUNTIME_IMAGE_CONTRACT, "acceptance_id": str(authority.acceptance_id), "request_digest": authority.request_digest})
        or not authority.demo_only
        or authority.allow_real_funds
    ):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_ACCEPTANCE_DRIFT", "persisted runtime image authority drifted"
        )
    return authority


def load_accepted_runtime_image(connection: Connection, acceptance_id: UUID) -> AcceptedRuntimeImage:
    row = connection.execute(
        select(RUNTIME_IMAGE_ACCEPTANCES_TABLE).where(
            RUNTIME_IMAGE_ACCEPTANCES_TABLE.c.id == acceptance_id
        )
    ).mappings().one_or_none()
    if row is None:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_NOT_ACCEPTED", str(acceptance_id)
        )
    return verify_accepted_runtime_image(_row_to_authority(row))


__all__ = [
    "AcceptedRuntimeImage",
    "CanonicalRuntimeImageBlocked",
    "PodmanRuntimeImageInspector",
    "RUNTIME_IMAGE_BASE_DIGEST",
    "RUNTIME_IMAGE_CONTRACT",
    "RUNTIME_IMAGE_ENTRYPOINT",
    "RUNTIME_IMAGE_SECURITY_PROFILE",
    "RUNTIME_IMAGE_TITLE",
    "RuntimeImageInspection",
    "RuntimeImageInspector",
    "accept_runtime_image",
    "canonical_release_digest",
    "git_source_tree_digest",
    "load_accepted_runtime_image",
    "runtime_image_recipe_digest",
    "verify_accepted_runtime_image",
]
