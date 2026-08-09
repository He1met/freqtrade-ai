from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from app.schemas.strategy_blueprint import StrategyBlueprint
from app.services.strategy_renderer import STRATEGY_RENDERER_VERSION, StrategyCodeRenderer


class StrategyBlueprintEquivalenceBlocked(ValueError):
    """Blueprint evidence cannot reproduce the exact researched source bytes."""


@dataclass(frozen=True)
class StrategyBlueprintEquivalence:
    blueprint: StrategyBlueprint
    blueprint_digest: str
    rendered_code: str
    rendered_code_digest: str
    renderer_version: str


def canonical_json_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def prove_blueprint_code_equivalence(
    *,
    blueprint_payload: dict[str, Any],
    source_bytes: bytes,
    expected_source_digest: str,
    expected_class_name: str,
    expected_timeframe: str,
    renderer_version: str = STRATEGY_RENDERER_VERSION,
    renderer: StrategyCodeRenderer | None = None,
) -> StrategyBlueprintEquivalence:
    if renderer_version != STRATEGY_RENDERER_VERSION:
        raise StrategyBlueprintEquivalenceBlocked("unsupported strategy renderer version")
    blueprint = StrategyBlueprint.model_validate(blueprint_payload)
    if blueprint.class_name != expected_class_name:
        raise StrategyBlueprintEquivalenceBlocked("blueprint class does not match candidate identity")
    if blueprint.timeframe != expected_timeframe:
        raise StrategyBlueprintEquivalenceBlocked("blueprint timeframe does not match research contract")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    if source_digest != expected_source_digest:
        raise StrategyBlueprintEquivalenceBlocked("research candidate source digest changed")
    rendered_code = (renderer or StrategyCodeRenderer()).render(blueprint)
    rendered_bytes = rendered_code.encode("utf-8")
    if rendered_bytes != source_bytes:
        raise StrategyBlueprintEquivalenceBlocked(
            "candidate source is not the exact deterministic Blueprint v2 rendering"
        )
    rendered_digest = hashlib.sha256(rendered_bytes).hexdigest()
    return StrategyBlueprintEquivalence(
        blueprint=blueprint,
        blueprint_digest=canonical_json_digest(blueprint.model_dump(mode="json")),
        rendered_code=rendered_code,
        rendered_code_digest=rendered_digest,
        renderer_version=renderer_version,
    )
