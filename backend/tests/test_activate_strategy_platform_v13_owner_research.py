from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.activate_strategy_platform_v13_owner_research import main, parser


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "activate_strategy_platform_v13_owner_research.py"
)


def test_cli_requires_explicit_business_allocation_and_apply_digest() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([])

    dry_run = parser().parse_args(["--candidates-per-target", "7"])
    assert dry_run.candidates_per_target == 7
    assert dry_run.apply is False
    assert dry_run.expected_input_digest is None

    apply = parser().parse_args(
        [
            "--candidates-per-target",
            "7",
            "--apply",
            "--expected-input-digest",
            "a" * 64,
        ]
    )
    assert apply.apply is True
    assert apply.expected_input_digest == "a" * 64
    with pytest.raises(SystemExit):
        main(["--candidates-per-target", "7", "--apply"])
    with pytest.raises(SystemExit):
        main(["--candidates-per-target", "0"])


def test_cli_is_local_owner_only_and_has_no_hidden_allocation_or_execution_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "postgresql+psycopg:///freqtrade_ai_design_lab" not in source
    assert 'f"postgresql+psycopg:///{_DATABASE_NAME}"' in source
    assert "DATABASE_URL" not in source
    assert "FREQTRADE_AI_DATABASE_URL" not in source
    assert "candidates_per_target=10" not in source
    assert "candidate_count=60" not in source
    assert "backtest" not in {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    assert "worker" not in {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    assert "okx" not in source.lower()


def test_cli_report_explicitly_preserves_no_execution_boundary() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        '"historical_revalidation_started": False',
        '"backtest_or_worker_started": False',
        '"signal_or_order_created": False',
        '"credentials_accessed": False',
        "assert_owner_activation_fence(db)",
        'transaction.rollback()',
        'transaction.commit()',
    ):
        assert fragment in source
