import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.spikes.okx_demo_compatibility import (
    EXIT_BLOCKED,
    OkxBusinessError,
    credential_presence,
    exit_code_for_status,
    parse_okx_write_response,
    retry_decision,
    run_diagnostics,
    validate_target_contract,
)


@pytest.fixture
def target() -> dict[str, object]:
    return {
        "execution_target": "OKX_DEMO",
        "exchange": "okx",
        "instrument_type": "SWAP",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "position_mode": "net_mode",
        "simulated_trading": True,
        "allow_real_funds": False,
        "rest_url": "https://openapi.okx.com",
        "public_ws_url": "wss://wspap.okx.com:8443/ws/v5/public",
        "private_ws_url": "wss://wspap.okx.com:8443/ws/v5/private",
        "business_ws_url": "wss://wspap.okx.com:8443/ws/v5/business",
        "private_rest_headers": {"x-simulated-trading": "1"},
    }


def test_target_contract_accepts_only_demo_swap_isolated_net_mode(target) -> None:
    assert validate_target_contract(target) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_target", "OKX_LIVE"),
        ("instrument_type", "SPOT"),
        ("margin_mode", "cross"),
        ("position_mode", "long_short_mode"),
        ("simulated_trading", False),
        ("allow_real_funds", True),
        ("private_ws_url", "wss://ws.okx.com:8443/ws/v5/private"),
    ],
)
def test_target_contract_rejects_unsafe_or_unsupported_values(target, field, value) -> None:
    target[field] = value
    assert validate_target_contract(target)


def test_target_contract_requires_demo_header(target) -> None:
    target["private_rest_headers"] = {}
    assert validate_target_contract(target) == [
        "private_rest_headers must force x-simulated-trading='1'"
    ]


def test_write_response_requires_top_level_and_item_success() -> None:
    assert parse_okx_write_response(
        {"code": "0", "data": [{"ordId": "123", "clOrdId": "compat1", "sCode": "0"}]},
        expected_cl_ord_id="compat1",
    )[0]["ordId"] == "123"
    with pytest.raises(OkxBusinessError, match="top-level"):
        parse_okx_write_response(
            {"code": "50011", "data": []},
            expected_cl_ord_id="compat1",
        )
    with pytest.raises(OkxBusinessError, match="sCode"):
        parse_okx_write_response(
            {
                "code": "0",
                "data": [{"ordId": "", "clOrdId": "compat1", "sCode": "51008"}],
            },
            expected_cl_ord_id="compat1",
        )
    with pytest.raises(OkxBusinessError, match="non-empty"):
        parse_okx_write_response(
            {"code": "0", "data": []},
            expected_cl_ord_id="compat1",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "0", "data": [{"ordId": "", "clOrdId": "compat1", "sCode": "0"}]},
        {"code": "0", "data": [{"ordId": "123", "clOrdId": "", "sCode": "0"}]},
        {"code": "0", "data": [{"ordId": "123", "clOrdId": "other1", "sCode": "0"}]},
    ],
)
def test_write_response_requires_matching_order_identifiers(payload) -> None:
    with pytest.raises(OkxBusinessError):
        parse_okx_write_response(payload, expected_cl_ord_id="compat1")


@pytest.mark.parametrize("client_order_id", ["", "has-hyphen", "x" * 33, "空"])
def test_write_response_rejects_illegal_predetermined_client_order_id(
    client_order_id: str,
) -> None:
    with pytest.raises(OkxBusinessError, match="deterministic clOrdId"):
        parse_okx_write_response(
            {
                "code": "0",
                "data": [{"ordId": "123", "clOrdId": client_order_id, "sCode": "0"}],
            },
            expected_cl_ord_id=client_order_id,
        )


def test_timeout_retry_is_safe_for_reads_and_reconciles_writes() -> None:
    assert retry_decision(operation="read", timed_out=True) == "RETRY_WITH_BACKOFF"
    assert (
        retry_decision(
            operation="write",
            timed_out=True,
            deterministic_cl_ord_id="compat1",
        )
        == "RECONCILE_BY_CLORDID"
    )
    assert (
        retry_decision(
            operation="write",
            http_status=503,
            deterministic_cl_ord_id="compat1",
        )
        == "RECONCILE_BY_CLORDID"
    )
    assert retry_decision(operation="read", okx_code="50011") == "RETRY_WITH_BACKOFF"
    assert retry_decision(operation="write", okx_code="51008") == "DO_NOT_RETRY"


@pytest.mark.parametrize("client_order_id", [None, "", "has-hyphen", "x" * 33])
def test_write_timeout_without_valid_client_order_id_is_blocked(
    client_order_id,
) -> None:
    assert (
        retry_decision(
            operation="write",
            timed_out=True,
            deterministic_cl_ord_id=client_order_id,
        )
        == "BLOCKED_MISSING_CLORDID"
    )


def test_credential_report_never_contains_values() -> None:
    secret_values = {
        "OKX_DEMO_API_KEY": "key-value",
        "OKX_DEMO_API_SECRET": "secret-value",
        "OKX_DEMO_API_PASSPHRASE": "passphrase-value",
    }
    report = credential_presence(secret_values)
    serialized = json.dumps(report)
    assert set(report.values()) == {"PRESENT"}
    assert all(secret not in serialized for secret in secret_values.values())


def test_diagnostics_fail_closed_without_issue_443(target) -> None:
    report = run_diagnostics(
        environ={},
        target=target,
        versions={
            "freqtrade": "2026.5",
            "ccxt": "4.5.56",
            "okx_agent_trade_kit": "1.2.8",
        },
    )
    assert report.status == "BLOCKED"
    assert exit_code_for_status(report.status) == EXIT_BLOCKED
    assert (
        next(check for check in report.checks if check.name == "demo_canary").status
        == "BLOCKED"
    )
    assert "only order writer" in report.decision


def test_diagnostics_does_not_claim_missing_tool_version_passed(target) -> None:
    report = run_diagnostics(
        environ={},
        target=target,
        versions={
            "freqtrade": "NOT_INSTALLED",
            "ccxt": "NOT_INSTALLED",
            "okx_agent_trade_kit": "NOT_INSTALLED",
        },
    )
    boundary_checks = [
        check for check in report.checks if check.name.endswith("_boundary")
    ]
    assert {check.status for check in boundary_checks} == {"NOT_INSTALLED"}
    assert report.status == "BLOCKED"


def test_real_cli_defaults_to_blocked_when_tools_and_ccxt_are_not_installed(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    environment = {
        "PATH": str(tmp_path),
        "PYTHONPATH": "",
    }

    completed = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "okx_demo_compatibility.py")],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == EXIT_BLOCKED
    payload = json.loads(completed.stdout)
    assert payload["status"] == "BLOCKED"
    boundary_checks = {
        check["name"]: check["status"]
        for check in payload["checks"]
        if check["name"].endswith("_boundary")
    }
    assert boundary_checks == {
        "freqtrade_boundary": "NOT_INSTALLED",
        "ccxt_boundary": "NOT_INSTALLED",
        "agent_kit_boundary": "NOT_INSTALLED",
    }
