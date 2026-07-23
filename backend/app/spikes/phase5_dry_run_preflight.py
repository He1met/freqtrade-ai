from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Mapping, Optional

from app.adapters.freqtrade.binary import resolve_freqtrade_binary
from app.core.config import REPO_ROOT
from app.core.paths import resolve_repo_path


DEFAULT_REQUIRED_ENV_VARS = (
    "FREQTRADE_DRY_RUN_EXCHANGE",
    "FREQTRADE_DRY_RUN_PAIR",
    "FREQTRADE_DRY_RUN_TIMEFRAME",
    "FREQTRADE_DRY_RUN_API_KEY",
    "FREQTRADE_DRY_RUN_API_SECRET",
)
DEFAULT_OPTIONAL_ENV_VARS = ("FREQTRADE_DRY_RUN_API_PASSPHRASE",)
SECRET_FILE_SUFFIXES = {
    ".env",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
}
SECRET_KEY_PATTERN = re.compile(
    r"(?P<key>api[_-]?key|api[_-]?secret|secret|token|passphrase|password|private[_-]?key)"
    r"[\s\"']*[:=][\s\"']*(?P<value>[^\"',}\]\s#]+)",
    re.IGNORECASE,
)
PLACEHOLDER_VALUES = {
    "",
    "change_me",
    "changeme",
    "dummy",
    "env",
    "env_only",
    "example",
    "false",
    "none",
    "not_set",
    "null",
    "placeholder",
    "redacted",
    "test",
    "true",
    "xxx",
    "your_api_key",
    "your_api_secret",
    "your_passphrase",
}


@dataclass(frozen=True)
class DryRunPreflightConfig:
    tmp_dir: Path = Path("/tmp/freqtrade-ai-phase5-preflight")
    report_path: Path = Path("reports/spikes/phase5_dry_run_preflight_latest.md")
    user_data_dir: Path = Path("user_data")
    freqtrade_binary: Optional[str] = None
    required_env_vars: tuple[str, ...] = DEFAULT_REQUIRED_ENV_VARS
    optional_env_vars: tuple[str, ...] = DEFAULT_OPTIONAL_ENV_VARS
    secret_scan_paths: tuple[Path, ...] | None = None


@dataclass(frozen=True)
class SecretFinding:
    path: Path
    line_number: int
    key: str


@dataclass
class DryRunPreflightReport:
    status: str = "PENDING"
    report_path: Optional[Path] = None
    tmp_dir: Optional[Path] = None
    temp_config_dir: Optional[Path] = None
    freqtrade_binary: Optional[Path] = None
    user_data_dir: Optional[Path] = None
    data_dir: Optional[Path] = None
    strategies_dir: Optional[Path] = None
    local_data_file_count: int = 0
    required_env_present: list[str] = field(default_factory=list)
    required_env_missing: list[str] = field(default_factory=list)
    optional_env_present: list[str] = field(default_factory=list)
    optional_env_missing: list[str] = field(default_factory=list)
    secret_findings: list[SecretFinding] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def find_freqtrade_binary(explicit_binary: Optional[str] = None) -> Optional[Path]:
    environ = {"FREQTRADE_BINARY": explicit_binary} if explicit_binary else None
    resolution = resolve_freqtrade_binary(environ=environ)
    return resolution.resolved_path if resolution.ready else None


def run_preflight(
    config: DryRunPreflightConfig,
    environ: Mapping[str, str] | None = None,
) -> DryRunPreflightReport:
    env = environ if environ is not None else {}
    if environ is None:
        import os

        env = os.environ

    report = DryRunPreflightReport()
    report.report_path = resolve_repo_path(config.report_path)
    report.tmp_dir = config.tmp_dir.expanduser().resolve()
    report.temp_config_dir = report.tmp_dir / "freqtrade_configs"

    try:
        report.temp_config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.failures.append(f"temporary config directory is not writable: {exc}")

    report.freqtrade_binary = find_freqtrade_binary(config.freqtrade_binary)
    if report.freqtrade_binary is None:
        report.blockers.append("freqtrade command was not found")

    user_data_dir = resolve_repo_path(config.user_data_dir)
    data_dir = user_data_dir / "data"
    strategies_dir = user_data_dir / "strategies"
    report.user_data_dir = user_data_dir
    report.data_dir = data_dir
    report.strategies_dir = strategies_dir

    for label, path in (
        ("user_data directory", user_data_dir),
        ("user_data/data directory", data_dir),
        ("user_data/strategies directory", strategies_dir),
    ):
        if not path.exists() or not path.is_dir():
            report.blockers.append(f"{label} is missing: {path}")

    report.local_data_file_count = count_local_data_files(data_dir)
    if data_dir.exists() and report.local_data_file_count == 0:
        report.blockers.append(f"no local market data files found under {data_dir}")

    report.required_env_present, report.required_env_missing = split_env_presence(
        config.required_env_vars,
        env,
    )
    report.optional_env_present, report.optional_env_missing = split_env_presence(
        config.optional_env_vars,
        env,
    )
    if report.required_env_missing:
        missing = ", ".join(report.required_env_missing)
        report.blockers.append(f"required ENV variables are missing or empty: {missing}")

    scan_paths = (
        tuple(default_secret_scan_paths(REPO_ROOT))
        if config.secret_scan_paths is None
        else config.secret_scan_paths
    )
    report.secret_findings = find_secret_shaped_config_values(scan_paths)
    if report.secret_findings:
        report.failures.append(
            "secret-shaped values were found in repository-controlled config files"
        )

    if report.failures:
        report.status = "FAILED"
    elif report.blockers:
        report.status = "BLOCKED"
    else:
        report.status = "PASS"

    write_report(report)
    return report


