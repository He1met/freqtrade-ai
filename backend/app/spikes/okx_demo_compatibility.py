from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OKX_REST_URL = "https://openapi.okx.com"
OKX_DEMO_PUBLIC_WS_URL = "wss://wspap.okx.com:8443/ws/v5/public"
OKX_DEMO_PRIVATE_WS_URL = "wss://wspap.okx.com:8443/ws/v5/private"
OKX_DEMO_BUSINESS_WS_URL = "wss://wspap.okx.com:8443/ws/v5/business"
OKX_DEMO_HEADER = ("x-simulated-trading", "1")
OKX_DEMO_CREDENTIAL_ENV_NAMES = (
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
)

EXIT_PASS = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
FROZEN_VERSIONS = {
    "freqtrade": "2026.5",
    "ccxt": "4.5.56",
    "okx_agent_trade_kit": "1.2.8",
}
OKX_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DiagnosticReport:
    status: str
    checks: tuple[Check, ...]
    versions: Mapping[str, str]
    credentials: Mapping[str, str]
    decision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [asdict(check) for check in self.checks],
            "versions": dict(self.versions),
            "credentials": dict(self.credentials),
            "decision": self.decision,
        }


class OkxBusinessError(ValueError):
    """An HTTP-success response that failed OKX business-level validation."""


def credential_presence(environ: Mapping[str, str]) -> dict[str, str]:
    """Report only presence; never copy a credential value."""
    return {
        name: "PRESENT" if bool(environ.get(name, "").strip()) else "ABSENT"
        for name in OKX_DEMO_CREDENTIAL_ENV_NAMES
    }


def validate_okx_client_order_id(client_order_id: object) -> str:
    """Require an OKX-legal client order ID chosen before the write request."""
    if not isinstance(client_order_id, str) or not OKX_CLIENT_ORDER_ID_PATTERN.fullmatch(
        client_order_id
    ):
        raise OkxBusinessError(
            "deterministic clOrdId must be 1-32 case-sensitive alphanumeric characters"
        )
    return client_order_id


def parse_okx_write_response(
    payload: object,
    *,
    expected_cl_ord_id: str,
) -> list[dict[str, Any]]:
    """Require business success and reconcile exchange IDs to the request."""
    expected_cl_ord_id = validate_okx_client_order_id(expected_cl_ord_id)
    if not isinstance(payload, dict):
        raise OkxBusinessError("response must be a JSON object")
    code = payload.get("code")
    if str(code) != "0":
        raise OkxBusinessError(f"OKX top-level code={code!r}")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise OkxBusinessError("OKX response data must be a non-empty array")
    parsed: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise OkxBusinessError(f"OKX response data[{index}] must be an object")
        s_code = item.get("sCode")
        if str(s_code) != "0":
            raise OkxBusinessError(f"OKX data[{index}] sCode={s_code!r}")
        ord_id = item.get("ordId")
        if not isinstance(ord_id, str) or not ord_id.strip():
            raise OkxBusinessError(f"OKX data[{index}] must contain a non-empty ordId")
        response_cl_ord_id = item.get("clOrdId")
        if response_cl_ord_id != expected_cl_ord_id:
            raise OkxBusinessError(
                f"OKX data[{index}] clOrdId does not match the predetermined request ID"
            )
        parsed.append(item)
    return parsed


def retry_decision(
    *,
    operation: str,
    http_status: Optional[int] = None,
    okx_code: Optional[str] = None,
    timed_out: bool = False,
    network_error: bool = False,
    deterministic_cl_ord_id: Optional[str] = None,
) -> str:
    """
    Classify retries conservatively.

    A timed-out write has an unknown outcome and must be reconciled by clOrdId;
    blindly resubmitting it could create a duplicate order.
    """
    if operation not in {"read", "write"}:
        raise ValueError("operation must be read or write")
    write_outcome_unknown = operation == "write" and (
        timed_out
        or network_error
        or http_status in {429, 500, 502, 503, 504}
        or okx_code in {"50001", "50004", "50011", "50013", "50026", "50061"}
    )
    if write_outcome_unknown:
        try:
            validate_okx_client_order_id(deterministic_cl_ord_id)
        except OkxBusinessError:
            return "BLOCKED_MISSING_CLORDID"
        return "RECONCILE_BY_CLORDID"
    if timed_out or network_error:
        return "RETRY_WITH_BACKOFF"
    if http_status == 429 or (http_status is not None and 500 <= http_status <= 599):
        return "RETRY_WITH_BACKOFF"
    if okx_code in {"50001", "50004", "50011", "50013", "50026", "50061"}:
        return "RETRY_WITH_BACKOFF"
    return "DO_NOT_RETRY"


def validate_target_contract(target: Mapping[str, object]) -> list[str]:
    expected = {
        "execution_target": "OKX_DEMO",
        "exchange": "okx",
        "instrument_type": "SWAP",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "position_mode": "net_mode",
        "simulated_trading": True,
        "allow_real_funds": False,
        "rest_url": OKX_REST_URL,
        "public_ws_url": OKX_DEMO_PUBLIC_WS_URL,
        "private_ws_url": OKX_DEMO_PRIVATE_WS_URL,
        "business_ws_url": OKX_DEMO_BUSINESS_WS_URL,
    }
    failures = [
        f"{key}: expected {expected_value!r}, got {target.get(key)!r}"
        for key, expected_value in expected.items()
        if target.get(key) != expected_value
    ]
    headers = target.get("private_rest_headers")
    if (
        not isinstance(headers, dict)
        or headers.get(OKX_DEMO_HEADER[0]) != OKX_DEMO_HEADER[1]
    ):
        failures.append("private_rest_headers must force x-simulated-trading='1'")
    return failures


