import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "harden_okx_demo_attestation.py"


def load_hardening_module():
    spec = importlib.util.spec_from_file_location(
        "harden_okx_demo_attestation",
        SCRIPT_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("runtime_identity", "admin_identity"),
    [
        (
            (
                "freqtrade_ai",
                "127.0.0.1/32",
                5432,
                "freqtrade",
                100,
                "cluster-a",
            ),
            (
                "freqtrade_ai",
                "local",
                5432,
                "local-admin",
                101,
                "cluster-a",
            ),
        ),
        (
            (
                "freqtrade_ai",
                "::1/128",
                5432,
                "freqtrade",
                100,
                "cluster-a",
            ),
            (
                "freqtrade_ai",
                "local",
                5432,
                "local-admin",
                100,
                "cluster-b",
            ),
        ),
    ],
)
def test_hardening_blocks_database_or_cluster_identity_mismatch(
    monkeypatch,
    runtime_identity,
    admin_identity,
) -> None:
    hardening = load_hardening_module()
    engines = [object(), object()]
    identities = iter((runtime_identity, admin_identity))
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://freqtrade:change_me@localhost:5432/freqtrade_ai",
    )
    monkeypatch.setattr(
        hardening,
        "create_database_engine",
        lambda url: engines.pop(0),
    )
    monkeypatch.setattr(
        hardening,
        "_database_identity",
        lambda engine: next(identities),
    )
    keychain_called = []
    monkeypatch.setattr(
        hardening,
        "_keychain_key",
        lambda: keychain_called.append(True),
    )

    with pytest.raises(
        hardening.HardeningBlocked,
        match="same local database",
    ):
        hardening.main()

    assert keychain_called == []


def test_database_identity_fails_closed_when_system_identifier_is_unavailable() -> None:
    hardening = load_hardening_module()

    class UnavailableEngine:
        def connect(self):
            raise RuntimeError("pg_control_system unavailable")

    with pytest.raises(
        hardening.HardeningBlocked,
        match="cluster identity cannot be proven",
    ):
        hardening._database_identity(UnavailableEngine())


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("local", True),
        ("127.0.0.1/32", True),
        ("::1/128", True),
        ("192.0.2.10/32", False),
        ("database.example", False),
    ],
)
def test_hardening_accepts_only_local_server_addresses(
    address,
    expected,
) -> None:
    hardening = load_hardening_module()
    assert hardening._is_local_server_address(address) is expected
