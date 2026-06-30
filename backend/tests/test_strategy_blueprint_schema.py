import pytest
from pydantic import ValidationError

from app.schemas.strategy_blueprint import BLUEPRINT_SCHEMA_VERSION, StrategyBlueprint


def valid_blueprint_payload() -> dict:
    return {
        "name": "Schema V2 Demo",
        "slug": "schema-v2-demo",
        "class_name": "SchemaV2DemoStrategy",
        "indicators": [
            {"name": "rsi", "kind": "rsi", "period": 14},
            {"name": "ema_fast", "kind": "ema", "period": 12},
        ],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 30}],
        "exit_rules": [{"indicator": "ema_fast", "operator": ">", "value": 1.0}],
    }


def test_strategy_blueprint_v2_accepts_valid_payload() -> None:
    blueprint = StrategyBlueprint(**valid_blueprint_payload())

    assert blueprint.schema_version == BLUEPRINT_SCHEMA_VERSION
    assert blueprint.model_dump()["schema_version"] == "2"


def test_strategy_blueprint_defaults_schema_version_for_phase1_payload() -> None:
    payload = valid_blueprint_payload()
    payload.pop("schema_version", None)

    blueprint = StrategyBlueprint(**payload)

    assert blueprint.schema_version == "2"


def test_strategy_blueprint_rejects_version_mismatch() -> None:
    payload = valid_blueprint_payload()
    payload["schema_version"] = "1"

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "schema_version" in str(exc_info.value)


def test_strategy_blueprint_rejects_extra_fields() -> None:
    payload = valid_blueprint_payload()
    payload["unexpected"] = "not allowed"

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "Extra inputs are not permitted" in str(exc_info.value)


def test_strategy_blueprint_rejects_missing_required_rules() -> None:
    payload = valid_blueprint_payload()
    payload.pop("entry_rules")

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "entry_rules" in str(exc_info.value)


def test_strategy_blueprint_rejects_invalid_indicator_reference() -> None:
    payload = valid_blueprint_payload()
    payload["entry_rules"] = [{"indicator": "missing", "operator": "<", "value": 30}]

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "rule indicator is not defined: missing" in str(exc_info.value)


def test_strategy_blueprint_rejects_invalid_operator() -> None:
    payload = valid_blueprint_payload()
    payload["entry_rules"] = [{"indicator": "rsi", "operator": "crosses", "value": 30}]

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "operator" in str(exc_info.value)


def test_strategy_blueprint_rejects_invalid_indicator_period() -> None:
    payload = valid_blueprint_payload()
    payload["indicators"][0]["period"] = 1

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "period" in str(exc_info.value)


def test_strategy_blueprint_rejects_duplicate_indicator_names() -> None:
    payload = valid_blueprint_payload()
    payload["indicators"][1]["name"] = "rsi"

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "indicator names must be unique: rsi" in str(exc_info.value)


def test_strategy_blueprint_rejects_out_of_range_rsi_rule_value() -> None:
    payload = valid_blueprint_payload()
    payload["entry_rules"] = [{"indicator": "rsi", "operator": "<", "value": 101}]

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "rsi rule value must be between 0 and 100" in str(exc_info.value)


def test_strategy_blueprint_rejects_invalid_moving_average_rule_value() -> None:
    payload = valid_blueprint_payload()
    payload["entry_rules"] = [{"indicator": "ema_fast", "operator": ">", "value": 0}]

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "moving average rule value must be positive" in str(exc_info.value)


def test_strategy_blueprint_rejects_invalid_minimal_roi_range() -> None:
    payload = valid_blueprint_payload()
    payload["minimal_roi"] = {"0": 12.0}

    with pytest.raises(ValidationError) as exc_info:
        StrategyBlueprint(**payload)

    assert "minimal_roi values must not exceed 10.0" in str(exc_info.value)
