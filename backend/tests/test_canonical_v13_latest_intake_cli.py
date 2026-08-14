from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from app.canonical_v13.latest_intake_manifest import build_latest_strategy_manifest
import pytest


def _load_cli() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "canonical_v13_latest_strategy_intake.py"
    )
    spec = importlib.util.spec_from_file_location("canonical_latest_intake_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_strategy(root: Path, class_name: str, run: int) -> None:
    generated = root / "generated"
    generated.mkdir(exist_ok=True)
    (generated / f"{class_name.lower()}_run_{run}_1.py").write_text(
        "from freqtrade.strategy import IStrategy\n"
        f"class {class_name}(IStrategy):\n"
        "    pass\n",
        encoding="utf-8",
    )


def test_apply_uses_no_trade_api_and_persists_receipt_only_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    cli = _load_cli()
    source = tmp_path / "source"
    source.mkdir()
    _write_strategy(source, "Alpha", 2)
    manifest = build_latest_strategy_manifest(source)
    calls: list[tuple[str, dict[str, object] | None]] = []

    def request(_origin: str, path: str, *, body=None):
        calls.append((path, body))
        if path == "/healthz":
            return {"status": "HEALTHY", "trading_capability": "TRADING_DISABLED"}
        return {
            "submission_id": "submission-1",
            "artifact_id": "artifact-1",
            "strategy_id": "strategy-1",
            "strategy_version_id": "version-1",
            "intake_receipt_id": "receipt-1",
            "receipt_digest": "a" * 64,
            "request_digest": "b" * 64,
            "artifact_digest": manifest.entries[0].selected_code_digest,
            "intake_status": "INTAKE_ACCEPTED",
            "catalog_status": "DRAFT",
            "validation_status": "UNVALIDATED",
            "qualification_status": "NOT_EVALUATED",
            "execution_authorized": False,
            "idempotent_replay": False,
        }

    monkeypatch.setattr(cli, "_request", request)
    evidence_path = tmp_path / "private" / "evidence.json"
    result = cli.apply_manifest(
        manifest,
        api_origin="http://127.0.0.1:8011",
        evidence_output=evidence_path,
        expected_archive_digest=manifest.archive_snapshot_digest,
    )

    assert [call[0] for call in calls] == [
        "/healthz",
        "/api/canonical-v13/submissions",
    ]
    command = calls[1][1]
    assert command is not None
    assert command["source_strategy_key"] == "Alpha"
    assert command["display_name"] == "Alpha"
    assert len(command["idempotency_key"]) < 200
    assert result["status"] == "INTAKE_ACCEPTED"
    persisted = json.loads(evidence_path.read_text())
    assert persisted["results"][0]["receipt_digest"] == "a" * 64
    assert "artifact_base64" not in evidence_path.read_text()
    assert str(source) not in evidence_path.read_text()


def test_cli_records_blocked_source_without_api_or_execution(
    tmp_path: Path, monkeypatch
) -> None:
    cli = _load_cli()
    source = tmp_path / "source"
    source.mkdir()
    generated = source / "generated"
    generated.mkdir()
    (generated / "unsafe_run_1_1.py").write_text(
        "import os\n"
        "from freqtrade.strategy import IStrategy\n"
        "class Unsafe(IStrategy):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API must not be called")
        ),
    )
    evidence = tmp_path / "blocked.json"
    exit_code = cli.main(
        [
            "apply",
            "--source-root",
            str(source),
            "--evidence-output",
            str(evidence),
            "--expected-archive-digest",
            "a" * 64,
        ]
    )
    assert exit_code == 2
    payload = json.loads(evidence.read_text())
    assert payload["status"] == "BLOCKED"
    assert payload["reason_code"] == "REJECTED_IMPORT_NOT_ALLOWED"
    assert payload["legacy_database_access"] == "NONE"
    assert payload["execution_side_effects"] == 0


def test_apply_rejects_plan_digest_drift_before_api(
    tmp_path: Path, monkeypatch
) -> None:
    cli = _load_cli()
    source = tmp_path / "source"
    source.mkdir()
    _write_strategy(source, "Alpha", 1)
    manifest = build_latest_strategy_manifest(source)
    monkeypatch.setattr(
        cli,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API must not be called")
        ),
    )
    with pytest.raises(cli.LatestIntakeCLIBlocked) as raised:
        cli.apply_manifest(
            manifest,
            api_origin="http://127.0.0.1:8011",
            evidence_output=tmp_path / "evidence.json",
            expected_archive_digest="0" * 64,
        )
    assert raised.value.code == "BLOCKED_ARCHIVE_SNAPSHOT_DRIFT"


def test_apply_preserves_prior_receipts_when_a_later_request_is_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    cli = _load_cli()
    source = tmp_path / "source"
    source.mkdir()
    _write_strategy(source, "Alpha", 1)
    _write_strategy(source, "Beta", 1)
    manifest = build_latest_strategy_manifest(source)
    submission_count = 0

    def request(_origin: str, path: str, *, body=None):
        nonlocal submission_count
        if path == "/healthz":
            return {"status": "HEALTHY", "trading_capability": "TRADING_DISABLED"}
        submission_count += 1
        if submission_count == 2:
            raise cli.LatestIntakeCLIBlocked("BLOCKED_CANONICAL_API_HTTP_503")
        entry = manifest.entries[0]
        return {
            "submission_id": "submission-1",
            "artifact_id": "artifact-1",
            "strategy_id": "strategy-1",
            "strategy_version_id": "version-1",
            "intake_receipt_id": "receipt-1",
            "receipt_digest": "a" * 64,
            "request_digest": "b" * 64,
            "artifact_digest": entry.selected_code_digest,
            "intake_status": "INTAKE_ACCEPTED",
            "catalog_status": "DRAFT",
            "validation_status": "UNVALIDATED",
            "qualification_status": "NOT_EVALUATED",
            "execution_authorized": False,
            "idempotent_replay": False,
        }

    monkeypatch.setattr(cli, "_request", request)
    evidence_path = tmp_path / "evidence.json"
    with pytest.raises(cli.LatestIntakeCLIBlocked) as raised:
        cli.apply_manifest(
            manifest,
            api_origin="http://127.0.0.1:8011",
            evidence_output=evidence_path,
            expected_archive_digest=manifest.archive_snapshot_digest,
        )
    assert raised.value.evidence_preserved is True
    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "BLOCKED"
    assert evidence["results"][0]["status"] == "INTAKE_ACCEPTED"
    assert evidence["results"][1]["reason_code"] == "BLOCKED_CANONICAL_API_HTTP_503"
