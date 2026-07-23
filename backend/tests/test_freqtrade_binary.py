from pathlib import Path

from app.adapters.freqtrade.binary import resolve_freqtrade_binary


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
    )

    assert resolution.ready is True
    assert resolution.source == "PATH"
    assert resolution.resolved_path == binary.resolve()


def test_reports_missing_binary_consistently() -> None:
    resolution = resolve_freqtrade_binary(environ={}, which=lambda _name: None)

    assert resolution.ready is False
    assert resolution.resolved_path is None
    assert resolution.blocked_reason == "freqtrade binary is not available: freqtrade"
