from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union


DEFAULT_SCAN_PATHS = (
    ".env.example",
    ".github/workflows",
    "backend/app",
    "backend/tests/fixtures",
    "config",
    "docs",
    "frontend/src",
    "reports",
    "scripts",
    "README.md",
)

TEXT_SUFFIXES = {
    "",
    ".css",
    ".env",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

SKIP_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}

KEY_VALUE_PATTERN = re.compile(
    r"[\"'`]?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"[\"'`]?"
    r"\s*(?P<separator>[:=])\s*"
    r"(?P<value>[^,#}\n]+)",
    re.IGNORECASE,
)
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class SecretScanFinding:
    path: str
    line_number: int
    key: str
    rule_id: str = "secret-shaped-assignment"
    category: str = "secret"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "key": self.key,
            "rule_id": self.rule_id,
            "category": self.category,
        }


@dataclass(frozen=True)
class SecretScanReport:
    scanned_files: int
    findings: tuple[SecretScanFinding, ...]

    @property
    def status(self) -> str:
        return "BLOCKED" if self.findings else "PASS"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "scanned_files": self.scanned_files,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def scan_repo_for_secrets(
    repo_root: Path,
    scan_paths: Optional[Sequence[Union[str, Path]]] = None,
    tracked_only: bool = True,
) -> SecretScanReport:
    root = repo_root.resolve()
    candidates = list(_iter_candidate_files(root, scan_paths or DEFAULT_SCAN_PATHS, tracked_only))
    findings: list[SecretScanFinding] = []

    for path in candidates:
        rel_path = path.relative_to(root).as_posix()
        for line_number, line in _iter_text_lines(path):
            for finding in _scan_line(rel_path, line_number, line):
                findings.append(finding)

    return SecretScanReport(scanned_files=len(candidates), findings=tuple(findings))


def format_secret_scan_report(report: SecretScanReport) -> str:
    if not report.findings:
        return f"PASS: scanned {report.scanned_files} files; no secret-shaped values found."

    lines = [
        f"BLOCKED: found {len(report.findings)} secret-shaped assignments "
        f"across {report.scanned_files} scanned files."
    ]
    for finding in report.findings:
        lines.append(
            f"{finding.path}:{finding.line_number}: key={finding.key} "
            f"rule={finding.rule_id}"
        )
    return "\n".join(lines)


def _iter_candidate_files(
    repo_root: Path,
    scan_paths: Sequence[Union[str, Path]],
    tracked_only: bool,
) -> Iterable[Path]:
    if tracked_only:
        tracked = _git_tracked_files(repo_root, scan_paths)
        if tracked is not None:
            for path in tracked:
                if _should_scan_path(path):
                    yield path
            return

    for raw_path in scan_paths:
        path = (repo_root / raw_path).resolve()
        if not _is_relative_to(path, repo_root) or not path.exists():
            continue
        if path.is_file():
            if _should_scan_path(path):
                yield path
            continue
        for child in path.rglob("*"):
            if child.is_file() and _should_scan_path(child):
                yield child


def _git_tracked_files(
    repo_root: Path,
    scan_paths: Sequence[Union[str, Path]],
) -> Optional[list[Path]]:
    command = ["git", "-C", str(repo_root), "ls-files", "--"]
    command.extend(str(path) for path in scan_paths)
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        candidate = (repo_root / line).resolve()
        if _is_relative_to(candidate, repo_root) and candidate.exists():
            paths.append(candidate)
    return paths


