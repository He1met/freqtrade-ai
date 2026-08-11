import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "okx_demo_natural_chain_preflight.py"
)
SPEC = importlib.util.spec_from_file_location("natural_chain_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def test_contract_matrix_has_every_required_safety_class_once() -> None:
    categories = [check.category for check in preflight.CONTRACT_CHECKS]
    assert categories == [
        "SCHEMA_ACL_SECURITY_DEFINER",
        "SNAPSHOT_SIGNAL_BINDING",
        "RECEIPT_LINEAGE_DEPLOYMENT_POLICY",
        "DEMO_READINESS_RECONCILIATION_GUARD",
        "ACTIONABLE_CLAIM_EXECUTION_HANDOFF",
        "WRITER_LEASE_FENCING",
        "RISK_BUDGET_DECISION_IDEMPOTENCY",
    ]
    assert all(check.nodeids and check.proves for check in preflight.CONTRACT_CHECKS)
    assert len(list(preflight._all_nodeids())) == len(
        set(preflight._all_nodeids())
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://postgres@127.0.0.1/freqtrade_ai",
        "postgresql+psycopg://postgres@127.0.0.1/unrelated",
    ],
)
def test_external_mode_refuses_canonical_or_unmarked_database(url: str) -> None:
    with pytest.raises(ValueError, match="disposable"):
        preflight.validate_external_url(url, isolated=True)


def test_external_mode_requires_explicit_isolation_attestation() -> None:
    with pytest.raises(ValueError, match="external PostgreSQL requires"):
        preflight.validate_external_url(
            "postgresql+psycopg://postgres@127.0.0.1/freqtrade_ai_preflight_test",
            isolated=False,
        )


def test_external_mode_accepts_disposable_test_database() -> None:
    preflight.validate_external_url(
        "postgresql+psycopg://postgres@127.0.0.1/freqtrade_ai_preflight_test",
        isolated=True,
    )


def test_parameterized_junit_cases_roll_up_to_the_contract_name(tmp_path) -> None:
    junit = tmp_path / "report.xml"
    junit.write_text(
        "<testsuite>"
        '<testcase name="test_gate[first]" />'
        '<testcase name="test_gate[second]" />'
        "</testsuite>",
        encoding="utf-8",
    )
    assert preflight._test_outcomes(junit) == {"test_gate": "PASSED"}