def split_env_presence(
    names: tuple[str, ...],
    env: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    present = []
    missing = []
    for name in names:
        value = env.get(name)
        if value is None or value.strip() == "":
            missing.append(name)
        else:
            present.append(name)
    return present, missing


def count_local_data_files(data_dir: Path) -> int:
    if not data_dir.exists() or not data_dir.is_dir():
        return 0
    supported_suffixes = {".feather", ".json", ".json.gz", ".parquet"}
    count = 0
    for path in data_dir.rglob("*"):
        if path.is_file() and any(path.name.endswith(suffix) for suffix in supported_suffixes):
            count += 1
    return count


def default_secret_scan_paths(repo_root: Path) -> list[Path]:
    tracked_files = git_tracked_files(repo_root)
    if not tracked_files:
        return []
    return [
        path
        for path in tracked_files
        if should_scan_for_secret_values(path)
    ]


def git_tracked_files(repo_root: Path) -> list[Path]:
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return [
        repo_root / line
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def should_scan_for_secret_values(path: Path) -> bool:
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    if path.suffix.lower() in SECRET_FILE_SUFFIXES:
        return True
    return any(part in {"config", "configs"} for part in path.parts)


def find_secret_shaped_config_values(paths: tuple[Path, ...] | list[Path]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        if path.stat().st_size > 512 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            match = SECRET_KEY_PATTERN.search(line)
            if not match:
                continue
            if is_placeholder_secret_value(match.group("value")):
                continue
            findings.append(
                SecretFinding(
                    path=path,
                    line_number=index,
                    key=match.group("key"),
                )
            )
    return findings


def is_placeholder_secret_value(value: str) -> bool:
    normalized = value.strip().strip("\"'").strip()
    lowered = normalized.lower()
    if lowered in PLACEHOLDER_VALUES:
        return True
    if lowered.startswith("${"):
        return True
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    if set(lowered) <= {"*", "x"}:
        return True
    return False


def write_report(report: DryRunPreflightReport) -> Path:
    if report.report_path is None:
        raise ValueError("report_path is required")
    report.report_path.parent.mkdir(parents=True, exist_ok=True)
    report.report_path.write_text(render_report(report), encoding="utf-8")
    return report.report_path


def render_report(report: DryRunPreflightReport) -> str:
    def value(item: object) -> str:
        return str(item) if item is not None else "not available"

    def bullet(items: list[str]) -> list[str]:
        return [f"- {item}" for item in items] or ["- none"]

    secret_lines = [
        f"- {finding.path}:{finding.line_number} key={finding.key}"
        for finding in report.secret_findings
    ] or ["- none"]

    payload = {
        "required_env_present": report.required_env_present,
        "required_env_missing": report.required_env_missing,
        "optional_env_present": report.optional_env_present,
        "optional_env_missing": report.optional_env_missing,
    }

    lines = [
        "# Phase 5 Dry-run Preflight Report",
        "",
        f"- Status: {report.status}",
        f"- Report path: {value(report.report_path)}",
        f"- Temporary workspace: {value(report.tmp_dir)}",
        f"- Temporary config directory: {value(report.temp_config_dir)}",
        f"- Freqtrade command: {value(report.freqtrade_binary)}",
        f"- user_data directory: {value(report.user_data_dir)}",
        f"- user_data/data directory: {value(report.data_dir)}",
        f"- user_data/strategies directory: {value(report.strategies_dir)}",
        f"- Local data file count: {report.local_data_file_count}",
        "",
        "## ENV Readiness",
        "",
        "Only variable names are reported. Values are never printed.",
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
        "## Secret Scan",
        "",
        "Only file path, line number, and key name are reported. Values are never printed.",
        "",
        *secret_lines,
        "",
        "## Safety Boundary",
        "",
        "- This preflight does not start Freqtrade dry-run.",
        "- This preflight does not connect to an exchange.",
        "- This preflight does not download market data.",
        "- This preflight does not write a real Freqtrade config.",
        "- This preflight never prints ENV values or secret-shaped values.",
        "",
        "## Blockers",
        "",
        *bullet(report.blockers),
        "",
        "## Failures",
        "",
        *bullet(report.failures),
        "",
    ]
    return "\n".join(lines)


def exit_code_for_status(status: str) -> int:
    if status == "PASS":
        return 0
    if status == "BLOCKED":
        return 2
    return 1
