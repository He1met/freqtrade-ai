import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shlex
import subprocess
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.adapters.okx_demo import credential_preflight as preflight
from app.adapters.okx_demo.secure_http import build_direct_no_redirect_opener


def valid_environment() -> dict[str, str]:
    return {
        preflight.EXECUTION_TARGET_ENV: "OKX_DEMO",
        preflight.ALLOW_REAL_FUNDS_ENV: "false",
        preflight.REST_URL_ENV: preflight.OKX_DEMO_REST_URL,
        "OKX_DEMO_API_KEY": "test-api-key",
        "OKX_DEMO_API_SECRET": "test-api-secret",
        "OKX_DEMO_API_PASSPHRASE": "test-passphrase",
        preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: preflight.account_fingerprint(
            valid_payload()["data"][0]
        ),
    }


def valid_payload() -> dict:
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "uid": "must-not-be-rendered",
                "mainUid": "also-private",
                "acctLv": "2",
                "posMode": "net_mode",
                "perm": "read_only,trade",
            }
        ],
    }


def test_request_is_signed_for_fixed_demo_account_config_contract() -> None:
    request = preflight.build_account_config_request(
        valid_environment(),
        timestamp="2026-07-27T01:02:03.004Z",
    )

    assert request.full_url == "https://openapi.okx.com/api/v5/account/config"
    assert request.method == "GET"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-simulated-trading"] == "1"
    assert headers["ok-access-key"] == "test-api-key"
    assert headers["ok-access-passphrase"] == "test-passphrase"
    assert headers["ok-access-timestamp"] == "2026-07-27T01:02:03.004Z"
    assert headers["ok-access-sign"]


def test_shared_signature_boundary_is_get_only_and_signs_the_exact_query_path() -> None:
    headers = preflight._build_demo_authorization_headers(
        valid_environment(),
        method="GET",
        request_path="/api/v5/account/balance?ccy=USDT",
        body="",
        timestamp="2026-07-27T01:02:03.004Z",
    )

    assert set(headers) == {
        "OK-ACCESS-KEY",
        "OK-ACCESS-SIGN",
        "OK-ACCESS-TIMESTAMP",
        "OK-ACCESS-PASSPHRASE",
    }
    assert headers["OK-ACCESS-KEY"] == "test-api-key"
    assert headers["OK-ACCESS-PASSPHRASE"] == "test-passphrase"
    assert "x-simulated-trading" not in headers


@pytest.mark.parametrize(
    ("method", "request_path", "body"),
    [
        ("POST", "/api/v5/trade/order", ""),
        ("GET", "/api/v5/account/balance", "{}"),
        ("GET", "https://example.invalid/api/v5/account/balance", ""),
        ("GET", "/api/v5/account/balance\nInjected: value", ""),
    ],
)
def test_shared_signature_boundary_rejects_write_or_untrusted_request_contract(
    method: str,
    request_path: str,
    body: str,
) -> None:
    with pytest.raises(preflight.OkxDemoPreflightBlocked):
        preflight._build_demo_authorization_headers(
            valid_environment(),
            method=method,
            request_path=request_path,
            body=body,
        )


def test_direct_opener_ignores_proxy_env_and_never_forwards_auth_on_302(
    monkeypatch,
) -> None:
    observed = {"source": 0, "sink": 0}

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["sink"] += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["source"] += 1
            assert self.headers.get("OK-ACCESS-SIGN") == "test-signature"
            self.send_response(302)
            self.send_header(
                "Location",
                "http://127.0.0.1:{}/sink".format(sink.server_port),
            )
            self.end_headers()

        def log_message(self, *_args):
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        Thread(target=server.serve_forever, daemon=True)
        for server in (source, sink)
    ]
    for thread in threads:
        thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    try:
        request = Request(
            "http://127.0.0.1:{}/start".format(source.server_port),
            headers={"OK-ACCESS-SIGN": "test-signature"},
        )
        with pytest.raises(HTTPError) as redirected:
            build_direct_no_redirect_opener().open(request, timeout=2)
        assert redirected.value.code == 302
        assert observed == {"source": 1, "sink": 0}
    finally:
        source.shutdown()
        sink.shutdown()
        source.server_close()
        sink.server_close()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (preflight.EXECUTION_TARGET_ENV, "OKX_LIVE", "sole OKX_DEMO"),
        (preflight.ALLOW_REAL_FUNDS_ENV, "true", "real-fund"),
        (preflight.REST_URL_ENV, "https://example.invalid", "URL"),
        ("OKX_DEMO_API_PASSPHRASE", "", "bundle is incomplete"),
    ],
)
def test_request_contract_fails_closed(name: str, value: str, message: str) -> None:
    environment = valid_environment()
    environment[name] = value

    with pytest.raises(preflight.OkxDemoPreflightBlocked, match=message):
        preflight.build_account_config_request(environment)


