#!/usr/bin/env python3
"""Canonical V1.3 audited no-trade research control and one-shot worker CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.canonical_v13.dto import ResearchAuthorizationConsumptionReceiptDTO
from app.canonical_v13.bootstrap import LOCAL_SERVICE_PRINCIPALS, local_role_mapping
from app.canonical_v13.freqtrade_production import (
    BoundedSubprocessSandboxRunner,
    FreqtradeProductionLookaheadAdapter,
    FreqtradeProductionResearchAdapter,
    PRODUCTION_LOOKAHEAD_ACTIVATION,
    ProductionResearchLimits,
    RemotePodmanVolumeSandboxRunner,
    execute_production_static_lookahead_gate,
    materialize_production_lookahead_inputs,
    materialize_production_research_inputs,
)
from app.canonical_v13.production import READER_DATABASE_URL_ENV
from app.canonical_v13.research_authorization import ResearchAuthorizationConsumption
from app.canonical_v13.research_orchestration import execute_production_research_chain
from app.canonical_v13.research_persistence import (
    QUALIFICATION_DATABASE_URL_ENV,
    SCORING_DATABASE_URL_ENV,
    VALIDATION_DATABASE_URL_ENV,
    research_service_principal,
)
from app.canonical_v13.research_validation import ResearchLineage


API_BASE_ENV = "FREQTRADE_AI_CANONICAL_V13_API_BASE_URL"
ACTIVATION_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_EXECUTION_ENABLED"
LOOKAHEAD_ACTIVATION_ENV = "FREQTRADE_AI_CANONICAL_V13_LOOKAHEAD_EXECUTION_ENABLED"
OCI_RUNTIME_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_OCI_RUNTIME"
IMAGE_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_IMAGE"
MARKET_ROOT_ENV = "FREQTRADE_AI_CANONICAL_V13_MARKET_ARTIFACT_ROOT"
WORKSPACE_ROOT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_WORKSPACE_ROOT"
CPU_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_CPU_LIMIT"
MEMORY_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_MEMORY_MB"
TIMEOUT_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_TIMEOUT_SECONDS"
OUTPUT_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_OUTPUT_BYTES"
PIDS_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_PIDS_LIMIT"
TMPFS_LIMIT_ENV = "FREQTRADE_AI_CANONICAL_V13_RESEARCH_TMPFS_MB"
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PINNED_IMAGE = re.compile(r"^.+@sha256:([0-9a-f]{64})$")


class ResearchCLIBlocked(RuntimeError):
    pass


@contextmanager
def _gate_writer_lock(environment: dict[str, str]):
    root = Path(_required(environment, WORKSPACE_ROOT_ENV))
    try:
        info = root.stat()
    except OSError as exc:
        raise ResearchCLIBlocked("BLOCKED_GATE_OWNER_LOCK_ROOT") from exc
    if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
        raise ResearchCLIBlocked("BLOCKED_GATE_OWNER_LOCK_ROOT")
    path = root / ".canonical-v13-gate-writer.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(descriptor, "r+")
    try:
        if os.fstat(handle.fileno()).st_uid != os.getuid() or os.fstat(handle.fileno()).st_mode & 0o777 != 0o600:
            raise ResearchCLIBlocked("BLOCKED_GATE_OWNER_LOCK_FILE")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResearchCLIBlocked("BLOCKED_GATE_WRITER_ALREADY_ACTIVE") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _json_file(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_PATH")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_INVALID") from exc
    if not isinstance(payload, dict):
        raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_INVALID")
    return payload


def _verify_gate_release_identity(environment: dict[str, str], payload: dict[str, object]) -> None:
    release_commit = payload.get("release_commit")
    image_digest = payload.get("executor_image_digest")
    worker_source_digest = payload.get("worker_source_digest")
    image_match = _PINNED_IMAGE.fullmatch(_required(environment, IMAGE_ENV))
    if (
        not isinstance(release_commit, str)
        or _RELEASE_COMMIT.fullmatch(release_commit) is None
        or not isinstance(image_digest, str)
        or _HEX_DIGEST.fullmatch(image_digest) is None
        or not isinstance(worker_source_digest, str)
        or _HEX_DIGEST.fullmatch(worker_source_digest) is None
        or image_match is None
        or image_match.group(1) != image_digest
    ):
        raise ResearchCLIBlocked("BLOCKED_GATE_RELEASE_IDENTITY")
    repository = Path(__file__).resolve().parents[2]
    worker = repository / "containers/canonical-v13-research/canonical_v13_research_worker.py"
    if not worker.is_file() or worker.is_symlink() or worker.stat().st_size > 1_048_576:
        raise ResearchCLIBlocked("BLOCKED_GATE_WORKER_SOURCE")
    if sha256(worker.read_bytes()).hexdigest() != worker_source_digest:
        raise ResearchCLIBlocked("BLOCKED_GATE_WORKER_SOURCE_DIGEST")
    git = Path("/usr/bin/git")
    if not git.is_file() or not os.access(git, os.X_OK):
        raise ResearchCLIBlocked("BLOCKED_GATE_RELEASE_GIT")
    try:
        head = subprocess.run(
            (str(git), "-C", str(repository), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        dirty = subprocess.run(
            (str(git), "-C", str(repository), "status", "--porcelain=v1"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin"},
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResearchCLIBlocked("BLOCKED_GATE_RELEASE_GIT") from exc
    if head != release_commit or dirty:
        raise ResearchCLIBlocked("BLOCKED_GATE_RELEASE_CHECKOUT")


def _api_base(environment: dict[str, str]) -> str:
    raw = environment.get(API_BASE_ENV, "")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ResearchCLIBlocked("BLOCKED_CANONICAL_API_BASE_URL")
    return raw.rstrip("/")


def _request(
    environment: dict[str, str],
    *,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload, sort_keys=True).encode()
    request = Request(
        f"{_api_base(environment)}/api/canonical-v13{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            error_payload = {"error": {"code": "BLOCKED_API_REQUEST"}}
        if not isinstance(error_payload, dict):
            error_payload = {"error": {"code": "BLOCKED_API_REQUEST"}}
        return {**error_payload, "status": "BLOCKED"}
    except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCLIBlocked("BLOCKED_CANONICAL_API_UNAVAILABLE") from exc
    if not isinstance(result, dict):
        raise ResearchCLIBlocked("BLOCKED_CANONICAL_API_RESPONSE")
    return result


def _required(environment: dict[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise ResearchCLIBlocked(f"BLOCKED_ENVIRONMENT_UNSET:{name}")
    return value


def _positive_int(environment: dict[str, str], name: str) -> int:
    raw = _required(environment, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResearchCLIBlocked(f"BLOCKED_ENVIRONMENT_INVALID:{name}") from exc
    if value <= 0:
        raise ResearchCLIBlocked(f"BLOCKED_ENVIRONMENT_INVALID:{name}")
    return value


def _consumption(payload: object) -> ResearchAuthorizationConsumption:
    command = ResearchAuthorizationConsumptionReceiptDTO.model_validate(payload)
    lineage = ResearchLineage(**command.lineage.model_dump())
    return ResearchAuthorizationConsumption(
        authorization_id=command.authorization_id,
        consumption_id=command.consumption_id,
        attempt_id=command.attempt_id,
        lineage=lineage,
        validation_plan_id=command.validation_plan_id,
        validation_plan_digest=command.validation_plan_digest,
        actor_identity=command.actor_identity,
        authorization_receipt_digest=command.authorization_receipt_digest,
        request_digest=command.request_digest,
        receipt_digest=command.receipt_digest,
        consumed_at=command.consumed_at,
        environment_class=command.environment_class,
    )


def _worker_execute(
    environment: dict[str, str], payload: dict[str, object]
) -> dict[str, object]:
    required_fields = {
        "validation_attempt_id",
        "expected_plan_digest",
        "authorization_consumption",
        "scorer_identity",
        "qualifier_identity",
    }
    if set(payload) != required_fields:
        raise ResearchCLIBlocked("BLOCKED_WORKER_COMMAND_FIELDS")
    urls = {
        "reader": make_url(_required(environment, READER_DATABASE_URL_ENV)),
        "validation": make_url(_required(environment, VALIDATION_DATABASE_URL_ENV)),
        "scoring": make_url(_required(environment, SCORING_DATABASE_URL_ENV)),
        "qualification": make_url(
            _required(environment, QUALIFICATION_DATABASE_URL_ENV)
        ),
    }
    if any(url.drivername != "postgresql+psycopg" for url in urls.values()):
        raise ResearchCLIBlocked("BLOCKED_POSTGRESQL_REQUIRED")
    mapping = local_role_mapping()
    expected_reader = next(
        principal
        for principal, capability in LOCAL_SERVICE_PRINCIPALS.items()
        if capability == "canonical_api_reader"
    )
    expected_identities = {
        "reader": expected_reader,
        "validation": research_service_principal(
            mapping, "canonical_validation_writer"
        ),
        "scoring": research_service_principal(mapping, "canonical_scoring_writer"),
        "qualification": research_service_principal(
            mapping, "canonical_qualification_writer"
        ),
    }
    if any(
        urls[capability].username != principal
        for capability, principal in expected_identities.items()
    ):
        raise ResearchCLIBlocked("BLOCKED_RESEARCH_ROLE_IDENTITY")
    locators = {
        (url.host, url.port, url.database, tuple(sorted(url.normalized_query.items())))
        for url in urls.values()
    }
    if len(locators) != 1 or len({url.username for url in urls.values()}) != 4:
        raise ResearchCLIBlocked("BLOCKED_RESEARCH_ROLE_OR_DATABASE_SEPARATION")
    engines = {
        capability: create_engine(url, pool_pre_ping=True)
        for capability, url in urls.items()
    }
    market_root = Path(_required(environment, MARKET_ROOT_ENV))
    workspace_root = Path(_required(environment, WORKSPACE_ROOT_ENV))

    @contextmanager
    def factory(capability: str):
        with engines[capability].connect() as connection:
            yield connection

    @contextmanager
    def inputs(running_attempt):
        materializer = None
        with engines["reader"].connect() as connection:
            materializer = materialize_production_research_inputs(
                connection,
                running_attempt=running_attempt,
                market_artifact_root=market_root,
                workspace_root=workspace_root,
            )
            materialized = materializer.__enter__()
        try:
            yield materialized
        finally:
            materializer.__exit__(None, None, None)

    limits = ProductionResearchLimits(
        cpu_count=_required(environment, CPU_LIMIT_ENV),
        memory_mb=_positive_int(environment, MEMORY_LIMIT_ENV),
        timeout_seconds=_positive_int(environment, TIMEOUT_LIMIT_ENV),
        max_output_bytes=_positive_int(environment, OUTPUT_LIMIT_ENV),
        pids_limit=_positive_int(environment, PIDS_LIMIT_ENV),
        tmpfs_mb=_positive_int(environment, TMPFS_LIMIT_ENV),
    )
    try:
        executor = FreqtradeProductionResearchAdapter(
            activation=_required(environment, ACTIVATION_ENV),
            runtime_path=Path(_required(environment, OCI_RUNTIME_ENV)),
            image_reference=_required(environment, IMAGE_ENV),
            limits=limits,
            input_factory=inputs,
            runner=BoundedSubprocessSandboxRunner(),
        )
        result = execute_production_research_chain(
            audit_connection_factory=lambda: factory("reader"),
            validation_connection_factory=lambda: factory("validation"),
            scoring_connection_factory=lambda: factory("scoring"),
            qualification_connection_factory=lambda: factory("qualification"),
            validation_attempt_id=UUID(str(payload["validation_attempt_id"])),
            expected_plan_digest=str(payload["expected_plan_digest"]),
            authorization_consumption=_consumption(
                payload["authorization_consumption"]
            ),
            executor=executor,
            scorer_identity=str(payload["scorer_identity"]),
            qualifier_identity=str(payload["qualifier_identity"]),
        )
        return {"status": "ACCEPTED", "receipt": asdict(result)}
    finally:
        for engine in engines.values():
            engine.dispose()


def _gate_execute(
    environment: dict[str, str], payload: dict[str, object]
) -> dict[str, object]:
    if set(payload) != {"lineage", "idempotency_key", "release_commit", "executor_image_digest", "worker_source_digest"} or not isinstance(payload["lineage"], dict):
        raise ResearchCLIBlocked("BLOCKED_GATE_COMMAND_FIELDS")
    _verify_gate_release_identity(environment, payload)
    lineage = ResearchLineage(
        **{
            key: UUID(str(value)) if key.endswith("_id") else str(value)
            for key, value in payload["lineage"].items()
        }
    )
    reader_url = make_url(_required(environment, READER_DATABASE_URL_ENV))
    expected_reader = next(
        principal
        for principal, capability in LOCAL_SERVICE_PRINCIPALS.items()
        if capability == "canonical_api_reader"
    )
    if (
        reader_url.drivername != "postgresql+psycopg"
        or reader_url.username != expected_reader
        or reader_url.database != "freqtrade_ai_v13"
    ):
        raise ResearchCLIBlocked("BLOCKED_RESEARCH_ROLE_IDENTITY")
    engine = create_engine(reader_url, pool_pre_ping=True)
    market_root = Path(_required(environment, MARKET_ROOT_ENV))
    workspace_root = Path(_required(environment, WORKSPACE_ROOT_ENV))

    @contextmanager
    def inputs(requested_lineage: ResearchLineage):
        with engine.connect() as connection:
            with materialize_production_lookahead_inputs(
                connection,
                lineage=requested_lineage,
                market_artifact_root=market_root,
                workspace_root=workspace_root,
            ) as materialized:
                yield materialized

    limits = ProductionResearchLimits(
        cpu_count=_required(environment, CPU_LIMIT_ENV),
        memory_mb=_positive_int(environment, MEMORY_LIMIT_ENV),
        timeout_seconds=_positive_int(environment, TIMEOUT_LIMIT_ENV),
        max_output_bytes=_positive_int(environment, OUTPUT_LIMIT_ENV),
        pids_limit=_positive_int(environment, PIDS_LIMIT_ENV),
        tmpfs_mb=_positive_int(environment, TMPFS_LIMIT_ENV),
    )
    try:
        attempt = _request(
            environment,
            method="POST",
            path="/research/gates/attempts",
            payload={
                "lineage": payload["lineage"],
                "idempotency_key": payload["idempotency_key"],
                "release_commit": payload["release_commit"],
                "executor_image_digest": payload["executor_image_digest"],
                "worker_source_digest": payload["worker_source_digest"],
            },
        )
        if attempt.get("status") == "BLOCKED":
            return attempt
        if attempt.get("repeat_noop") is True and attempt.get("status") != "PENDING":
            if attempt.get("status") != "RUNNING":
                return _request(environment, method="GET", path=f"/research/gates/{attempt['gate_attempt_id']}", payload=None)
        lease = _request(
            environment,
            method="POST",
            path=f"/research/gates/attempts/{attempt['gate_attempt_id']}/claim",
            payload={"actor_identity": "canonical-v13-planless-gate-runner"},
        )
        if lease.get("status") == "BLOCKED":
            return lease
        adapter = FreqtradeProductionLookaheadAdapter(
            activation=_required(environment, LOOKAHEAD_ACTIVATION_ENV),
            runtime_path=Path(_required(environment, OCI_RUNTIME_ENV)),
            image_reference=_required(environment, IMAGE_ENV),
            limits=limits,
            input_factory=inputs,
            runner=RemotePodmanVolumeSandboxRunner(),
        )
        with engine.connect() as connection:
            receipt = execute_production_static_lookahead_gate(
                connection, lineage=lineage, adapter=adapter
            )
        static_payload = asdict(receipt.static_receipt)
        static_payload["strategy_version_id"] = str(static_payload["strategy_version_id"])
        static_payload["lease_token"] = lease["lease_token"]
        persisted_static = _request(
            environment,
            method="POST",
            path=f"/research/gates/attempts/{attempt['gate_attempt_id']}/static-receipts",
            payload=static_payload,
        )
        if persisted_static.get("status") == "BLOCKED" or receipt.lookahead_receipt is None:
            return persisted_static if persisted_static.get("status") == "BLOCKED" else _request(environment, method="GET", path=f"/research/gates/{attempt['gate_attempt_id']}", payload=None)
        lookahead_payload = asdict(receipt.lookahead_receipt)
        lookahead_payload.pop("lineage", None)
        lookahead_payload["lease_token"] = lease["lease_token"]
        persisted_lookahead = _request(
            environment,
            method="POST",
            path=f"/research/gates/attempts/{attempt['gate_attempt_id']}/lookahead-receipts",
            payload=lookahead_payload,
        )
        if persisted_lookahead.get("status") == "BLOCKED":
            return persisted_lookahead
        return _request(environment, method="GET", path=f"/research/gates/{attempt['gate_attempt_id']}", payload=None)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "gate",
            "plan",
            "authorize",
            "consume",
            "revoke",
            "start",
            "status",
            "score",
            "qualify",
            "worker-execute",
        ),
    )
    parser.add_argument("--command-file", type=Path)
    parser.add_argument("--id")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    try:
        payload = _json_file(args.command_file) if args.command_file else None
        if args.command == "gate":
            if payload is None:
                raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_REQUIRED")
            with _gate_writer_lock(environment):
                result = _gate_execute(environment, payload)
        elif args.command == "worker-execute":
            if payload is None:
                raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_REQUIRED")
            result = _worker_execute(environment, payload)
        else:
            routes = {
                "plan": ("POST", "/research/validation-plans", False),
                "authorize": ("POST", "/research/authorizations", False),
                "consume": (
                    "POST",
                    "/research/authorizations/{id}/consume",
                    True,
                ),
                "revoke": ("POST", "/research/authorizations/{id}/revoke", True),
                "start": ("POST", "/research/attempts", False),
                "status": ("GET", "/research/validation-plans/{id}", True),
                "score": ("POST", "/research/scores", False),
                "qualify": ("POST", "/research/qualifications", False),
            }
            method, path, needs_id = routes[args.command]
            if needs_id and not args.id:
                raise ResearchCLIBlocked("BLOCKED_COMMAND_ID_REQUIRED")
            if method == "POST" and payload is None:
                raise ResearchCLIBlocked("BLOCKED_COMMAND_FILE_REQUIRED")
            if "{id}" in path:
                path = path.replace("{id}", str(UUID(args.id)))
            result = _request(environment, method=method, path=path, payload=payload)
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, ResearchCLIBlocked)
            else getattr(exc, "code", "BLOCKED_RESEARCH_CLI_FAILURE")
        )
        result = {"status": "BLOCKED", "reason_code": code}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    strategy_level_block = (
        args.command == "gate"
        and result.get("status") == "BLOCKED"
        and result.get("terminal_reason_code") == "LOOKAHEAD_INSUFFICIENT_TRADES"
    )
    return 0 if result.get("status") != "BLOCKED" or strategy_level_block else 2


if __name__ == "__main__":
    sys.exit(main())
