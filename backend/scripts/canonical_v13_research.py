#!/usr/bin/env python3
"""Canonical V1.3 audited no-trade research control and one-shot worker CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
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
    FreqtradeProductionResearchAdapter,
    ProductionResearchLimits,
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


class ResearchCLIBlocked(RuntimeError):
    pass


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
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
        if args.command == "worker-execute":
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
    return 0 if result.get("status") not in {"BLOCKED"} else 2


if __name__ == "__main__":
    sys.exit(main())