def _should_scan_path(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return False
    if path.name in {"package-lock.json"}:
        return False
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Makefile", "Dockerfile"}:
        return False
    try:
        return path.stat().st_size <= 1_000_000
    except OSError:
        return False


def _iter_text_lines(path: Path) -> Iterable[tuple[int, str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if b"\x00" in raw:
        return
    text = raw.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), start=1):
        yield line_number, line


def _scan_line(path: str, line_number: int, line: str) -> Iterable[SecretScanFinding]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    for match in KEY_VALUE_PATTERN.finditer(line):
        key = match.group("key")
        raw_value = match.group("value").strip()
        if not _is_secret_key(key):
            continue
        if _is_allowed_secret_reference(key, raw_value, line):
            continue
        yield SecretScanFinding(path=path, line_number=line_number, key=key)


def _is_allowed_secret_reference(key: str, raw_value: str, line: str) -> bool:
    key_normalized = _normalize(key)
    value = _clean_value(raw_value)
    value_normalized = _normalize(value)

    if not value or value in {":", "="}:
        return True
    if _looks_like_type_annotation(value):
        return True
    if _looks_like_code_expression(value):
        return True
    if key_normalized.endswith("_env") or "api_key_env" in key_normalized or "api_secret_env" in key_normalized:
        return _is_env_reference(value)
    if _is_env_reference(value):
        return True
    if _is_placeholder_value(value_normalized):
        return True
    if "fixture" in value_normalized or "fake" in value_normalized:
        return True
    if "test" in value_normalized or "mock" in value_normalized or "dummy" in value_normalized:
        return True
    if "must_not" in value_normalized or "should_not" in value_normalized:
        return True
    if "non_secret" in value_normalized or "not_a_secret" in value_normalized:
        return True
    return False


def _clean_value(raw_value: str) -> str:
    value = raw_value.strip()
    value = value.split(" #", 1)[0].strip()
    while value.endswith((",", ";", ")")):
        value = value[:-1].strip()
    return value.strip("\"'`")


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _is_secret_key(key: str) -> bool:
    normalized = _normalize(key)
    parts = normalized.split("_")
    if normalized.startswith(("hide_", "redact_", "masked_")):
        return False
    if normalized.endswith(("_finding", "_findings", "_line", "_lines", "_path", "_paths")):
        return False
    if "api_key" in normalized or "apikey" in normalized:
        return True
    if "api_secret" in normalized or "apisecret" in normalized:
        return True
    if "private_key" in normalized or "privatekey" in normalized:
        return True
    if "authorization" in normalized:
        return True
    if "password" in parts or "passphrase" in parts or "secret" in parts:
        return True
    token_keys = {
        "token",
        "api_token",
        "auth_token",
        "access_token",
        "refresh_token",
        "bearer_token",
        "raw_token",
    }
    return normalized in token_keys or normalized.endswith(
        ("_api_token", "_auth_token", "_access_token", "_refresh_token", "_bearer_token")
    )


def _looks_like_type_annotation(value: str) -> bool:
    normalized = value.strip()
    safe_literals = {
        "str",
        "string",
        "bool",
        "boolean",
        "int",
        "number",
        "float",
        "Any",
        "None",
        "null",
        "true",
        "false",
    }
    if normalized in safe_literals:
        return True
    if "->" in normalized:
        return True
    if normalized.startswith(("str)", "int)", "float)", "bool)", "string)", "number)")):
        return True
    prefixes = (
        "Optional[",
        "Literal[",
        "Field(",
        "list[",
        "dict[",
        "tuple[",
        "set[",
        "frozenset(",
        "Sequence[",
        "Mapping[",
        "Union[",
    )
    return normalized.startswith(prefixes)


def _looks_like_code_expression(value: str) -> bool:
    lowered = value.lower()
    prefixes = (
        "os.environ",
        "os.getenv",
        "settings.",
        "self.",
        "value.",
        "payload.",
        "re.compile",
        "field(",
        "field.default",
    )
    if lowered.startswith(prefixes):
        return True
    if value.startswith(("[", "(", "{")):
        return True
    if "{" in value or "}" in value:
        return True
    if "+" in value and ("\"" in value or "'" in value):
        return True
    return False


def _is_env_reference(value: str) -> bool:
    stripped = value.strip()
    if ENV_NAME_PATTERN.fullmatch(stripped):
        return True
    if stripped.startswith("${") and stripped.endswith("}"):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return ENV_NAME_PATTERN.fullmatch(stripped[1:-1].strip()) is not None
    if stripped.startswith("$") and ENV_NAME_PATTERN.fullmatch(stripped[1:]):
        return True
    if stripped.startswith("env:"):
        return ENV_NAME_PATTERN.fullmatch(stripped.split(":", 1)[1].strip()) is not None
    return False


def _is_placeholder_value(normalized_value: str) -> bool:
    placeholders = {
        "change_me",
        "changeme",
        "replace_me",
        "replace_me_locally",
        "redacted",
        "placeholder",
        "example",
        "sample",
        "your_api_key",
        "your_api_secret",
        "your_passphrase",
        "your_token",
        "none",
        "null",
    }
    if normalized_value in placeholders:
        return True
    return normalized_value.startswith(("replace_", "example_", "placeholder_", "your_"))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
