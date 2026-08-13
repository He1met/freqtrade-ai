from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = REPOSITORY_ROOT / "scripts" / "audit_v13_business_constants.py"
MANIFEST_PATH = REPOSITORY_ROOT / "config" / "v13_business_constant_audit.json"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_v13_business_constants", AUDIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()


def _entry(finding, *, disposition: str | None = None, **updates):
    technical = finding.category in audit.TECHNICAL_CATEGORIES
    entry = {
        "path": finding.path,
        "line": finding.line,
        "symbol": finding.symbol,
        "fingerprint": finding.fingerprint,
        "category": finding.category,
        "disposition": disposition or ("TECHNICAL" if technical else "BLOCKED"),
        "reason": "Exact reviewed syntax node; no file or directory allowlist.",
        "owner": "issue-704-old-rule-removal",
        "dependency": "Task1 frozen configuration bundle/resolver contract",
    }
    entry.update(updates)
    return entry


def _manifest(target: str, findings: list[dict]) -> dict:
    return {
        "schema_version": audit.MANIFEST_SCHEMA,
        "baseline_commit": "synthetic-test-baseline",
        "scan_targets": [target],
        "findings": findings,
    }


def test_python_ast_detects_business_literals_and_hidden_fallbacks(tmp_path: Path) -> None:
    target = "backend/app/services/example.py"
    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_text(
        """
ALLOWED_RESEARCH_PAIRS = ("BTC/USDT:USDT", "SOL/USDT:USDT")
MIN_STRATEGY_SCORE = 50.0

def select(payload, requested_count=60):
    score = payload.get("min_strategy_score", 50)
    return payload.get("timeframe", "5m"), score, requested_count
""".lstrip(),
        encoding="utf-8",
    )

    findings = audit.scan_source(target, path.read_text(encoding="utf-8"))
    categories = {finding.category for finding in findings}
    symbols = {finding.symbol for finding in findings}

    assert {"RESEARCH_TARGET", "QUALITY_GATE", "GENERATION_PROFILE", "TIMEFRAME_DEFINITION"} <= categories
    assert any("fallback:min_strategy_score" in symbol for symbol in symbols)
    assert any("fallback:timeframe" in symbol for symbol in symbols)

    report = audit.audit_repository(tmp_path, _manifest(target, []))
    assert report.ok is False
    assert set(report.unknown) == set(findings)


def test_good_python_uses_runtime_bundle_values_without_literal_findings() -> None:
    source = """
def select(bundle, target_id):
    profile = bundle.require_single_version("generation_profile")
    return profile.payload_json["requested_count"], target_id
""".lstrip()

    assert audit.scan_source("backend/app/services/example.py", source) == ()


def test_typescript_scans_business_defaults_but_not_dynamic_projection() -> None:
    bad = audit.scan_source(
        "frontend/src/pages/example.tsx",
        'const expectedCount = batch?.expected_count ?? 60;\n'
        'if (contract.max_drawdown_per_validation_window === 0.15) return "固定质量门";\n',
    )
    assert {finding.category for finding in bad} == {"GENERATION_PROFILE", "QUALITY_GATE"}

    good = audit.scan_source(
        "frontend/src/pages/example.tsx",
        "const expectedCount = bundle.generationProfile.requestedCount;\n"
        "return metadata.qualityGateLabel;\n",
    )
    assert good == ()


def test_yaml_and_json_findings_bind_exact_json_pointers() -> None:
    yaml_findings = audit.scan_source(
        "config/example.yaml",
        "research:\n  instruments: [BTC-USDT-SWAP, SOL-USDT-SWAP]\n  requested_count: 60\n",
    )
    assert {(finding.symbol, finding.category) for finding in yaml_findings} == {
        ("json-pointer:/research/instruments", "RESEARCH_TARGET"),
        ("json-pointer:/research/requested_count", "GENERATION_PROFILE"),
    }

    json_findings = audit.scan_source(
        "config/compatibility/example.json",
        json.dumps({"market": {"supported_bars": ["5m", "15m"]}}),
    )
    assert [(finding.symbol, finding.category) for finding in json_findings] == [
        ("json-pointer:/market/supported_bars", "ADAPTER_CAPABILITY")
    ]


