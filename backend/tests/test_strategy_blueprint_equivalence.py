from __future__ import annotations

import hashlib

import pytest

from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_blueprint_equivalence import (
    StrategyBlueprintEquivalenceBlocked,
    canonical_json_digest,
    prove_blueprint_code_equivalence,
)
from app.services.strategy_renderer import (
    STRATEGY_RENDERER_VERSION,
    StrategyCodeRenderer,
)


def _blueprint_payload() -> dict:
    return {
        "schema_version": "2",
        "name": "Exact Blueprint Candidate",
        "slug": "exact-blueprint-candidate",
        "class_name": "ExactBlueprintCandidate",
        "description": "Deterministic Blueprint v2 bridge fixture.",
        "timeframe": "15m",
        "stoploss": -0.1,
        "minimal_roi": {"0": 0.03},
        "indicators": [{"name": "rsi", "kind": "rsi", "period": 14}],
        "entry_rules": [{"indicator": "rsi", "operator": "<", "value": 35.0}],
        "exit_rules": [{"indicator": "rsi", "operator": ">", "value": 70.0}],
        "can_short": False,
        "short_entry_rules": [],
        "short_exit_rules": [],
        "regime_rules": [],
        "tags": ["formal-research"],
    }


def test_exact_renderer_bytes_prove_blueprint_equivalence() -> None:
    payload = _blueprint_payload()
    rendered = StrategyCodeRenderer().render(StrategyBlueprint.model_validate(payload))
    source = rendered.encode("utf-8")
    digest = hashlib.sha256(source).hexdigest()

    evidence = prove_blueprint_code_equivalence(
        blueprint_payload=payload,
        source_bytes=source,
        expected_source_digest=digest,
        expected_class_name="ExactBlueprintCandidate",
        expected_timeframe="15m",
    )

    assert evidence.rendered_code == rendered
    assert evidence.rendered_code_digest == digest
    assert evidence.renderer_version == STRATEGY_RENDERER_VERSION
    assert evidence.blueprint_digest == canonical_json_digest(
        StrategyBlueprint.model_validate(payload).model_dump(mode="json")
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("whitespace", "exact deterministic Blueprint v2 rendering"),
        ("empty", "exact deterministic Blueprint v2 rendering"),
        ("hash", "source digest changed"),
        ("class", "class does not match"),
        ("timeframe", "timeframe does not match"),
        ("renderer_version", "unsupported strategy renderer version"),
    ],
)
def test_equivalence_fails_closed_for_any_identity_or_byte_mismatch(
    mutation: str,
    message: str,
) -> None:
    payload = _blueprint_payload()
    rendered = StrategyCodeRenderer().render(StrategyBlueprint.model_validate(payload))
    source = rendered.encode("utf-8")
    expected_digest = hashlib.sha256(source).hexdigest()
    expected_class_name = "ExactBlueprintCandidate"
    expected_timeframe = "15m"
    renderer_version = STRATEGY_RENDERER_VERSION

    if mutation == "whitespace":
        source += b" "
        expected_digest = hashlib.sha256(source).hexdigest()
    elif mutation == "empty":
        source = b""
        expected_digest = hashlib.sha256(source).hexdigest()
    elif mutation == "hash":
        expected_digest = "0" * 64
    elif mutation == "class":
        expected_class_name = "DifferentCandidate"
    elif mutation == "timeframe":
        expected_timeframe = "5m"
    elif mutation == "renderer_version":
        renderer_version = "strategy-renderer-v999"

    with pytest.raises(StrategyBlueprintEquivalenceBlocked, match=message):
        prove_blueprint_code_equivalence(
            blueprint_payload=payload,
            source_bytes=source,
            expected_source_digest=expected_digest,
            expected_class_name=expected_class_name,
            expected_timeframe=expected_timeframe,
            renderer_version=renderer_version,
        )
