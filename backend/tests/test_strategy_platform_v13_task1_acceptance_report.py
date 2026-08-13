from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = (
    REPO_ROOT
    / "reports/migrations/strategy-platform-v13-task1-design-lab-acceptance.json"
)
MARKET_EVIDENCE_PATH = (
    REPO_ROOT
    / "reports/migrations/strategy-platform-v13-task1-market-evidence.json"
)
RECONCILIATION_PATH = (
    REPO_ROOT
    / "backend/scripts/strategy_platform_v13_task1_reconciliation.sql"
)
EVIDENCE_SQL_PATH = (
    REPO_ROOT / "docs/migrations/strategy_platform_v13_task1_evidence.sql"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_task1_acceptance_report_binds_current_immutable_artifacts() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert report["database_migration_subgate"] == "ACCEPTED"
    assert (
        report["overall_task1_status"]
        == "NOT_ACCEPTED_STATIC_RUNTIME_CUTOVER_PENDING"
    )
    assert report["architecture_boundary"]["legacy_database_target_mode"] == (
        "READ_ONLY_HISTORICAL_SOURCE"
    )
    assert report["architecture_boundary"]["legacy_database_read_only_enforced"] is False
    assert report["architecture_boundary"]["shared_in_place_migration_performed"] is False
    assert report["architecture_boundary"]["okx_live_accessed"] is False
    assert report["architecture_boundary"]["orders_or_signals_created"] is False

    sql_artifacts = report["read_only_sql_artifacts"]
    assert sql_artifacts["reconciliation_sha256"] == _sha256(RECONCILIATION_PATH)
    assert sql_artifacts["evidence_sha256"] == _sha256(EVIDENCE_SQL_PATH)

    market_evidence = report["market_evidence"]
    assert market_evidence["artifact_file_sha256"] == _sha256(MARKET_EVIDENCE_PATH)
    evidence_payload = json.loads(MARKET_EVIDENCE_PATH.read_text(encoding="utf-8"))
    artifact_digest = evidence_payload.pop("artifact_digest")
    canonical = json.dumps(
        evidence_payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert artifact_digest == hashlib.sha256(canonical).hexdigest()
    assert market_evidence["artifact_digest"] == artifact_digest
    assert market_evidence["freshness"] == "UNKNOWN"
    assert market_evidence["quality_decision"] == "NOT_STRATEGY_QUALIFICATION"


def test_task1_acceptance_report_is_private_and_keeps_remaining_gates_explicit() -> None:
    raw = REPORT_PATH.read_text(encoding="utf-8")
    report = json.loads(raw)

    assert "/Users/" not in raw
    assert "/private/" not in raw
    assert "/tmp/" not in raw
    assert report["tests"]["backend_failed"] == 0
    assert report["tests"]["backend_passed"] >= 1_600
    assert report["migration"]["destructive_write_count"] == 0
    assert report["migration"]["overwritten_row_count"] == 0
    assert report["migration"]["deleted_row_count"] == 0
    assert report["real_data_counts"]["qualified_evaluation_summaries"] == 0
    assert report["real_data_counts"]["unmapped"] == 0
    assert report["real_data_counts"]["conflicts"] == 0
    assert any("supervisor" in gate for gate in report["remaining_gates"])
    assert any("static" in gate or "AST" in gate for gate in report["remaining_gates"])
