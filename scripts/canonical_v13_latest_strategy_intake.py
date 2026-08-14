#!/usr/bin/env python3
"""Plan or apply one latest source artifact per top-level strategy class."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.intake import CanonicalIntakeBlocked  # noqa: E402
from app.canonical_v13.latest_intake_manifest import (  # noqa: E402
    LatestStrategyManifest,
    build_latest_strategy_manifest,
)


DEFAULT_API_ORIGIN = "http://127.0.0.1:8011"
CALLER_IDENTITY = "canonical-v13-latest-filesystem-intake"


class LatestIntakeCLIBlocked(RuntimeError):
    """Stable fail-closed adapter error."""

    def __init__(self, code: str, *, evidence_preserved: bool = False) -> None:
        self.code = code
        self.evidence_preserved = evidence_preserved
        super().__init__(code)


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _request(
    api_origin: str, path: str, *, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = Request(
        api_origin.rstrip("/") + path,
        data=(
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        ),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        exc.read()
        raise LatestIntakeCLIBlocked(f"BLOCKED_CANONICAL_API_HTTP_{exc.code}") from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise LatestIntakeCLIBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(payload, dict):
        raise LatestIntakeCLIBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return payload


def _base_evidence(manifest: LatestStrategyManifest, *, mode: str) -> dict[str, Any]:
    return {
        **manifest.evidence(),
        "mode": mode,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "caller_identity": CALLER_IDENTITY,
        "status": "PLANNED" if mode == "plan" else "APPLYING",
        "results": [],
    }


def apply_manifest(
    manifest: LatestStrategyManifest,
    *,
    api_origin: str,
    evidence_output: Path,
    expected_archive_digest: str,
) -> dict[str, Any]:
    if manifest.archive_snapshot_digest != expected_archive_digest:
        raise LatestIntakeCLIBlocked("BLOCKED_ARCHIVE_SNAPSHOT_DRIFT")
    health = _request(api_origin, "/healthz")
    if (
        health.get("status") != "HEALTHY"
        or health.get("trading_capability") != "TRADING_DISABLED"
    ):
        raise LatestIntakeCLIBlocked("BLOCKED_CANONICAL_API_NOT_NO_TRADE")
    evidence = _base_evidence(manifest, mode="apply")
    _write_evidence(evidence_output, evidence)
    for entry in manifest.entries:
        try:
            response = _request(
                api_origin,
                "/api/canonical-v13/submissions",
                body=entry.api_command(caller_identity=CALLER_IDENTITY),
            )
        except LatestIntakeCLIBlocked as exc:
            evidence["status"] = "BLOCKED"
            evidence["results"].append(
                {
                    "strategy_class": entry.strategy_class,
                    "selected_path": entry.selected_path,
                    "selected_run": entry.selected_run,
                    "selected_code_digest": entry.selected_code_digest,
                    "status": "BLOCKED",
                    "reason_code": exc.code,
                }
            )
            _write_evidence(evidence_output, evidence)
            raise LatestIntakeCLIBlocked(
                exc.code, evidence_preserved=True
            ) from exc
        required_strings = (
            "submission_id",
            "artifact_id",
            "strategy_id",
            "strategy_version_id",
            "intake_receipt_id",
            "request_digest",
            "artifact_digest",
            "receipt_digest",
        )
        if (
            response.get("intake_status") != "INTAKE_ACCEPTED"
            or response.get("catalog_status") != "DRAFT"
            or response.get("validation_status") != "UNVALIDATED"
            or response.get("qualification_status") != "NOT_EVALUATED"
            or response.get("execution_authorized") is not False
            or not isinstance(response.get("idempotent_replay"), bool)
            or any(
                not isinstance(response.get(field), str) or not response.get(field)
                for field in required_strings
            )
            or response.get("artifact_digest") != entry.selected_code_digest
            or any(
                len(str(response.get(field))) != 64
                for field in ("request_digest", "artifact_digest", "receipt_digest")
            )
        ):
            evidence["status"] = "BLOCKED"
            evidence["results"].append(
                {
                    "strategy_class": entry.strategy_class,
                    "selected_path": entry.selected_path,
                    "selected_run": entry.selected_run,
                    "selected_code_digest": entry.selected_code_digest,
                    "status": "BLOCKED",
                    "reason_code": "BLOCKED_UNEXPECTED_INTAKE_RESPONSE",
                }
            )
            _write_evidence(evidence_output, evidence)
            raise LatestIntakeCLIBlocked(
                "BLOCKED_UNEXPECTED_INTAKE_RESPONSE", evidence_preserved=True
            )
        evidence["results"].append(
            {
                "strategy_class": entry.strategy_class,
                "selected_path": entry.selected_path,
                "selected_run": entry.selected_run,
                "selected_code_digest": entry.selected_code_digest,
                "submission_id": response.get("submission_id"),
                "artifact_id": response.get("artifact_id"),
                "strategy_id": response.get("strategy_id"),
                "strategy_version_id": response.get("strategy_version_id"),
                "intake_receipt_id": response.get("intake_receipt_id"),
                "receipt_digest": response.get("receipt_digest"),
                "request_digest": response.get("request_digest"),
                "artifact_digest": response.get("artifact_digest"),
                "idempotent_replay": response.get("idempotent_replay"),
                "status": "INTAKE_ACCEPTED",
            }
        )
        _write_evidence(evidence_output, evidence)
    evidence["status"] = "INTAKE_ACCEPTED"
    evidence["accepted_count"] = len(evidence["results"])
    _write_evidence(evidence_output, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"), nargs="?", default="plan")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--api-origin", default=DEFAULT_API_ORIGIN)
    parser.add_argument("--expected-archive-digest")
    args = parser.parse_args(argv)
    try:
        manifest = build_latest_strategy_manifest(args.source_root)
        if args.command == "apply":
            if args.expected_archive_digest is None:
                raise LatestIntakeCLIBlocked("BLOCKED_EXPECTED_ARCHIVE_DIGEST_UNSET")
            result = apply_manifest(
                manifest,
                api_origin=args.api_origin,
                evidence_output=args.evidence_output,
                expected_archive_digest=args.expected_archive_digest,
            )
        else:
            result = _base_evidence(manifest, mode="plan")
            _write_evidence(args.evidence_output, result)
    except (CanonicalIntakeBlocked, LatestIntakeCLIBlocked) as exc:
        result = {
            "status": "BLOCKED",
            "reason_code": getattr(exc, "code", str(exc).split(":", 1)[0]),
            "detail": getattr(exc, "detail", str(exc)),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "legacy_database_access": "NONE",
            "execution_side_effects": 0,
        }
        if not getattr(exc, "evidence_preserved", False):
            _write_evidence(args.evidence_output, result)
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
