from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import StrategyCodeRenderer
from app.services.strategy_static_review import StrategyStaticReviewService


def test_generated_strategy_passes_static_review() -> None:
    blueprint = StrategyBlueprint(
        name="Safe RSI",
        slug="safe-rsi",
        class_name="SafeRsiStrategy",
        indicators=[{"name": "rsi", "kind": "rsi", "period": 14}],
        entry_rules=[{"indicator": "rsi", "operator": "<", "value": 35}],
        exit_rules=[{"indicator": "rsi", "operator": ">", "value": 70}],
    )
    code = StrategyCodeRenderer().render(blueprint)

    result = StrategyStaticReviewService().review_code(code)

    assert result.passed is True
    assert result.findings == []
    assert result.summary == {"errors": 0, "warnings": 0}


def test_static_review_reports_syntax_errors() -> None:
    result = StrategyStaticReviewService().review_code("class BrokenStrategy(:\n    pass\n")

    assert result.passed is False
    assert result.summary["errors"] == 1
    assert result.findings[0].category == "syntax_error"
    assert result.findings[0].rule_id == "syntax.parse"


def test_static_review_blocks_forbidden_imports_and_secret_access() -> None:
    code = """
import os
from pathlib import Path


API_KEY = os.getenv("EXCHANGE_API_KEY")
CONFIG = Path("config.yml").read_text()
"""

    result = StrategyStaticReviewService().review_code(code)
    categories = {finding.category for finding in result.findings}
    rule_ids = {finding.rule_id for finding in result.findings}

    assert result.passed is False
    assert "secret_access" in categories
    assert "file_access" in categories
    assert "import.os" in rule_ids
    assert "call.os.getenv" in rule_ids
    assert "call.file_access" in rule_ids


def test_static_review_blocks_network_and_dynamic_execution() -> None:
    code = """
import requests


def populate_indicators(dataframe, metadata):
    exec("print('unsafe')")
    requests.get("https://example.com")
    return dataframe
"""

    result = StrategyStaticReviewService().review_code(code)
    categories = {finding.category for finding in result.findings}
    rule_ids = {finding.rule_id for finding in result.findings}

    assert result.passed is False
    assert "network_access" in categories
    assert "dangerous_call" in categories
    assert "import.requests" in rule_ids
    assert "call.exec" in rule_ids
    assert "call.network_client" in rule_ids


def test_static_review_blocks_lookahead_patterns() -> None:
    code = """
def populate_indicators(dataframe, metadata):
    dataframe["future_close"] = dataframe["close"].shift(-1)
    dataframe["last_close"] = dataframe.iloc[-1]["close"]
    return dataframe
"""

    result = StrategyStaticReviewService().review_code(code)
    rule_ids = {finding.rule_id for finding in result.findings}

    assert result.passed is False
    assert "lookahead.shift_negative" in rule_ids
    assert "lookahead.iloc_negative" in rule_ids


def test_static_review_findings_can_be_mapped_to_failure_reasons() -> None:
    result = StrategyStaticReviewService().review_code("eval('1 + 1')\n")

    payloads = StrategyStaticReviewService().build_failure_reasons(
        strategy_id=1,
        strategy_version_id=2,
        result=result,
    )

    assert len(payloads) == 1
    assert payloads[0].stage == "static_check"
    assert payloads[0].reason_type == "static_policy_violation"
    assert payloads[0].details["rule_id"] == "call.eval"
