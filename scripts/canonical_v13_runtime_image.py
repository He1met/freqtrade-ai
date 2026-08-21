#!/usr/bin/env python3
"""Build, verify, migrate, and accept the canonical Phase 9 runtime OCI image."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.canonical_v13.bootstrap import local_role_mapping  # noqa: E402
from app.canonical_v13.runtime_image_authority import (  # noqa: E402
    CanonicalRuntimeImageBlocked,
    PodmanRuntimeImageInspector,
    accept_runtime_image,
    git_source_tree_digest,
    load_accepted_runtime_image,
    runtime_image_recipe_digest,
)
from app.canonical_v13.runtime_image_upgrade import (  # noqa: E402
    CanonicalRuntimeImageUpgradeBlocked,
    apply_runtime_image_upgrade,
    rollback_runtime_image_upgrade,
    verify_runtime_image_upgrade,
)


DATABASE_URL_ENV = "FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL"
PODMAN = "/opt/homebrew/bin/podman"
CONTAINERFILE = ROOT / "containers/canonical-v13-runtime/Containerfile"
SBOM = ROOT / "containers/canonical-v13-runtime/sbom.spdx.json"
BUILD_CONTEXT = ROOT / "containers/canonical-v13-runtime"


def _head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise CanonicalRuntimeImageBlocked("BLOCKED_RUNTIME_IMAGE_SOURCE_SHA", "git HEAD unavailable")
    return result.stdout.strip()


def _engine():
    url = os.environ.get(DATABASE_URL_ENV, "")
    if not url.startswith("postgresql+psycopg://"):
        raise CanonicalRuntimeImageBlocked("BLOCKED_RUNTIME_IMAGE_DATABASE", DATABASE_URL_ENV)
    return create_engine(url, pool_pre_ping=True)


def build_image() -> dict[str, object]:
    source_commit = _head()
    source_tree_digest = git_source_tree_digest(ROOT, source_commit=source_commit)
    recipe_digest = runtime_image_recipe_digest(ROOT)
    sbom_digest = sha256(SBOM.read_bytes()).hexdigest()
    tag = f"localhost/canonical-v13-runtime:build-{source_commit}"
    command = [
        PODMAN, "build", "--pull=never", "--network=none", "--no-cache",
        "--platform=linux/arm64", "--timestamp=0", "--file", str(CONTAINERFILE),
        "--build-arg", f"SOURCE_COMMIT={source_commit}",
        "--build-arg", f"SOURCE_TREE_DIGEST={source_tree_digest}",
        "--build-arg", f"BUILD_RECIPE_DIGEST={recipe_digest}",
        "--build-arg", f"SBOM_DIGEST={sbom_digest}", "--tag", tag, str(BUILD_CONTEXT),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise CanonicalRuntimeImageBlocked("BLOCKED_RUNTIME_IMAGE_BUILD", "network-none Podman build failed")
    image_id = subprocess.run(
        [PODMAN, "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True, text=True,
    )
    image_id_value = image_id.stdout.strip().removeprefix("sha256:")
    immutable_reference = f"sha256:{image_id_value}"
    if (
        image_id.returncode != 0
        or len(image_id_value) != 64
        or any(character not in "0123456789abcdef" for character in image_id_value)
    ):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_BUILD", "Podman did not return an immutable image id"
        )
    inspection = PodmanRuntimeImageInspector(PODMAN).inspect(immutable_reference)
    preflight = subprocess.run(
        [
            PODMAN,
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=0.5",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--tmpfs=/run/canonical-v13-runtime:rw,noexec,nosuid,nodev,size=4m",
            immutable_reference,
            "preflight",
        ],
        capture_output=True,
        text=True,
    )
    try:
        preflight_payload = json.loads(preflight.stdout)
    except json.JSONDecodeError as exc:
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_PREFLIGHT", "hardened image preflight returned invalid evidence"
        ) from exc
    if (
        preflight.returncode != 0
        or preflight_payload.get("status") != "READY"
        or preflight_payload.get("reason_code") != "RUNTIME_IMAGE_PREFLIGHT_ACCEPTED"
        or preflight_payload.get("capability", {}).get("order_submission") != "DISABLED"
        or preflight_payload.get("capability", {}).get("allow_real_funds") is not False
    ):
        raise CanonicalRuntimeImageBlocked(
            "BLOCKED_RUNTIME_IMAGE_PREFLIGHT", "hardened image preflight was not accepted"
        )
    return {
        "status": "BUILT",
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "build_recipe_digest": recipe_digest,
        "sbom_digest": sbom_digest,
        "immutable_reference": f"sha256:{inspection.image_config_digest}",
        "image_manifest_digest": inspection.image_manifest_digest,
        "image_config_digest": inspection.image_config_digest,
        "platform": inspection.platform,
        "architecture": inspection.architecture,
        "build_output_redacted": True,
    }


def accept_image(immutable_reference: str, actor: str) -> dict[str, object]:
    source_commit = _head()
    source_tree_digest = git_source_tree_digest(ROOT, source_commit=source_commit)
    recipe_digest = runtime_image_recipe_digest(ROOT)
    sbom_digest = sha256(SBOM.read_bytes()).hexdigest()
    engine = _engine()
    try:
        with engine.begin() as connection:
            accepted = accept_runtime_image(
                connection,
                inspector=PodmanRuntimeImageInspector(PODMAN),
                immutable_reference=immutable_reference,
                source_commit=source_commit,
                source_tree_digest=source_tree_digest,
                build_recipe_digest=recipe_digest,
                sbom_digest=sbom_digest,
                accepted_by=actor,
                accepted_at=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()
    return {**asdict(accepted), "status": "ACCEPTED"}


def schema(operation: str) -> dict[str, object]:
    engine = _engine()
    try:
        if operation == "verify":
            with engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    result = verify_runtime_image_upgrade(connection)
        else:
            with engine.begin() as connection:
                result = (
                    apply_runtime_image_upgrade(connection, role_mapping=local_role_mapping())
                    if operation == "apply"
                    else rollback_runtime_image_upgrade(connection, role_mapping=local_role_mapping())
                )
    finally:
        engine.dispose()
    return asdict(result)


def show(acceptance_id: UUID) -> dict[str, object]:
    engine = _engine()
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                accepted = load_accepted_runtime_image(connection, acceptance_id)
    finally:
        engine.dispose()
    return {**asdict(accepted), "status": "ACCEPTED"}


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "accept", "show", "schema-verify", "schema-apply", "schema-rollback"))
    parser.add_argument("--immutable-reference")
    parser.add_argument("--actor")
    parser.add_argument("--acceptance-id", type=UUID)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            payload = build_image()
        elif args.command == "accept":
            if not args.immutable_reference or not args.actor:
                raise CanonicalRuntimeImageBlocked("BLOCKED_RUNTIME_IMAGE_INPUT", "immutable reference and actor required")
            payload = accept_image(args.immutable_reference, args.actor)
        elif args.command == "show":
            if args.acceptance_id is None:
                raise CanonicalRuntimeImageBlocked("BLOCKED_RUNTIME_IMAGE_INPUT", "acceptance id required")
            payload = show(args.acceptance_id)
        else:
            payload = schema(args.command.removeprefix("schema-"))
    except (CanonicalRuntimeImageBlocked, CanonicalRuntimeImageUpgradeBlocked) as exc:
        payload = {"status": "BLOCKED", "reason": str(exc)}
    print(json.dumps(_json_safe(payload), ensure_ascii=True, sort_keys=True))
    return 0 if payload["status"] in {"BUILT", "ACCEPTED", "UPGRADED", "ROLLED_BACK", "PREVIOUS_READY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
