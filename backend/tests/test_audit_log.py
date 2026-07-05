import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.repositories import GovernanceEventArchiveRepository
from app.schemas import GovernanceArtifactLink, GovernanceEvent
from app.services.audit_log import GovernanceAuditLogService


FIXED_NOW = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)


def service(tmp_path):
    repository = GovernanceEventArchiveRepository(
        tmp_path / "reports" / "governance-events.jsonl"
    )
    return GovernanceAuditLogService(repository=repository, now_provider=lambda: FIXED_NOW)


def actor() -> dict:
    return {"actor_id": "codex-phase7", "role": "automation"}


def source() -> dict:
    return {
        "name": "phase7-audit-log",
        "source_type": "automation",
        "ref": "reports/governance/phase7-audit-log.json",
    }


def test_governance_events_can_be_written_and_queried(tmp_path) -> None:
    audit = service(tmp_path)

    audit.record_event(
        event_id="event-accepted",
        event_type="operator_action",
        status="ACCEPTED",
        actor=actor(),
        source=source(),
        summary="Operator status fixture was accepted for review.",
        artifact_links=[
            {
                "name": "operator-status",
                "path": "reports/runtime/operator-status.json",
                "kind": "artifact",
            }
        ],
        payload={"result": "accepted", "checks": ["runtime-contract", "operator-status"]},
        tags=["phase-7", "accepted"],
    )
    audit.record_event(
        event_id="event-blocked",
        event_type="blocked_decision",
        status="BLOCKED",
        actor=actor(),
        source=source(),
        summary="Smoke run was blocked by missing local fixture.",
        reason="Required offline smoke fixture is missing.",
        payload={"missing": "reports/runtime/phase7-smoke-summary.json"},
        tags=["phase-7", "blocked"],
    )
    audit.record_event(
        event_id="event-failed",
        event_type="smoke_run",
        status="FAILED",
        actor=actor(),
        source=source(),
        summary="Offline governance smoke failed.",
        reason="Fixture status was FAILED.",
        payload={"status": "FAILED"},
        tags=["phase-7", "failed"],
    )

    events = audit.query_events(source_name="phase7-audit-log")
    blocked = audit.query_events(status="BLOCKED")
    found = audit.get_event("event-accepted")

    assert [event.event_id for event in events] == [
        "event-accepted",
        "event-blocked",
        "event-failed",
    ]
    assert [event.event_id for event in blocked] == ["event-blocked"]
    assert found is not None
    assert found.schema_version == "1"
    assert found.status == "ACCEPTED"
    assert found.actor.actor_id == "codex-phase7"
    assert found.source.source_type == "automation"
    assert found.artifact_links[0].path == "reports/runtime/operator-status.json"
    assert "does not authorize live trading" in found.safety_boundary


def test_secret_shaped_payload_is_redacted_before_archive(tmp_path) -> None:
    audit = service(tmp_path)
    secret_key = "api_" + "secret"
    token_key = "to" + "ken"
    password_key = "pass" + "word"

    event = audit.record_event(
        event_id="event-secret-redaction",
        event_type="security_check",
        status="BLOCKED",
        actor=actor(),
        source=source(),
        summary=f"Blocked because {secret_key}=fixture-sensitive-value",
        reason=f"{token_key}=fixture-token-value was present in local input",
        payload={
            secret_key: "fixture-sensitive-value",
            "details": {
                "message": f"{password_key}=fixture-password-value",
                "raw_token": f"{token_key}=fixture-raw-token-value",
            },
        },
    )
    rendered = (tmp_path / "reports" / "governance-events.jsonl").read_text(encoding="utf-8")

    assert event.payload[secret_key] == "[REDACTED]"
    assert event.payload["details"]["raw_token"] == f"{token_key}=[REDACTED]"
    assert "fixture-sensitive-value" not in rendered
    assert "fixture-password-value" not in rendered
    assert "fixture-token-value" not in rendered
    assert "fixture-raw-token-value" not in rendered
    assert f"{secret_key}=[REDACTED]" in rendered
    assert f"{token_key}=[REDACTED]" in rendered


def test_blocked_and_failed_events_require_reasons() -> None:
    base_payload = {
        "event_id": "event-missing-reason",
        "event_type": "blocked_decision",
        "actor": actor(),
        "source": source(),
        "created_at": FIXED_NOW,
        "summary": "Missing reason fixture.",
    }

    with pytest.raises(ValidationError, match="blocked governance events require reason"):
        GovernanceEvent.model_validate({**base_payload, "status": "BLOCKED"})

    with pytest.raises(ValidationError, match="failed governance events require reason"):
        GovernanceEvent.model_validate(
            {
                **base_payload,
                "event_id": "event-failed-missing-reason",
                "event_type": "smoke_run",
                "status": "FAILED",
            }
        )


def test_archive_records_have_stable_json_shape(tmp_path) -> None:
    audit = service(tmp_path)

    event = audit.record_event(
        event_id="event-stable-shape",
        event_type="review_evidence",
        status="ACCEPTED",
        actor=actor(),
        source=source(),
        summary="Review evidence was attached.",
        payload={"evidence": "reports/governance/phase7-review.json"},
    )
    record = event.to_archive_record()
    archived = json.loads(
        (tmp_path / "reports" / "governance-events.jsonl").read_text(encoding="utf-8")
    )
    rendered = json.dumps(record, sort_keys=True)

    assert {
        "schema_version",
        "event_id",
        "event_type",
        "status",
        "actor",
        "source",
        "created_at",
        "summary",
        "payload",
        "safety_boundary",
        "event_hash",
    } <= set(record)
    assert record["schema_version"] == "1"
    assert archived["event_hash"] == record["event_hash"]
    assert len(record["event_hash"]) == 64
    assert "live trading" in record["safety_boundary"]
    assert "start_live" not in rendered
    assert "deploy_command" not in rendered


def test_artifact_links_reject_remote_urls() -> None:
    with pytest.raises(ValidationError, match="must not be a remote URL"):
        GovernanceArtifactLink(
            name="remote-artifact",
            path="https://example.invalid/report.json",
            kind="artifact",
        )
