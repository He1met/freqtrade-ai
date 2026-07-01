import ast
from typing import Optional

from app.schemas.strategy_failure_reason import StrategyFailureReasonCreate
from app.schemas.strategy_static_review import (
    StrategyStaticReviewCategory,
    StrategyStaticReviewFinding,
    StrategyStaticReviewResult,
    StrategyStaticReviewSeverity,
)

# Static review is a deterministic pre-flight check for generated strategy
# code. It is not a sandbox and does not prove a strategy is safe to trade; it
# blocks obvious policy violations before code reaches Freqtrade backtesting.

FORBIDDEN_IMPORTS: dict[str, tuple[StrategyStaticReviewCategory, str]] = {
    "aiohttp": ("network_access", "Network clients are not allowed in generated strategies."),
    "dotenv": ("secret_access", "Loading secrets from files is not allowed in strategies."),
    "ftplib": ("network_access", "Network clients are not allowed in generated strategies."),
    "httpx": ("network_access", "Network clients are not allowed in generated strategies."),
    "os": ("secret_access", "Environment and filesystem access are not allowed in strategies."),
    "pathlib": ("file_access", "Filesystem access is not allowed in generated strategies."),
    "requests": ("network_access", "Network clients are not allowed in generated strategies."),
    "shutil": ("file_access", "Filesystem access is not allowed in generated strategies."),
    "socket": ("network_access", "Network clients are not allowed in generated strategies."),
    "subprocess": ("dangerous_call", "Spawning processes is not allowed in strategies."),
    "urllib": ("network_access", "Network clients are not allowed in generated strategies."),
}

DANGEROUS_CALLS: dict[str, tuple[StrategyStaticReviewCategory, str]] = {
    "__import__": ("dangerous_call", "Dynamic imports are not allowed in generated strategies."),
    "compile": ("dangerous_call", "Runtime code compilation is not allowed in strategies."),
    "eval": ("dangerous_call", "Runtime evaluation is not allowed in generated strategies."),
    "exec": ("dangerous_call", "Runtime code execution is not allowed in generated strategies."),
    "open": ("file_access", "Direct file access is not allowed in generated strategies."),
    "os.getenv": ("secret_access", "Strategy code must not read secrets from environment variables."),
    "os.environ.get": ("secret_access", "Strategy code must not read secrets from environment variables."),
    "subprocess.call": ("dangerous_call", "Spawning processes is not allowed in strategies."),
    "subprocess.run": ("dangerous_call", "Spawning processes is not allowed in strategies."),
}

NETWORK_CALL_SUFFIXES = (
    ".delete",
    ".get",
    ".patch",
    ".post",
    ".put",
    ".request",
)
NETWORK_CALL_PREFIXES = ("requests", "httpx", "urllib", "socket", "aiohttp", "ftplib")
FILE_CALL_SUFFIXES = (".read_text", ".write_text", ".read_bytes", ".write_bytes")


class StrategyStaticReviewService:
    """Runs AST-based policy checks and converts findings into failure reasons."""

    def review_code(
        self,
        code: str,
        filename: str = "generated_strategy.py",
    ) -> StrategyStaticReviewResult:
        try:
            tree = ast.parse(code, filename=filename)
        except SyntaxError as exc:
            finding = StrategyStaticReviewFinding(
                rule_id="syntax.parse",
                category="syntax_error",
                severity="error",
                message=f"Strategy code has invalid Python syntax: {exc.msg}.",
                line=exc.lineno,
                column=exc.offset,
                details={"filename": filename},
            )
            return self._result([finding])

        visitor = _StaticReviewVisitor()
        visitor.visit(tree)
        return self._result(visitor.findings)

    def build_failure_reasons(
        self,
        strategy_id: int,
        strategy_version_id: int,
        result: StrategyStaticReviewResult,
    ) -> list[StrategyFailureReasonCreate]:
        return [
            StrategyFailureReasonCreate(
                strategy_id=strategy_id,
                strategy_version_id=strategy_version_id,
                stage="static_check",
                reason_type="static_policy_violation",
                severity=finding.severity,
                message=finding.message,
                details={
                    "rule_id": finding.rule_id,
                    "category": finding.category,
                    "line": finding.line,
                    "column": finding.column,
                    **finding.details,
                },
            )
            for finding in result.findings
        ]

    def _result(self, findings: list[StrategyStaticReviewFinding]) -> StrategyStaticReviewResult:
        summary = {"errors": 0, "warnings": 0}
        for finding in findings:
            if finding.severity == "error":
                summary["errors"] += 1
            else:
                summary["warnings"] += 1
        return StrategyStaticReviewResult(
            passed=summary["errors"] == 0,
            findings=findings,
            summary=summary,
        )


