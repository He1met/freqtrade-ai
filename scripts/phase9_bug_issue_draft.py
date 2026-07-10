#!/usr/bin/env python3
"""Build a reviewable, redacted Issue draft from one runtime evidence record."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


CLASSIFICATIONS = {"Bug", "Test Gap", "Config Gap"}
SOURCE_TYPES = {"database", "api_aggregate", "fixture", "fallback", "mock", "unknown"}
SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "secret",
    "token",
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|passphrase|password|secret|token)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
ENV_SECRET = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:API_KEY|PASSWORD|PASSPHRASE|SECRET|TOKEN))=([^\s]+)"
)


class EvidenceValidationError(ValueError):
    """Evidence cannot produce a reviewable draft."""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a redacted GitHub Issue draft payload. This command never publishes."
    )
    parser.add_argument("evidence", type=Path, help="JSON evidence for exactly one failure.")
    parser.add_argument("--output", type=Path, help="Optional local JSON output path.")
    parser.add_argument("--json", action="store_true", help="Print the complete draft payload as JSON.")
    return parser.parse_args(argv)


def redact_text(value: str) -> str:
    value = BEARER_VALUE.sub("Bearer [REDACTED]", value)
    value = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    return ENV_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


def redact_value(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if any(part in normalized_key for part in SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(child_key): redact_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def _required_text(payload: Mapping[str, Any], name: str, errors: List[str]) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name} must be a non-empty string")
        return ""
    return value.strip()


def validate_evidence(payload: Mapping[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    normalized = dict(payload)
    for name in ("title", "current_behavior", "expected_behavior", "environment"):
        normalized[name] = _required_text(payload, name, errors)

    steps = payload.get("reproduction_steps")
    if not isinstance(steps, list) or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
        errors.append("reproduction_steps must be a non-empty list of strings")
    else:
        normalized["reproduction_steps"] = [step.strip() for step in steps]

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        errors.append("evidence must be a non-empty object")
    else:
        normalized["evidence"] = dict(evidence)

    source_type = payload.get("data_source")
    if source_type not in SOURCE_TYPES:
        errors.append("data_source must be one of: " + ", ".join(sorted(SOURCE_TYPES)))

    requested = payload.get("classification")
    if requested is not None and requested not in CLASSIFICATIONS:
        errors.append("classification must be Bug, Test Gap, or Config Gap")

    if errors:
        raise EvidenceValidationError("; ".join(errors))
    return normalized


def classify_evidence(payload: Mapping[str, Any]) -> Tuple[str, str]:
    requested = payload.get("classification")
    if requested in CLASSIFICATIONS:
        return str(requested), "classification explicitly supplied by the evidence producer"

    status = str(payload.get("status", "")).upper()
    missing = payload.get("missing_prerequisites")
    if status == "BLOCKED" and isinstance(missing, list) and missing:
        return "Config Gap", "run is BLOCKED by one or more declared prerequisites"

    if payload.get("product_behavior_observed") is False:
        return "Test Gap", "evidence does not demonstrate incorrect product behavior"

    return "Bug", "evidence demonstrates observed behavior that differs from expected behavior"


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n```"


def build_draft(raw_payload: Mapping[str, Any]) -> Dict[str, Any]:
    payload = redact_value(validate_evidence(raw_payload))
    classification, classification_reason = classify_evidence(payload)
    status = str(payload.get("status") or "FAILED").upper()
    missing = payload.get("missing_prerequisites") or []
    blocked_reason = str(payload.get("blocked_reason") or "")
    next_action = str(payload.get("next_action") or "Review the evidence and assign an owner.")
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(payload["reproduction_steps"], 1))
    impact = payload.get("impact") or {}
    security = payload.get("security") or {}

    body = f"""## Classification

- Type: `{classification}`
- Reason: {classification_reason}
- Runtime status: `{status}`
- Environment: `{payload['environment']}`

## Reproduction steps

{steps}

## Current behavior

{payload['current_behavior']}

## Expected behavior

{payload['expected_behavior']}

## Page / API / database impact

{_json_block(impact)}

## Data source

- Source type: `{payload['data_source']}`
- Acceptable as real evidence: `{'yes' if payload.get('acceptance_ready') is True else 'no'}`

## Runtime evidence

{_json_block(payload['evidence'])}

## Blocking conditions

- Reason: {blocked_reason or 'None declared.'}
- Missing prerequisites: {', '.join(str(item) for item in missing) or 'None declared.'}
- Next action: {next_action}

## Security review

{_json_block(security)}

## Acceptance criteria

- [ ] The failure is reproducible, or the BLOCKED prerequisite is explicit.
- [ ] Page, API, and database evidence agree after the fix.
- [ ] Data source and acceptance status are accurate.
- [ ] No secret, credential, cookie, token, or account-sensitive value is present.
- [ ] The change remains scoped to this single finding.
"""
    labels = [classification.lower().replace(" ", "-")]
    if status == "BLOCKED":
        labels.append("blocked")
    return {
        "schema_version": "phase9-bug-issue-draft/v1",
        "publish_allowed": False,
        "review_required": True,
        "title": f"[{classification}] {payload['title']}",
        "body": body,
        "classification": classification,
        "classification_reason": classification_reason,
        "labels": labels,
        "source_evidence": payload,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        raw_payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, Mapping):
            raise EvidenceValidationError("evidence root must be a JSON object")
        draft = build_draft(raw_payload)
    except (OSError, json.JSONDecodeError, EvidenceValidationError) as exc:
        print(f"Invalid evidence: {redact_text(str(exc))}", file=sys.stderr)
        return 2

    serialized = json.dumps(draft, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    if args.json:
        print(serialized, end="")
    else:
        print(draft["body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