def test_valid_demo_identity_and_minimum_permissions_are_redacted() -> None:
    expected = preflight.account_fingerprint(valid_payload()["data"][0])
    result = preflight.validate_account_config(
        valid_payload(),
        expected_fingerprint=expected,
    )

    assert result["status"] == "READY"
    assert result["execution_target"] == "OKX_DEMO"
    assert result["remote_account_evidence"]["fingerprint_match"] is True
    assert result["remote_account_evidence"]["permissions"] == {
        "read": True,
        "trade": True,
        "withdraw": False,
    }
    assert result["local_target_contract"] == {
        "product_type": "SWAP",
        "margin_mode": "isolated",
        "allow_real_funds": False,
    }
    rendered = json.dumps(result)
    assert "must-not-be-rendered" not in rendered
    assert "mainUid" not in rendered


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(code="1"), "attestation failed"),
        (lambda payload: payload.update(data=[]), "identity is unknown"),
        (lambda payload: payload["data"][0].update(uid=""), "identity is unknown"),
        (
            lambda payload: payload["data"][0].update(perm="read_only"),
            "exactly read_only and trade",
        ),
        (
            lambda payload: payload["data"][0].update(
                perm="read_only,trade,withdraw"
            ),
            "exactly read_only and trade",
        ),
        (
            lambda payload: payload["data"][0].update(posMode="long_short_mode"),
            "net_mode",
        ),
        (
            lambda payload: payload["data"][0].update(acctLv="3"),
            "Futures mode",
        ),
    ],
)
def test_account_attestation_rejects_unknown_or_unsafe_contract(
    mutation,
    message: str,
) -> None:
    payload = valid_payload()
    mutation(payload)
    try:
        expected = preflight.account_fingerprint(payload["data"][0])
    except (IndexError, preflight.OkxDemoPreflightBlocked):
        expected = preflight.account_fingerprint(valid_payload()["data"][0])

    with pytest.raises(preflight.OkxDemoPreflightBlocked, match=message):
        preflight.validate_account_config(
            payload,
            expected_fingerprint=expected,
        )


@pytest.mark.parametrize("expected", ["", "not-a-sha256", "f" * 64])
def test_account_attestation_requires_matching_pinned_fingerprint(expected: str) -> None:
    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="fingerprint",
    ):
        preflight.validate_account_config(
            valid_payload(),
            expected_fingerprint=expected,
        )


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def test_run_preflight_never_returns_raw_account_identifiers() -> None:
    payload = json.dumps(valid_payload()).encode()

    result = preflight.run_preflight(
        valid_environment(),
        opener=lambda request, timeout: FakeResponse(payload),
    )

    assert result["remote_account_evidence"]["fingerprint_match"] is True
    assert "must-not-be-rendered" not in json.dumps(result)


def test_run_preflight_does_not_call_network_without_pinned_fingerprint() -> None:
    environment = valid_environment()
    environment.pop(preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV)

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="bundle is incomplete",
    ):
        preflight.run_preflight(
            environment,
            opener=lambda *_args, **_kwargs: pytest.fail(
                "network must not run without identity pinning"
            ),
        )


def test_account_pin_validates_demo_identity_and_writes_only_in_memory_digest() -> None:
    payload = json.dumps(valid_payload()).encode()
    captured = {}
    environment = valid_environment()
    environment.pop(preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV)

    result = preflight.run_account_pin(
        environment,
        opener=lambda request, timeout: FakeResponse(payload),
        pin_exists=lambda: False,
        pin_writer=lambda fingerprint: captured.update(fingerprint=fingerprint),
    )

    expected = preflight.account_fingerprint(valid_payload()["data"][0])
    assert captured == {"fingerprint": expected}
    assert result == {
        "status": "READY",
        "execution_target": "OKX_DEMO",
        "account_fingerprint_pinned": True,
    }
    rendered = json.dumps(result)
    assert expected not in rendered
    assert "must-not-be-rendered" not in rendered
    assert "also-private" not in rendered


