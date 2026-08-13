#!/usr/bin/env python3
"""Fail closed when V1.3 business literals drift outside the reviewed baseline.

The audit deliberately works at syntax-node / structured-key granularity.  A
baseline entry is not an allowlist for a file or directory: it binds one exact
path, symbol, line, category, and content fingerprint.  Existing business
rules remain visible as ``BLOCKED`` debt until their bundle-backed replacement
lands; only narrowly defined safety/protocol invariants may be ``TECHNICAL``.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by the CLI error path
    yaml = None


MANIFEST_SCHEMA = "v13-business-constant-audit-v1"
DEFAULT_MANIFEST = "config/v13_business_constant_audit.json"

# These are evidence/fixture classes explicitly excluded by the V1.3 design.
# The patterns are intentionally narrow and cannot be extended by the manifest.
EXCLUDED_PATH_PATTERNS = (
    "backend/app/db/migrations.py",
    "backend/app/db/migrations/**",
    "docs/migrations/**",
    "reports/**",
    "artifacts/**",
    "user_data/**",
    "scripts/smoke_*.py",
    "scripts/seed_*.py",
    "scripts/*e2e*.py",
    "scripts/spike_*.py",
    "scripts/phase*.py",
    "scripts/run_local_strategy_lab_acceptance_server.py",
)

TECHNICAL_CATEGORIES = frozenset(
    {
        "ADAPTER_CAPABILITY",
        "INTEGRITY_INVARIANT",
        "PROTOCOL_CONSTANT",
        "SAFETY_INVARIANT",
        "TECHNICAL_INVARIANT",
    }
)

SAFETY_TRUE_TOKENS = (
    "demo_only",
    "fail_closed",
    "single_writer",
    "unique_writer",
)
SAFETY_FALSE_TOKENS = (
    "allow_real_funds",
    "allow_live",
    "real_orders",
    "manual_order_authorized",
)

BUSINESS_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DIVERSITY_PROFILE", ("similarity", "correlation", "diversity")),
    (
        "QUALITY_GATE",
        (
            "quality",
            "score",
            "drawdown",
            "min_trade",
            "minimum_trade",
            "total_trades",
            "positive_net_profit",
            "profit_pct",
            "fee_per_side",
            "slippage_per_side",
            "lookahead",
            "profit_threshold",
        ),
    ),
    ("VALIDATION_WINDOW", ("validation_window", "window_key", "windows", "window", "walk_forward", "oos")),
    ("MARKET_REGIME", ("market_regime", "regime", "bull", "bear")),
    (
        "GENERATION_PROFILE",
        (
            "candidate_count",
            "candidate_limit",
            "candidates_per",
            "requested_count",
            "expected_count",
            "batch_size",
            "unit_slot",
            "strategy_family",
            "families",
            "family",
            "候选",
        ),
    ),
    (
        "WORKER_EXECUTION_PROFILE",
        (
            "max_matrix",
            "matrix_task",
            "max_parallel",
            "parallelism",
            "worker_timeout",
            "lease_seconds",
            "lease_ttl",
            "retry_limit",
            "retry_count",
            "串行",
            "并行",
        ),
    ),
    (
        "SCHEDULER_PROFILE",
        (
            "schedule",
            "poll_interval",
            "generation_interval",
            "revalidation_interval",
            "catch_up",
            "claim_order",
        ),
    ),
    (
        "EVIDENCE_FRESHNESS_PROFILE",
        (
            "freshness",
            "fresh_",
            "receipt_ttl",
            "terminal_ttl",
            "heartbeat_ttl",
            "evidence_ttl",
        ),
    ),
    (
        "MARKET_DATA_POLICY",
        (
            "download_overlap",
            "overlap_hours",
            "history_start",
            "market_data",
            "candle_limit",
            "gap_repair",
        ),
    ),
    (
        "DEPLOYMENT_CAPACITY_PROFILE",
        (
            "strategy_whitelist",
            "whitelist",
            "active_slot",
            "slot_limit",
            "slot_count",
            "max_active",
            "capacity",
            "pair_map",
        ),
    ),
    (
        "RISK_PROFILE",
        (
            "stake_amount",
            "stake_currency",
            "leverage",
            "exposure",
            "max_position",
            "max_open_trade",
            "position_limit",
            "price_deviation",
            "loss_limit",
            "stoploss",
        ),
    ),
    ("MONITORING_PROFILE", ("soak_days", "soak_period", "probe_interval", "probe_gap", "cache_ttl")),
    (
        "MODEL_PROVIDER_PROFILE",
        (
            "provider_key",
            "provider_name",
            "model_name",
            "default_model",
            "temperature",
            "max_tokens",
        ),
    ),
    ("SOURCE_DEFINITION", ("strategy_source", "trigger_source", "source_options", "source_label")),
    ("OPTIMIZATION_PROFILE", ("hyperopt", "optimizer", "epochs", "epoch_limit", "spaces", "loss_function")),
    (
        "TIMEFRAME_DEFINITION",
        ("timeframe", "timeframes", "supported_bars", "bar_mapping", "expected_interval"),
    ),
    ("RESEARCH_TARGET", ("research_pair", "allowed_pair", "instrument", "instruments", "symbols", "target_pair")),
    ("UI_PRESENTATION", ("display_label", "status_label", "profile_label", "default_sort", "presentation", "translation")),
)

PAIR_LITERAL = re.compile(r"^[A-Z0-9]{2,12}(?:/|-)USDT(?::USDT|-SWAP)?$")
TIMEFRAME_LITERAL = re.compile(r"^[1-9][0-9]*[mhdw]$")
WINDOW_LITERALS = frozenset({"oos", "walk_forward", "wf_bull", "wf_range", "wf_bear"})
REGIME_LITERALS = frozenset({"bull", "bear", "range"})
STRING_LITERAL_RE = re.compile(r"(?P<quote>['\"`])(?P<value>.*?)(?P=quote)")
NUMBER_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_])(?:0\.\d+|[1-9][0-9]*)(?![A-Za-z0-9_])")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    symbol: str
    fingerprint: str
    category: str
    language: str

    def identity(self) -> tuple[str, int, str, str, str]:
        return (self.path, self.line, self.symbol, self.fingerprint, self.category)


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[Finding, ...]
    unknown: tuple[Finding, ...]
    stale: tuple[Mapping[str, Any], ...]
    errors: tuple[str, ...]
    blocked_count: int
    technical_count: int

    @property
    def ok(self) -> bool:
        return not self.unknown and not self.stale and not self.errors


def _normalise_path(path: str | Path) -> str:
    return Path(path).as_posix().lstrip("./")


def is_excluded_path(path: str | Path) -> bool:
    normalised = _normalise_path(path)
    return any(fnmatch.fnmatchcase(normalised, pattern) for pattern in EXCLUDED_PATH_PATTERNS)


def _fingerprint(language: str, symbol: str, payload: str) -> str:
    canonical = "\x1f".join((language, symbol, payload))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalise_text(text: str) -> str:
    return " ".join(text.strip().split())


def _technical_disposition(category: str) -> bool:
    return category in TECHNICAL_CATEGORIES


def _baseline_entry(finding: Finding) -> dict[str, Any]:
    technical = _technical_disposition(finding.category)
    return {
        "path": finding.path,
        "line": finding.line,
        "symbol": finding.symbol,
        "fingerprint": finding.fingerprint,
        "category": finding.category,
        "disposition": "TECHNICAL" if technical else "BLOCKED",
        "reason": (
            "V1.3 technical/safety invariant reviewed at this exact syntax node."
            if technical
            else "Existing V1.3 business literal awaiting frozen bundle/profile replacement."
        ),
        "owner": "issue-704-old-rule-removal",
        "dependency": (
            "stable runtime safety and canonical bundle contract"
            if technical
            else "Task1 frozen configuration bundle/resolver contract"
        ),
    }


def _attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _attribute_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, (ast.Tuple, ast.List)):
        return ",".join(_attribute_name(item) for item in node.elts)
    return ""


def _literal_values(node: ast.AST) -> tuple[Any, ...]:
    """Return actual policy literals, excluding field names used as lookups."""

    values: list[Any] = []

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.Constant):
            if isinstance(current.value, (str, int, float, bool)):
                values.append(current.value)
            return
        if isinstance(current, ast.Subscript):
            visit(current.value)
            if not isinstance(current.slice, ast.Constant):
                visit(current.slice)
            return
        if isinstance(current, ast.Dict):
            for child in current.values:
                visit(child)
            return
        if isinstance(current, ast.Call) and _attribute_name(current.func).endswith(".get"):
            for child in current.args[1:]:
                visit(child)
            for keyword in current.keywords:
                visit(keyword.value)
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return tuple(values)


def _semantic_context(node: ast.AST) -> str:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.append(child.id)
        elif isinstance(child, ast.Attribute):
            names.append(child.attr)
        elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            if isinstance(child.slice.value, str):
                names.append(child.slice.value)
        elif isinstance(child, ast.Call) and _attribute_name(child.func).endswith(".get"):
            if child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
                names.append(child.args[0].value)
    return "_".join(names)


def _bound_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for child in node.elts for name in _bound_names(child))
    return ()


def _has_policy_literal(node: ast.AST) -> bool:
    return bool(_literal_values(node))


def _literal_category(values: Iterable[Any]) -> str | None:
    strings = [value.strip() for value in values if isinstance(value, str)]
    if any(PAIR_LITERAL.fullmatch(value) for value in strings):
        return "RESEARCH_TARGET"
    if any(TIMEFRAME_LITERAL.fullmatch(value) for value in strings):
        return "TIMEFRAME_DEFINITION"
    lowered = {value.lower() for value in strings}
    if lowered & WINDOW_LITERALS:
        return "VALIDATION_WINDOW"
    if lowered & REGIME_LITERALS:
        return "MARKET_REGIME"
    return None


def _category_for(context: str, values: Iterable[Any] = (), *, path: str = "") -> str | None:
    normalised = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "_", context.lower())
    materialised_values = tuple(values)
    if path == "backend/app/services/frozen_configuration_bundle.py" and any(
        token in normalised
        for token in (
            "bundle_digest_contract",
            "version_digest_contract",
            "resolution_contract",
            "required_capabilities",
            "sha256_pattern",
            "forbidden_secret_keys",
            "forbidden_executable_keys",
            "digest_contract",
            "allow_real_funds",
            "demo_only",
            "single_writer_required",
        )
    ):
        return "TECHNICAL_INVARIANT"
    if path.startswith("config/compatibility/"):
        if _literal_category(materialised_values) is not None or any(
            token in normalised
            for token in ("target", "market", "mode", "bar", "timeframe", "instrument", "interval")
        ):
            return "ADAPTER_CAPABILITY"
    expected_safety_value: bool | None = None
    if any(token in normalised for token in SAFETY_TRUE_TOKENS):
        expected_safety_value = True
    elif any(token in normalised for token in SAFETY_FALSE_TOKENS):
        expected_safety_value = False
    if expected_safety_value is not None:
        boolean_values = tuple(value for value in materialised_values if isinstance(value, bool))
        if boolean_values and all(value is expected_safety_value for value in boolean_values):
            return "SAFETY_INVARIANT"
        return "SAFETY_POLICY"
    for category, tokens in BUSINESS_CATEGORY_RULES:
        if any(token in normalised for token in tokens):
            return category
    return _literal_category(materialised_values)


class PythonBusinessConstantScanner(ast.NodeVisitor):
    def __init__(self, *, relative_path: str, source: str) -> None:
        self.relative_path = relative_path
        self.source = source
        self.scope: list[str] = ["<module>"]
        self.findings: list[Finding] = []
        self._seen: set[tuple[str, int, str, str, str]] = set()

    def _qualify(self, symbol: str) -> str:
        return ".".join((*self.scope, symbol))

    def _emit(self, node: ast.AST, symbol: str, category: str, payload: ast.AST | str) -> None:
        qualified = self._qualify(symbol)
        canonical = (
            ast.dump(payload, annotate_fields=True, include_attributes=False)
            if isinstance(payload, ast.AST)
            else _normalise_text(payload)
        )
        finding = Finding(
            path=self.relative_path,
            line=int(getattr(node, "lineno", 1)),
            symbol=qualified,
            fingerprint=_fingerprint("python", qualified, canonical),
            category=category,
            language="python",
        )
        if finding.identity() not in self._seen:
            self._seen.add(finding.identity())
            self.findings.append(finding)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        positional = [*node.args.posonlyargs, *node.args.args]
        positional_defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for argument, default in zip(positional, positional_defaults):
            if default is None or not _has_policy_literal(default):
                continue
            category = _category_for(argument.arg, _literal_values(default), path=self.relative_path)
            if category:
                self._emit(default, f"{node.name}.default:{argument.arg}", category, default)
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is None or not _has_policy_literal(default):
                continue
            category = _category_for(argument.arg, _literal_values(default), path=self.relative_path)
            if category:
                self._emit(default, f"{node.name}.default:{argument.arg}", category, default)
        self.scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if _has_policy_literal(node.value):
            for target in node.targets:
                for name in _bound_names(target):
                    category = _category_for(name, _literal_values(node.value), path=self.relative_path)
                    if category:
                        self._emit(node, f"assign:{name}", category, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None and _has_policy_literal(node.value):
            for name in _bound_names(node.target):
                category = _category_for(name, _literal_values(node.value), path=self.relative_path)
                if category:
                    self._emit(node, f"assign:{name}", category, node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if not _has_policy_literal(value):
                continue
            category = _category_for(key.value, _literal_values(value), path=self.relative_path)
            if category:
                self._emit(key, f"dict:{key.value}", category, value)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        values = _literal_values(node)
        if values:
            rendered = ast.unparse(node)
            context = _semantic_context(node)
            category = _category_for(context, values, path=self.relative_path)
            if category in {"RESEARCH_TARGET", "VALIDATION_WINDOW", "TIMEFRAME_DEFINITION"}:
                has_policy_value = _literal_category(values) == category or any(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in values
                )
                if not has_policy_value:
                    category = None
            if category:
                self._emit(node, f"compare:{_normalise_text(rendered)}", category, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        callable_name = _attribute_name(node.func)
        if callable_name.endswith(".get") and len(node.args) >= 2:
            key, default = node.args[0], node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and _has_policy_literal(default):
                category = _category_for(key.value, _literal_values(default), path=self.relative_path)
                if category:
                    self._emit(node, f"fallback:{key.value}", category, default)
        if callable_name.endswith("add_argument"):
            option = next(
                (
                    arg.value.lstrip("-").replace("-", "_")
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-")
                ),
                "argument",
            )
            for keyword in node.keywords:
                if keyword.arg not in {"default", "choices"} or not _has_policy_literal(keyword.value):
                    continue
                category = _category_for(option, _literal_values(keyword.value), path=self.relative_path)
                if category:
                    self._emit(keyword.value, f"argparse:{option}:{keyword.arg}", category, keyword.value)
        if callable_name == "range" and _has_policy_literal(node):
            line = ast.get_source_segment(self.source, node) or ast.unparse(node)
            category = _category_for(line, _literal_values(node), path=self.relative_path)
            if category:
                self._emit(node, f"call:{_normalise_text(line)}", category, node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            context = ast.unparse(node.value)
            category = _category_for(context, (node.slice.value,), path=self.relative_path)
            if category in {"VALIDATION_WINDOW", "TIMEFRAME_DEFINITION", "RESEARCH_TARGET"}:
                self._emit(node, f"index:{ast.unparse(node)}", category, node)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802
        context = ast.unparse(node.subject)
        for case in node.cases:
            values = _literal_values(case.pattern)
            if not values:
                continue
            category = _category_for(context, values, path=self.relative_path)
            if category:
                self._emit(case.pattern, f"match:{context}", category, case.pattern)
        self.generic_visit(node)


def scan_python(relative_path: str, source: str) -> tuple[Finding, ...]:
    tree = ast.parse(source, filename=relative_path)
    scanner = PythonBusinessConstantScanner(relative_path=relative_path, source=source)
    scanner.visit(tree)
    return tuple(sorted(scanner.findings))


def _text_symbol(line: str, scope: str) -> str:
    declaration = re.search(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", line)
    if declaration:
        return f"{scope}.assign:{declaration.group(1)}"
    property_name = re.match(r"\s*([A-Za-z_$][\w$]*)\s*:", line)
    if property_name:
        return f"{scope}.property:{property_name.group(1)}"
    return f"{scope}.line"


def scan_typescript(relative_path: str, source: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    seen: set[tuple[str, int, str, str, str]] = set()
    scope = "<module>"
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("/*") or line.startswith("*"):
            continue
        function = re.search(r"\bfunction\s+([A-Za-z_$][\w$]*)", line)
        if function:
            scope = function.group(1)
        values: list[Any] = [match.group("value") for match in STRING_LITERAL_RE.finditer(line)]
        values.extend(match.group(0) for match in NUMBER_LITERAL_RE.finditer(line))
        values.extend(re.findall(r"\b(?:true|false)\b", line))
        if not values:
            continue
        category = _category_for(line, values, path=relative_path)
        if re.match(r"\s*(?:label|detail)\s*:", raw_line) or re.match(
            r"\s*[A-Z][A-Z0-9_]+\s*:\s*['\"`]", raw_line
        ):
            category = "UI_PRESENTATION"
        if category is None:
            continue
        symbol = _text_symbol(raw_line, scope)
        finding = Finding(
            path=relative_path,
            line=line_number,
            symbol=symbol,
            fingerprint=_fingerprint("typescript", symbol, _normalise_text(raw_line)),
            category=category,
            language="typescript",
        )
        if finding.identity() not in seen:
            seen.add(finding.identity())
            findings.append(finding)
    return tuple(sorted(findings))


def _json_pointer(parts: Sequence[str | int]) -> str:
    if not parts:
        return "/"
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped)


def _scalar_sequence(value: Any) -> bool:
    return isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value)


def _structured_findings(
    *,
    relative_path: str,
    language: str,
    value: Any,
    line_for_pointer: Mapping[str, int],
) -> tuple[Finding, ...]:
    findings: list[Finding] = []

    def visit(current: Any, parts: list[str | int]) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                child_parts = [*parts, str(key)]
                pointer = _json_pointer(child_parts)
                if not isinstance(child, (dict, list)) or _scalar_sequence(child):
                    values = child if isinstance(child, list) else (child,)
                    category = _category_for(str(key), values, path=relative_path)
                    if category:
                        symbol = f"json-pointer:{pointer}"
                        payload = json.dumps(child, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        findings.append(
                            Finding(
                                path=relative_path,
                                line=line_for_pointer.get(pointer, 1),
                                symbol=symbol,
                                fingerprint=_fingerprint(language, symbol, payload),
                                category=category,
                                language=language,
                            )
                        )
                if isinstance(child, (dict, list)):
                    visit(child, child_parts)
        elif isinstance(current, list):
            for index, child in enumerate(current):
                visit(child, [*parts, index])

    visit(value, [])
    return tuple(sorted(set(findings)))


def _yaml_value_and_lines(source: str) -> tuple[Any, dict[str, int]]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to scan YAML business configuration")
    value = yaml.safe_load(source)
    document = yaml.compose(source)
    lines: dict[str, int] = {}

    def visit(node: Any, parts: list[str | int]) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = yaml.safe_load(key_node.value)
                child_parts = [*parts, str(key)]
                lines[_json_pointer(child_parts)] = key_node.start_mark.line + 1
                visit(value_node, child_parts)
        elif isinstance(node, yaml.SequenceNode):
            for index, child in enumerate(node.value):
                child_parts = [*parts, index]
                lines[_json_pointer(child_parts)] = child.start_mark.line + 1
                visit(child, child_parts)

    if document is not None:
        visit(document, [])
    return value, lines


def scan_yaml(relative_path: str, source: str) -> tuple[Finding, ...]:
    value, lines = _yaml_value_and_lines(source)
    return _structured_findings(
        relative_path=relative_path,
        language="yaml",
        value=value,
        line_for_pointer=lines,
    )


def _json_lines(source: str, value: Any) -> dict[str, int]:
    lines: dict[str, int] = {}
    key_positions: dict[str, list[int]] = {}
    for line_number, line in enumerate(source.splitlines(), start=1):
        for match in re.finditer(r'"(?P<key>(?:[^"\\]|\\.)+)"\s*:', line):
            key = json.loads(f'"{match.group("key")}"')
            key_positions.setdefault(key, []).append(line_number)
    offsets: dict[str, int] = {}

    def visit(current: Any, parts: list[str | int]) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                positions = key_positions.get(str(key), [1])
                offset = offsets.get(str(key), 0)
                lines[_json_pointer([*parts, str(key)])] = positions[min(offset, len(positions) - 1)]
                offsets[str(key)] = offset + 1
                visit(child, [*parts, str(key)])
        elif isinstance(current, list):
            for index, child in enumerate(current):
                visit(child, [*parts, index])

    visit(value, [])
    return lines


def scan_json(relative_path: str, source: str) -> tuple[Finding, ...]:
    value = json.loads(source)
    return _structured_findings(
        relative_path=relative_path,
        language="json",
        value=value,
        line_for_pointer=_json_lines(source, value),
    )


def scan_source(relative_path: str, source: str) -> tuple[Finding, ...]:
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".py":
        return scan_python(relative_path, source)
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
        return scan_typescript(relative_path, source)
    if suffix in {".yaml", ".yml"}:
        return scan_yaml(relative_path, source)
    if suffix == ".json":
        return scan_json(relative_path, source)
    raise ValueError(f"unsupported audit target type: {relative_path}")


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("audit manifest must be a JSON object")
    return payload


def _validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        errors.append(f"schema_version must be {MANIFEST_SCHEMA!r}")
    if not isinstance(manifest.get("baseline_commit"), str) or not manifest.get("baseline_commit"):
        errors.append("baseline_commit must be a non-empty string")
    targets = manifest.get("scan_targets")
    if not isinstance(targets, list) or not targets:
        errors.append("scan_targets must be a non-empty list of exact paths")
        targets = []
    normalised_targets: list[str] = []
    for target in targets:
        if not isinstance(target, str) or not target:
            errors.append("every scan target must be a non-empty string")
            continue
        normalised = _normalise_path(target)
        if target != normalised or any(character in target for character in "*?[]") or Path(target).is_absolute():
            errors.append(f"scan target must be one exact repository-relative path: {target!r}")
        if is_excluded_path(normalised):
            errors.append(f"excluded evidence/fixture path cannot be a scan target: {normalised}")
        normalised_targets.append(normalised)
    if len(normalised_targets) != len(set(normalised_targets)):
        errors.append("scan_targets contains duplicates")

    entries = manifest.get("findings")
    if not isinstance(entries, list):
        errors.append("findings must be a list")
        return errors
    required = {
        "path",
        "line",
        "symbol",
        "fingerprint",
        "category",
        "disposition",
        "reason",
        "owner",
        "dependency",
    }
    identities: set[tuple[Any, ...]] = set()
    for index, entry in enumerate(entries):
        prefix = f"findings[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = required - entry.keys()
        if missing:
            errors.append(f"{prefix} is missing: {', '.join(sorted(missing))}")
            continue
        path = _normalise_path(str(entry["path"]))
        if path not in normalised_targets:
            errors.append(f"{prefix}.path is not an exact scan target: {path}")
        if not isinstance(entry["line"], int) or entry["line"] < 1:
            errors.append(f"{prefix}.line must be a positive integer")
        if not isinstance(entry["symbol"], str) or not entry["symbol"]:
            errors.append(f"{prefix}.symbol must be non-empty")
        if not isinstance(entry["fingerprint"], str) or not FINGERPRINT_RE.fullmatch(entry["fingerprint"]):
            errors.append(f"{prefix}.fingerprint must be an exact SHA256 fingerprint")
        category = entry["category"]
        disposition = entry["disposition"]
        if disposition not in {"BLOCKED", "TECHNICAL"}:
            errors.append(f"{prefix}.disposition must be BLOCKED or TECHNICAL")
        if disposition == "TECHNICAL" and category not in TECHNICAL_CATEGORIES:
            errors.append(f"{prefix} disguises business category {category!r} as TECHNICAL")
        for field in ("reason", "owner", "dependency"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                errors.append(f"{prefix}.{field} must be non-empty")
        identity = (
            path,
            entry["line"],
            entry["symbol"],
            entry["fingerprint"],
            category,
        )
        if identity in identities:
            errors.append(f"{prefix} duplicates an earlier exact finding")
        identities.add(identity)
    return errors


def scan_repository(root: Path, scan_targets: Sequence[str]) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
    findings: list[Finding] = []
    errors: list[str] = []
    for target in scan_targets:
        relative_path = _normalise_path(target)
        if is_excluded_path(relative_path):
            errors.append(f"refusing to scan excluded historical/fixture path: {relative_path}")
            continue
        path = root / relative_path
        if not path.is_file():
            errors.append(f"scan target is missing: {relative_path}")
            continue
        try:
            findings.extend(scan_source(relative_path, path.read_text(encoding="utf-8")))
        except (SyntaxError, ValueError, RuntimeError) as exc:
            errors.append(f"unable to scan {relative_path}: {exc}")
    return tuple(sorted(set(findings))), tuple(errors)


def audit_repository(root: Path, manifest: Mapping[str, Any]) -> AuditReport:
    errors = _validate_manifest(manifest)
    targets = manifest.get("scan_targets") if isinstance(manifest.get("scan_targets"), list) else []
    findings, scan_errors = scan_repository(root, targets)
    errors.extend(scan_errors)

    baseline_entries = manifest.get("findings") if isinstance(manifest.get("findings"), list) else []
    baseline: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for entry in baseline_entries:
        if not isinstance(entry, dict) or not {
            "path", "line", "symbol", "fingerprint", "category"
        }.issubset(entry):
            continue
        identity = (
            _normalise_path(str(entry["path"])),
            entry["line"],
            entry["symbol"],
            entry["fingerprint"],
            entry["category"],
        )
        baseline[identity] = entry

    observed = {finding.identity(): finding for finding in findings}
    unknown = tuple(sorted(finding for identity, finding in observed.items() if identity not in baseline))
    stale = tuple(baseline[identity] for identity in sorted(baseline.keys() - observed.keys()))
    blocked_count = sum(entry.get("disposition") == "BLOCKED" for entry in baseline_entries if isinstance(entry, dict))
    technical_count = sum(
        entry.get("disposition") == "TECHNICAL" for entry in baseline_entries if isinstance(entry, dict)
    )
    return AuditReport(
        findings=findings,
        unknown=unknown,
        stale=stale,
        errors=tuple(errors),
        blocked_count=blocked_count,
        technical_count=technical_count,
    )


def _finding_json(finding: Finding) -> dict[str, Any]:
    return asdict(finding)


def _print_human(report: AuditReport) -> None:
    state = "PASS" if report.ok else "FAIL"
    print(
        f"V1.3 business-constant audit: {state}; "
        f"observed={len(report.findings)} blocked_baseline={report.blocked_count} "
        f"technical_baseline={report.technical_count} unknown={len(report.unknown)} "
        f"stale={len(report.stale)} errors={len(report.errors)}"
    )
    for error in report.errors:
        print(f"ERROR {error}")
    for finding in report.unknown:
        print(
            f"UNKNOWN {finding.path}:{finding.line} {finding.symbol} "
            f"{finding.category} {finding.fingerprint}"
        )
    for entry in report.stale:
        print(
            f"STALE {entry.get('path')}:{entry.get('line')} {entry.get('symbol')} "
            f"{entry.get('category')} {entry.get('fingerprint')}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="print exact observed findings without treating them as accepted baseline",
    )
    parser.add_argument(
        "--bootstrap-manifest",
        action="store_true",
        help="print a reviewed-baseline template; redirect explicitly after inspecting it",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest or root / DEFAULT_MANIFEST
    try:
        manifest = load_manifest(manifest_path)
        report = audit_repository(root, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"V1.3 business-constant audit: FAIL; {exc}", file=sys.stderr)
        return 2

    if args.bootstrap_manifest:
        print(
            json.dumps(
                {
                    "schema_version": MANIFEST_SCHEMA,
                    "baseline_commit": str(manifest.get("baseline_commit", "UNSET")),
                    "scan_targets": list(manifest.get("scan_targets", [])),
                    "findings": [_baseline_entry(item) for item in report.findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.discover:
        print(json.dumps([_finding_json(item) for item in report.findings], ensure_ascii=False, indent=2))
        return 0
    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "observed_count": len(report.findings),
                    "blocked_baseline_count": report.blocked_count,
                    "technical_baseline_count": report.technical_count,
                    "unknown": [_finding_json(item) for item in report.unknown],
                    "stale": list(report.stale),
                    "errors": list(report.errors),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_human(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
