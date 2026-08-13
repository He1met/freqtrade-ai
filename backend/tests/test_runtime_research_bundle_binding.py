from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.services.profile_bound_adapters import (
    DiversityProfileContract,
    GenerationProfileContract,
    ScoringProfileContract,
)
from app.services.runtime_research_bundle_binding import (
    RuntimeResearchBundleBindingService,
)


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar


class _Connection:
    def __init__(self, current_user="freqtrade"):
        self.current_user = current_user
        self.statements: list[str] = []

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if sql == "SELECT current_user":
            return _Result(self.current_user)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Bundle:
    def __init__(self):
        self.snapshot = SimpleNamespace(
            snapshot_id=77,
            bundle_digest="a" * 64,
            workflow_kind="RESEARCH",
            scope_type="WORKFLOW",
            scope_key="production-research-v13",
            aggregate_profile_version_id=105,
        )
        quality_contract = {
            "quality_components": [{"component_key": "static"}],
            "elimination_rules": [{"rule_key": "eliminate"}],
            "warning_rules": [{"rule_key": "warn"}],
        }
        self.versions = {
            "generation-profile": SimpleNamespace(
                id=101,
                schema_version="generation-profile-v2",
                payload_json={
                    "demo_only": True,
                    "allow_real_funds": False,
                    "single_writer_required": True,
                },
            ),
            "diversity-profile": SimpleNamespace(
                id=102,
                schema_version="diversity-profile-v2",
                payload_json={},
            ),
            "quality-gate-profile": SimpleNamespace(
                id=103,
                schema_version="quality-gate-profile-v2",
                payload_json=quality_contract,
            ),
            "scoring-profile": SimpleNamespace(
                id=104,
                schema_version="scoring-profile-v2",
                payload_json={
                    **quality_contract,
                    "quality_gate_profile_version_id": 103,
                },
            ),
            "research-profile": SimpleNamespace(
                id=105,
                schema_version="research-profile-v2",
                payload_json={
                    "generation_profile_version_id": 101,
                    "diversity_profile_version_id": 102,
                    "quality_gate_profile_version_id": 103,
                    "scoring_profile_version_id": 104,
                    "demo_only": True,
                    "allow_real_funds": False,
                    "single_writer_required": True,
                },
            ),
        }

    def require_single_version(self, type_key):
        return self.versions[type_key]


def _install_bundle_mocks(monkeypatch, bundle):
    monkeypatch.setattr(
        "app.services.runtime_research_bundle_binding."
        "RuntimeConfigurationBundleReader.read_validated",
        lambda _self, _bundle_id: bundle,
    )
    monkeypatch.setattr(
        "app.services.runtime_research_bundle_binding.generation_profile",
        lambda _bundle: GenerationProfileContract(
            version_id=101,
            candidates_per_target=4,
            target_count=6,
            candidate_count=24,
            family_version_ids=(201, 202),
        ),
    )
    monkeypatch.setattr(
        "app.services.runtime_research_bundle_binding.diversity_profile",
        lambda _bundle: DiversityProfileContract(
            version_id=102,
            generation_profile_version_id=101,
            adapter_key="diversity-threshold-v2",
            required_family_version_ids=(201, 202),
            thresholds={"max_signal_similarity": 0.9},
        ),
    )
    monkeypatch.setattr(
        "app.services.runtime_research_bundle_binding.scoring_profile",
        lambda _bundle: ScoringProfileContract(
            version_id=104,
            adapter_key="profile-bound-score-v2",
            component_weights={"quality_score": 1.0},
            normalization_rules={"quality_score": {"transform": "linear"}},
            quality_components=(),
            elimination_rules=(),
            warning_rules=(),
        ),
    )


def test_runtime_binding_is_exact_sanitized_and_no_trade(monkeypatch) -> None:
    bundle = _Bundle()
    _install_bundle_mocks(monkeypatch, bundle)

    binding = RuntimeResearchBundleBindingService(_Connection()).read(77)

    assert binding.bundle_id == 77
    assert binding.candidates_per_target == 4
    assert binding.target_count == 6
    assert binding.candidate_count == 24
    readiness = binding.sanitized_readiness()
    assert readiness["runtime_mode"] == "V13_NO_TRADE"
    assert readiness["credential_attestation"] == "UNKNOWN_OUT_OF_SCOPE"
    assert readiness["worker_execution"] == "DISABLED"
    assert readiness["order_submission"] == "DISABLED"
    assert readiness["allow_real_funds"] is False


def test_runtime_binding_requires_exact_least_privilege_role(monkeypatch) -> None:
    connection = _Connection(current_user="owner")
    with pytest.raises(StrategyPlatformReadError) as exc_info:
        RuntimeResearchBundleBindingService(connection).read(77)

    assert exc_info.value.code == "V13_RUNTIME_ROLE_INVALID"
    assert connection.statements == ["SELECT current_user"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda bundle: setattr(bundle.snapshot, "scope_key", "production-research"),
            "V13_RESEARCH_BUNDLE_SCOPE_INVALID",
        ),
        (
            lambda bundle: setattr(
                bundle.versions["generation-profile"],
                "schema_version",
                "generation-profile-v1",
            ),
            "V13_RESEARCH_PROFILE_SCHEMA_INVALID",
        ),
        (
            lambda bundle: bundle.versions["research-profile"].payload_json.update(
                generation_profile_version_id=999
            ),
            "V13_RESEARCH_PROFILE_BINDING_INVALID",
        ),
        (
            lambda bundle: bundle.versions["research-profile"].payload_json.update(
                allow_real_funds=True
            ),
            "V13_RESEARCH_SAFETY_INVALID",
        ),
    ],
)
def test_runtime_binding_fails_closed_on_identity_drift(
    monkeypatch, mutation, expected_code
) -> None:
    bundle = _Bundle()
    mutation(bundle)
    _install_bundle_mocks(monkeypatch, bundle)

    with pytest.raises(StrategyPlatformReadError) as exc_info:
        RuntimeResearchBundleBindingService(_Connection()).read(77)

    assert exc_info.value.code == expected_code


def test_runtime_binding_has_no_owner_resolver_repository_or_models_import() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "app"
        / "services"
        / "runtime_research_bundle_binding.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "app.services.configuration_resolver" not in imported_modules
    assert "app.services.owner_research_activation" not in imported_modules
    assert "app.repositories.strategy_platform" not in imported_modules
    assert not any(module.startswith("app.models") for module in imported_modules)