class _StaticReviewVisitor(ast.NodeVisitor):
    """Collects static findings from syntax tree nodes.

    The visitor favors explicit, high-signal rules over broad string matching
    so generated strategy code can be reviewed without executing it.
    """

    def __init__(self) -> None:
        self.findings: list[StrategyStaticReviewFinding] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root_name = alias.name.split(".", maxsplit=1)[0]
            self._record_forbidden_import(root_name, alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root_name = node.module.split(".", maxsplit=1)[0]
            self._record_forbidden_import(root_name, node.module, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _node_name(node.func)
        if call_name:
            self._record_dangerous_call(call_name, node)
            self._record_network_call(call_name, node)
            self._record_file_call(call_name, node)
            self._record_shift_lookahead(call_name, node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attribute_name = _node_name(node)
        if attribute_name == "os.environ":
            self._add_finding(
                rule_id="secret.env_access",
                category="secret_access",
                severity="error",
                message="Strategy code must not access environment variables.",
                node=node,
                details={"attribute": attribute_name},
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        value_name = _node_name(node.value)
        if value_name and value_name.endswith(".iloc") and _is_negative_index(node.slice):
            self._add_finding(
                rule_id="lookahead.iloc_negative",
                category="lookahead_bias",
                severity="error",
                message="Negative iloc indexing can read future candles during backtests.",
                node=node,
                details={"accessor": value_name},
            )
        self.generic_visit(node)

    def _record_forbidden_import(self, root_name: str, import_name: str, node: ast.AST) -> None:
        policy = FORBIDDEN_IMPORTS.get(root_name)
        if policy is None:
            return
        category, message = policy
        self._add_finding(
            rule_id=f"import.{root_name}",
            category=category,
            severity="error",
            message=message,
            node=node,
            details={"import": import_name},
        )

    def _record_dangerous_call(self, call_name: str, node: ast.AST) -> None:
        policy = DANGEROUS_CALLS.get(call_name)
        if policy is None:
            return
        category, message = policy
        self._add_finding(
            rule_id=f"call.{call_name}",
            category=category,
            severity="error",
            message=message,
            node=node,
            details={"call": call_name},
        )

    def _record_network_call(self, call_name: str, node: ast.AST) -> None:
        # Call detection complements forbidden imports because a strategy could
        # receive a network client through another name in future extensions.
        if not call_name.startswith(NETWORK_CALL_PREFIXES):
            return
        if not call_name.endswith(NETWORK_CALL_SUFFIXES) and call_name not in NETWORK_CALL_PREFIXES:
            return
        self._add_finding(
            rule_id="call.network_client",
            category="network_access",
            severity="error",
            message="Network calls are not allowed in generated strategies.",
            node=node,
            details={"call": call_name},
        )

    def _record_file_call(self, call_name: str, node: ast.AST) -> None:
        if call_name in {"read_text", "write_text", "read_bytes", "write_bytes"} or call_name.endswith(
            FILE_CALL_SUFFIXES
        ):
            self._add_finding(
                rule_id="call.file_access",
                category="file_access",
                severity="error",
                message="Filesystem reads and writes are not allowed in generated strategies.",
                node=node,
                details={"call": call_name},
            )

    def _record_shift_lookahead(self, call_name: str, node: ast.Call) -> None:
        # Negative shift is a common lookahead-bias pattern in dataframe-based
        # strategies because it can reference future candles.
        if call_name != "shift" and not call_name.endswith(".shift"):
            return
        periods = _first_call_argument(node, "periods")
        if _is_negative_number(periods):
            self._add_finding(
                rule_id="lookahead.shift_negative",
                category="lookahead_bias",
                severity="error",
                message="Negative shift can introduce lookahead bias by reading future candles.",
                node=node,
                details={"call": call_name},
            )

    def _add_finding(
        self,
        rule_id: str,
        category: StrategyStaticReviewCategory,
        severity: StrategyStaticReviewSeverity,
        message: str,
        node: ast.AST,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.findings.append(
            StrategyStaticReviewFinding(
                rule_id=rule_id,
                category=category,
                severity=severity,
                message=message,
                line=getattr(node, "lineno", None),
                column=getattr(node, "col_offset", None),
                details=details or {},
            )
        )


def _node_name(node: ast.AST) -> Optional[str]:
    """Return a dotted name for simple Name/Attribute expressions."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent_name = _node_name(node.value)
        if parent_name is None:
            return node.attr
        return f"{parent_name}.{node.attr}"
    return None


def _first_call_argument(node: ast.Call, keyword_name: str) -> Optional[ast.AST]:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    return None


def _is_negative_number(node: Optional[ast.AST]) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float))
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value < 0
    return False


def _is_negative_index(node: ast.AST) -> bool:
    if _is_negative_number(node):
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_negative_number(element) for element in node.elts)
    if isinstance(node, ast.Slice):
        return _is_negative_number(node.lower) or _is_negative_number(node.upper)
    return False