def test_account_pin_refuses_existing_pin_before_network_or_write() -> None:
    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="already exists",
    ):
        preflight.run_account_pin(
            valid_environment(),
            opener=lambda *_args, **_kwargs: pytest.fail(
                "existing pin must prevent the network request"
            ),
            pin_exists=lambda: True,
            pin_writer=lambda _fingerprint: pytest.fail(
                "existing pin must never be overwritten"
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(code="1"),
        lambda payload: payload["data"][0].update(perm="read_only"),
        lambda payload: payload["data"][0].update(acctLv="3"),
        lambda payload: payload["data"][0].update(posMode="long_short_mode"),
    ],
)
def test_account_pin_never_writes_after_remote_validation_failure(mutation) -> None:
    payload = valid_payload()
    mutation(payload)

    with pytest.raises(preflight.OkxDemoPreflightBlocked):
        preflight.run_account_pin(
            valid_environment(),
            opener=lambda request, timeout: FakeResponse(
                json.dumps(payload).encode()
            ),
            pin_exists=lambda: False,
            pin_writer=lambda _fingerprint: pytest.fail(
                "invalid remote account must never be pinned"
            ),
        )


def test_keychain_pin_write_uses_stdin_and_never_argv(monkeypatch) -> None:
    fingerprint = "d" * 64
    observed = []
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        stdout = fingerprint + "\n" if "find-generic-password" in command else ""
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": stdout,
                "stderr": "untrusted-keychain-error",
            },
        )()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    preflight.write_account_fingerprint_pin(fingerprint)

    assert len(observed) == 2
    assert all(fingerprint not in command for command, _kwargs in observed)
    add_command, add_kwargs = observed[0]
    read_command, read_kwargs = observed[1]
    assert "add-generic-password" in add_command
    assert add_command[-1] == "-w"
    assert add_kwargs["input"] == fingerprint + "\n" + fingerprint + "\n"
    assert add_kwargs["capture_output"] is True
    assert "find-generic-password" in read_command
    assert read_kwargs["stdin"] is preflight.subprocess.DEVNULL


@pytest.mark.parametrize(
    ("read_returncode", "read_stdout"),
    [
        (0, ""),
        (0, "e" * 64 + "\n"),
        (1, "d" * 64 + "\n"),
    ],
)
def test_keychain_pin_verification_failure_deletes_new_item_and_blocks(
    monkeypatch,
    read_returncode: int,
    read_stdout: str,
) -> None:
    fingerprint = "d" * 64
    observed = []
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        if "add-generic-password" in command:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "find-generic-password" in command:
            return type(
                "Completed",
                (),
                {
                    "returncode": read_returncode,
                    "stdout": read_stdout,
                    "stderr": "untrusted-read-error",
                },
            )()
        assert "delete-generic-password" in command
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="verification failed",
    ) as blocked:
        preflight.write_account_fingerprint_pin(fingerprint)

    assert len(observed) == 3
    assert "delete-generic-password" in observed[-1][0]
    assert all(fingerprint not in command for command, _kwargs in observed)
    assert fingerprint not in str(blocked.value)


def test_keychain_pin_read_exception_deletes_new_item_and_blocks(monkeypatch) -> None:
    fingerprint = "d" * 64
    observed = []
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        if "add-generic-password" in command:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "find-generic-password" in command:
            raise OSError("untrusted-read-failure")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="verification failed",
    ) as blocked:
        preflight.write_account_fingerprint_pin(fingerprint)

    assert "delete-generic-password" in observed[-1][0]
    assert "untrusted-read-failure" not in str(blocked.value)
    assert all(fingerprint not in command for command, _kwargs in observed)


