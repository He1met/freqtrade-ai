from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "canonical_v13_ui_service.py"


def _load_service():
    spec = importlib.util.spec_from_file_location("canonical_v13_ui_service_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class _CanonicalAPI(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _respond(self) -> None:
        body = json.dumps(
            {"status": "BLOCKED", "path": self.path, "method": self.command}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond


class _CanonicalReadinessAPI(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        assert self.path == "/api/canonical-v13/readiness/runtime"
        payload = {
            "status": "BLOCKED",
            "reason_codes": ["TRADING_DISABLED", "ACTIVE_DEPLOYMENT_UNSET"],
            "deployment_id": None,
            "runtime_instance_id": None,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_gateway_serves_spa_and_only_proxies_canonical_api(tmp_path: Path) -> None:
    service = _load_service()
    (tmp_path / "index.html").write_text("<main>canonical-v13</main>", encoding="utf-8")
    with _server(_CanonicalAPI) as api_port:
        handler = service._handler(tmp_path, api_port=api_port)
        with _server(handler) as ui_port:
            with urlopen(f"http://127.0.0.1:{ui_port}/v13/configuration") as response:
                assert response.read() == b"<main>canonical-v13</main>"
                assert response.headers["Content-Security-Policy"]
            request = Request(
                f"http://127.0.0.1:{ui_port}/api/canonical-v13/configurations",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                assert json.loads(response.read()) == {
                    "status": "BLOCKED",
                    "path": "/api/canonical-v13/configurations",
                    "method": "POST",
                }
            try:
                urlopen(f"http://127.0.0.1:{ui_port}/api/v1/strategies")
            except HTTPError as exc:
                assert exc.code == 404
                assert json.loads(exc.read())["error"]["code"] == (
                    "BLOCKED_LEGACY_API_DISABLED"
                )
            else:  # pragma: no cover - fail if a legacy fallback is introduced
                raise AssertionError("legacy API unexpectedly proxied")


def test_launch_agent_contract_contains_no_database_or_secret_material() -> None:
    service = _load_service()
    payload = service._plist_payload(8012)
    serialized = json.dumps(payload, sort_keys=True).lower()
    assert payload["Label"] == "com.he1met.freqtrade-ai.v13-canonical-ui"
    assert payload["ProgramArguments"][-1] == "8012"
    assert "database_url" not in serialized
    assert "password" not in serialized
    assert "keychain" not in serialized
    assert "8000" not in serialized


def test_gateway_preserves_direct_phase9_readiness_contract(tmp_path: Path) -> None:
    service = _load_service()
    (tmp_path / "index.html").write_text("<main>canonical-v13</main>", encoding="utf-8")
    with _server(_CanonicalReadinessAPI) as api_port:
        handler = service._handler(tmp_path, api_port=api_port)
        with _server(handler) as ui_port:
            with urlopen(
                f"http://127.0.0.1:{ui_port}"
                "/api/canonical-v13/readiness/runtime"
            ) as response:
                payload = json.loads(response.read())

    assert payload == {
        "status": "BLOCKED",
        "reason_codes": ["TRADING_DISABLED", "ACTIVE_DEPLOYMENT_UNSET"],
        "deployment_id": None,
        "runtime_instance_id": None,
    }
    serialized = json.dumps(payload, sort_keys=True).lower()
    assert "password" not in serialized
    assert "database_url" not in serialized
    assert "credential" not in serialized
