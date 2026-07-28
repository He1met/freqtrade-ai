from pathlib import Path

from app.adapters.freqtrade.binary import (
    resolve_freqtrade_binary,
    runtime_env_freqtrade_binary,
)


def test_resolves_absolute_env_binary(tmp_path: Path) -> None:
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    resolution = resolve_freqtrade_binary(
        environ={"FREQTRADE_BINARY": str(binary)},
        which=lambda _name: None,
    )

    assert resolution.ready is True
    assert resolution.source == "FREQTRADE_BINARY"
    assert resolution.resolved_path == binary.resolve()


def test_rejects_relative_env_path() -> None:
    resolution = resolve_freqtrade_binary(
        environ={"FREQTRADE_BINARY": "relative/bin/freqtrade"},
        which=lambda _name: None,
    )

    assert resolution.ready is False
    assert resolution.blocked_reason == "FREQTRADE_BINARY must be an absolute executable path"


def test_resolves_path_command_when_env_is_absent(tmp_path: Path) -> None:
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    resolution = resolve_freqtrade_binary(
        environ={},
        which=lambda name: str(binary) if name == "freqtrade" else None,
        runtime_env_path=tmp_path / "missing-runtime.env",
    )

    assert resolution.ready is True
    assert resolution.source == "PATH"
    assert resolution.resolved_path == binary.resolve()


def test_reports_missing_binary_consistently(tmp_path: Path) -> None:
    resolution = resolve_freqtrade_binary(
        environ={},
        which=lambda _name: None,
        runtime_env_path=tmp_path / "missing-runtime.env",
    )

    assert resolution.ready is False
    assert resolution.resolved_path is None
    assert resolution.blocked_reason == "freqtrade binary is not available: freqtrade"


def test_reads_canonical_runtime_env_binary_without_loading_other_values(tmp_path: Path) -> None:
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "DATABASE_URL=postgresql+psycopg://ignored\nFREQTRADE_BINARY={}\n".format(
            binary
        ),
        encoding="utf-8",
    )

    assert runtime_env_freqtrade_binary(runtime_env) == str(binary)


def test_runtime_env_binary_is_used_when_process_env_is_absent(tmp_path: Path) -> None:
    binary = tmp_path / "freqtrade"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "FREQTRADE_BINARY={}\n".format(binary),
        encoding="utf-8",
    )

    resolution = resolve_freqtrade_binary(
        environ={},
        which=lambda _name: None,
        runtime_env_path=runtime_env,
    )

    assert resolution.ready is True
    assert resolution.source == "runtime.env"
    assert resolution.resolved_path == binary.resolve()