def test_keychain_pin_add_failure_does_not_delete_possible_existing_item(
    monkeypatch,
) -> None:
    observed = []
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )

    def fake_run(command, **kwargs):
        observed.append(command)
        return type(
            "Completed",
            (),
            {"returncode": 45, "stdout": "", "stderr": "duplicate item"},
        )()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="write failed",
    ):
        preflight.write_account_fingerprint_pin("d" * 64)

    assert len(observed) == 1
    assert "add-generic-password" in observed[0]


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, True), (44, False)],
)
def test_keychain_pin_existence_check_is_explicit_and_redacted(
    monkeypatch,
    returncode: int,
    expected: bool,
) -> None:
    observed = {}
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return type(
            "Completed",
            (),
            {
                "returncode": returncode,
                "stdout": "untrusted-keychain-output",
                "stderr": "untrusted-keychain-error",
            },
        )()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    assert preflight.account_fingerprint_pin_exists() is expected
    assert observed["command"][-1] == preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE
    assert "-w" not in observed["command"]
    assert observed["kwargs"]["stdin"] is preflight.subprocess.DEVNULL


def test_keychain_pin_existence_check_fails_closed_on_ambiguous_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setattr(preflight.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        preflight.pwd,
        "getpwuid",
        lambda uid: type("Account", (), {"pw_name": "local-user"})(),
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "sensitive-error"},
        )(),
    )

    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="Keychain check failed",
    ) as blocked:
        preflight.account_fingerprint_pin_exists()

    assert "sensitive-error" not in str(blocked.value)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS security")
def test_macos_temporary_keychain_account_pin_contract(tmp_path, capsys) -> None:
    security = "/usr/bin/security"
    keychain_path = tmp_path / "okx-pin-contract.keychain-db"
    temporary_unlock = "temporary-test-keychain"
    fingerprint = "d" * 64
    original_default = subprocess.run(
        [security, "default-keychain", "-d", "user"],
        check=True,
        capture_output=True,
        text=True,
    )
    original_search = subprocess.run(
        [security, "list-keychains", "-d", "user"],
        check=True,
        capture_output=True,
        text=True,
    )
    default_paths = shlex.split(original_default.stdout)
    search_paths = shlex.split(original_search.stdout)
    assert default_paths
    try:
        subprocess.run(
            [security, "create-keychain", "-p", temporary_unlock, str(keychain_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [security, "unlock-keychain", "-p", temporary_unlock, str(keychain_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                security,
                "list-keychains",
                "-d",
                "user",
                "-s",
                str(keychain_path),
                *search_paths,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [security, "default-keychain", "-d", "user", "-s", str(keychain_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        preflight.write_account_fingerprint_pin(fingerprint)
        emitted = capsys.readouterr()
        assert fingerprint not in emitted.out
        assert fingerprint not in emitted.err

        stored = subprocess.run(
            [
                security,
                "find-generic-password",
                "-a",
                preflight.pwd.getpwuid(preflight.os.getuid()).pw_name,
                "-s",
                preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
                "-w",
                str(keychain_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert stored.stdout.rstrip("\r\n") == fingerprint
    finally:
        subprocess.run(
            [security, "default-keychain", "-d", "user", "-s", default_paths[0]],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [security, "list-keychains", "-d", "user", "-s", *search_paths],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                security,
                "delete-generic-password",
                "-a",
                preflight.pwd.getpwuid(preflight.os.getuid()).pw_name,
                "-s",
                preflight.OKX_DEMO_ACCOUNT_FINGERPRINT_KEYCHAIN_SERVICE,
                str(keychain_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [security, "delete-keychain", str(keychain_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    restored_default = subprocess.run(
        [security, "default-keychain", "-d", "user"],
        check=True,
        capture_output=True,
        text=True,
    )
    restored_search = subprocess.run(
        [security, "list-keychains", "-d", "user"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert shlex.split(restored_default.stdout) == default_paths
    assert shlex.split(restored_search.stdout) == search_paths
    assert not keychain_path.exists()


def test_transport_and_invalid_json_fail_without_echoing_remote_content() -> None:
    with pytest.raises(
        preflight.OkxDemoPreflightBlocked,
        match="invalid JSON",
    ) as invalid:
        preflight.run_preflight(
            valid_environment(),
            opener=lambda request, timeout: FakeResponse(b"remote-secret-content"),
        )

    assert "remote-secret-content" not in str(invalid.value)