def _command_version(command: Sequence[str], pattern: str) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "NOT_INSTALLED"
    if completed.returncode != 0:
        return "NOT_INSTALLED"
    text = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(pattern, text)
    return match.group(1) if match else "UNKNOWN"


def detect_versions(
    *,
    freqtrade_binary: str = "freqtrade",
    python_binary: str = "python3",
    agent_binary: str = "okx",
) -> dict[str, str]:
    return {
        "freqtrade": _command_version(
            (freqtrade_binary, "--version"), r"Freqtrade Version:\s*freqtrade\s+([^\s]+)"
        ),
        "ccxt": _command_version(
            (python_binary, "-c", "import ccxt; print(ccxt.__version__)"),
            r"([0-9]+\.[0-9]+\.[0-9]+)",
        ),
        "okx_agent_trade_kit": _command_version(
            (agent_binary, "--version"), r"([0-9]+\.[0-9]+\.[0-9]+)"
        ),
    }


def probe_public_rest(timeout_seconds: float = 10.0) -> Check:
    """Safe, unauthenticated probe. It never sends credentials or Demo headers."""
    url = f"{OKX_REST_URL}/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP"
    try:
        request = Request(url, headers={"User-Agent": "freqtrade-ai-okx-compat/1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return Check("public_rest", "FAILED", f"{type(exc).__name__}; no credentials sent")
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return Check("public_rest", "FAILED", "HTTP response did not contain OKX code=0")
    data = payload.get("data")
    if not isinstance(data, list) or not data or data[0].get("instType") != "SWAP":
        return Check("public_rest", "FAILED", "BTC-USDT-SWAP instrument was not returned")
    return Check("public_rest", "PASS", "public SWAP instrument query returned code=0")


def run_diagnostics(
    *,
    environ: Mapping[str, str],
    target: Mapping[str, object],
    versions: Mapping[str, str],
    probe_public: bool = False,
) -> DiagnosticReport:
    checks: list[Check] = []
    failures = validate_target_contract(target)
    checks.append(
        Check(
            "target_contract",
            "FAILED" if failures else "PASS",
            "; ".join(failures) if failures else "OKX_DEMO/SWAP/isolated/net_mode contract is exact",
        )
    )
    version_details = {
        "freqtrade": (
            "Freqtrade 2026.5 OKX adapter supports futures+isolated+net mode "
            "but rejects demo_trading"
        ),
        "ccxt": (
            "CCXT 4.5.56 okx.set_sandbox_mode(True) injects "
            "x-simulated-trading=1"
        ),
        "okx_agent_trade_kit": (
            "Agent Trade Kit npm stable 1.2.8 supports --demo/--json/read-only, "
            "SWAP, and sCode exit failures"
        ),
    }
    for name, expected in FROZEN_VERSIONS.items():
        actual = versions.get(name, "NOT_INSTALLED")
        check_name = {
            "freqtrade": "freqtrade_boundary",
            "ccxt": "ccxt_boundary",
            "okx_agent_trade_kit": "agent_kit_boundary",
        }[name]
        if actual == expected:
            version_status = "PASS"
            detail = version_details[name]
        elif actual == "NOT_INSTALLED":
            version_status = "NOT_INSTALLED"
            detail = f"expected {expected}; behavior is documented but not locally executable"
        else:
            version_status = "FAILED"
            detail = f"expected frozen version {expected}, detected {actual}"
        checks.append(Check(check_name, version_status, detail))
    checks.append(
        Check(
            "websocket_contract",
            "PASS",
            "Demo WS endpoints are isolated at wspap.okx.com; do not reuse production WS",
        )
    )
    if probe_public:
        checks.append(probe_public_rest())
    else:
        checks.append(
            Check(
                "public_rest",
                "NOT_RUN",
                "use --probe-public-rest for a credential-free probe",
            )
        )

    credentials = credential_presence(environ)
    credentials_ready = all(state == "PRESENT" for state in credentials.values())
    checks.append(
        Check(
            "demo_credentials",
            "PRESENT" if credentials_ready else "ABSENT",
            "values were neither read into the report nor persisted",
        )
    )
    checks.append(
        Check(
            "demo_canary",
            "BLOCKED",
            "Issue #443 is open; no authenticated request or order was attempted",
        )
    )

    if any(check.status == "FAILED" for check in checks):
        status = "FAILED"
    else:
        status = "BLOCKED"
    return DiagnosticReport(
        status=status,
        checks=tuple(checks),
        versions=dict(versions),
        credentials=credentials,
        decision=(
            "GO for a project-owned OKX REST/WS adapter as the only order writer; "
            "NO-GO for Demo canary until #443; NO-GO for Freqtrade demo_trading on OKX; "
            "NO-GO for Agent Kit and Freqtrade/project adapter writing concurrently"
        ),
    )


def exit_code_for_status(status: str) -> int:
    return {"PASS": EXIT_PASS, "FAILED": EXIT_FAILED, "BLOCKED": EXIT_BLOCKED}.get(
        status, EXIT_FAILED
    )


def load_target(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("target file must contain one JSON object")
    return payload