def test_manifest_rejects_broad_paths_and_business_as_technical(tmp_path: Path) -> None:
    target = "backend/app/core/example.py"
    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_text("MIN_STRATEGY_SCORE = 50\n", encoding="utf-8")
    finding = audit.scan_source(target, path.read_text(encoding="utf-8"))[0]

    broad = _manifest("backend/app/**/*.py", [])
    broad_report = audit.audit_repository(tmp_path, broad)
    assert any("exact repository-relative path" in error for error in broad_report.errors)

    disguised = _manifest(target, [_entry(finding, disposition="TECHNICAL")])
    disguised_report = audit.audit_repository(tmp_path, disguised)
    assert any("disguises business category" in error for error in disguised_report.errors)


def test_exact_baseline_detects_unknown_and_stale_fingerprints(tmp_path: Path) -> None:
    target = "backend/app/core/example.py"
    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_text("MIN_STRATEGY_SCORE = 50\n", encoding="utf-8")
    original = audit.scan_source(target, path.read_text(encoding="utf-8"))[0]
    manifest = _manifest(target, [_entry(original)])

    assert audit.audit_repository(tmp_path, manifest).ok is True

    path.write_text("MIN_STRATEGY_SCORE = 51\n", encoding="utf-8")
    drift = audit.audit_repository(tmp_path, manifest)
    assert drift.ok is False
    assert len(drift.unknown) == 1
    assert len(drift.stale) == 1
    assert drift.unknown[0].fingerprint != original.fingerprint


def test_exclusions_are_narrow_and_cannot_be_manifest_targets(tmp_path: Path) -> None:
    assert audit.is_excluded_path("backend/app/db/migrations.py")
    assert audit.is_excluded_path("scripts/smoke_phase8.py")
    assert audit.is_excluded_path("scripts/seed_local_strategy_lab_acceptance.py")
    assert audit.is_excluded_path("scripts/okx_demo_e2e.py")
    assert audit.is_excluded_path("scripts/spike_real_freqtrade_backtest.py")
    assert not audit.is_excluded_path("backend/app/services/local_runtime_policy.py")
    assert not audit.is_excluded_path("frontend/src/pages/ResearchQueue.tsx")

    manifest = _manifest("scripts/smoke_phase8.py", [])
    report = audit.audit_repository(tmp_path, manifest)
    assert any("cannot be a scan target" in error for error in report.errors)


def test_frozen_bundle_digest_and_safety_literals_are_technical_invariants() -> None:
    target = "backend/app/services/frozen_configuration_bundle.py"
    findings = audit.scan_source(
        target,
        (REPOSITORY_ROOT / target).read_text(encoding="utf-8"),
    )
    technical = [finding for finding in findings if finding.category == "TECHNICAL_INVARIANT"]

    assert technical
    assert any("BUNDLE_DIGEST_CONTRACT" in finding.symbol for finding in technical)
    assert any("REQUIRED_CAPABILITIES" in finding.symbol for finding in technical)
    assert not any(finding.category == "SAFETY_POLICY" for finding in findings)


def test_current_repository_matches_versioned_exact_baseline() -> None:
    manifest = audit.load_manifest(MANIFEST_PATH)
    report = audit.audit_repository(REPOSITORY_ROOT, manifest)

    assert report.ok, (
        f"errors={report.errors}; "
        f"unknown={[finding.__dict__ for finding in report.unknown[:10]]}; "
        f"stale={list(report.stale[:10])}"
    )
    assert report.findings
    assert report.blocked_count > 0
    assert report.technical_count > 0

    entries = manifest["findings"]
    assert all(entry["disposition"] == "BLOCKED" for entry in entries if entry["category"] not in audit.TECHNICAL_CATEGORIES)
    assert all("*" not in entry["path"] for entry in entries)
    assert {entry["path"] for entry in entries} <= set(manifest["scan_targets"])
