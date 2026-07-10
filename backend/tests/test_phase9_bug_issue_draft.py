from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]


def run_draft(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    evidence_path = Path("/tmp") / f"phase9-bug-evidence-{uuid4().hex}.json"
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        return subprocess.run(
            [sys.executable, "scripts/phase9_bug_issue_draft.py", str(evidence_path), "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        evidence_path.unlink(missing_ok=True)


def base_payload() -> dict[str, object]:
    return {
        "title": "Local Strategy Lab shows success without a database record",
        "status": "FAILED",
        "environment": "phase9-local",
        "reproduction_steps": ["Open Local Strategy Lab.", "Run generation once."],
        "current_behavior": "The page reports success but the API has no run id.",
        "expected_behavior": "Success requires a persisted generation run id.",
        "data_source": "api_aggregate",
        "acceptance_ready": False,
        "product_behavior_observed": True,
        "impact": {"page": "/local-strategy-lab", "api": "/api/strategy-generation-runs"},
        "evidence": {"http_status": 200, "database_ids": []},
        "next_action": "Reproduce against the local test database.",
        "security": {"contains_secrets": False},
    }


def test_builds_review_only_bug_payload_and_redacts_secrets() -> None:
    payload = base_payload()
    marker = "fixture-sensitive-marker"
    payload["evidence"] = {
        "authorization": marker,
        "log": f"Authorization: Bearer {marker} token={marker}",
    }

    result = run_draft(payload)

    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout
    draft = json.loads(result.stdout)
    assert draft["classification"] == "Bug"
    assert draft["publish_allowed"] is False
    assert draft["review_required"] is True
    assert draft["labels"] == ["bug"]
    assert "[REDACTED]" in draft["body"]


def test_classifies_blocked_prerequisites_as_config_gap() -> None:
    payload = base_payload()
    payload.update(
        {
            "status": "BLOCKED",
            "missing_prerequisites": ["freqtrade binary", "market data"],
            "blocked_reason": "Local runner prerequisites are absent.",
        }
    )

    result = run_draft(payload)

    assert result.returncode == 0, result.stderr
    draft = json.loads(result.stdout)
    assert draft["classification"] == "Config Gap"
    assert draft["labels"] == ["config-gap", "blocked"]


def test_classifies_unproven_product_failure_as_test_gap() -> None:
    payload = base_payload()
    payload["product_behavior_observed"] = False

    result = run_draft(payload)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["classification"] == "Test Gap"


def test_rejects_missing_required_fields_without_draft() -> None:
    result = run_draft({"title": "Incomplete", "evidence": {"status": "failed"}})

    assert result.returncode == 2
    assert result.stdout == ""
    assert "reproduction_steps" in result.stderr
    assert "current_behavior" in result.stderr
