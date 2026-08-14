#!/usr/bin/env python3
"""Serve the canonical UI and proxy only its canonical loopback API."""

from __future__ import annotations

import argparse
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import time
from typing import Sequence
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = REPO_ROOT / "frontend" / "dist"
BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SCRIPT_PATH = Path(__file__).resolve()
LABEL = "com.he1met.freqtrade-ai.v13-canonical-ui"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "FreqtradeAiV13"
STDOUT_LOG = LOG_DIR / "canonical-ui.log"
STDERR_LOG = LOG_DIR / "canonical-ui-error.log"
DEFAULT_UI_PORT = 8012
CANONICAL_API_HOST = "127.0.0.1"
CANONICAL_API_PORT = 8011
CANONICAL_API_PREFIX = "/api/canonical-v13"
MAX_COMMAND_BYTES = 1_048_576


class CanonicalUIBlocked(RuntimeError):
    """Fail-closed deployment or request boundary."""


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def _require_release_checkout() -> None:
    if ".codex/worktrees" in str(REPO_ROOT):
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED")
    status = _run(["git", "status", "--porcelain"])
    head = _run(["git", "rev-parse", "HEAD"])
    main = _run(["git", "rev-parse", "origin/main"])
    if (
        status.returncode != 0
        or status.stdout.strip()
        or head.returncode != 0
        or main.returncode != 0
        or head.stdout.strip() != main.stdout.strip()
    ):
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_RELEASE_CHECKOUT_REQUIRED")
    if not BACKEND_PYTHON.is_file():
        raise CanonicalUIBlocked("BLOCKED_BACKEND_VIRTUALENV_MISSING")
    if not (DIST_ROOT / "index.html").is_file():
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_UI_BUILD_MISSING")


def _port_available(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as candidate:
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _launchctl_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _plist_payload(port: int) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(BACKEND_PYTHON),
            str(SCRIPT_PATH),
            "serve",
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        },
    }


def _handler(
    dist_root: Path,
    *,
    api_host: str = CANONICAL_API_HOST,
    api_port: int = CANONICAL_API_PORT,
) -> type[BaseHTTPRequestHandler]:
    root = dist_root.resolve()

    class CanonicalUIHandler(BaseHTTPRequestHandler):
        server_version = "CanonicalV13UI/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._headers(status, "application/json", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _proxy(self) -> None:
            parsed = urlsplit(self.path)
            if not (
                parsed.path == CANONICAL_API_PREFIX
                or parsed.path.startswith(CANONICAL_API_PREFIX + "/")
            ):
                self._json(
                    404,
                    {
                        "status": "BLOCKED",
                        "error": {
                            "code": "BLOCKED_LEGACY_API_DISABLED",
                            "detail": "canonical UI gateway exposes no legacy API",
                        },
                    },
                )
                return
            length_text = self.headers.get("Content-Length", "0")
            try:
                length = int(length_text)
            except ValueError:
                self._json(
                    400,
                    {
                        "status": "BLOCKED",
                        "error": {"code": "BLOCKED_INVALID_CONTENT_LENGTH"},
                    },
                )
                return
            if length < 0 or length > MAX_COMMAND_BYTES:
                self._json(
                    413,
                    {
                        "status": "BLOCKED",
                        "error": {"code": "BLOCKED_COMMAND_TOO_LARGE"},
                    },
                )
                return
            body = self.rfile.read(length) if length else None
            target = parsed.path + (("?" + parsed.query) if parsed.query else "")
            headers = {"Accept": "application/json"}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type
            connection = HTTPConnection(api_host, api_port, timeout=5)
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                response_body = response.read(MAX_COMMAND_BYTES + 1)
                if len(response_body) > MAX_COMMAND_BYTES:
                    raise CanonicalUIBlocked("BLOCKED_UPSTREAM_RESPONSE_TOO_LARGE")
                self._headers(
                    response.status,
                    response.getheader("Content-Type", "application/json"),
                    len(response_body),
                )
                if self.command != "HEAD":
                    self.wfile.write(response_body)
            except (OSError, CanonicalUIBlocked):
                self._json(
                    502,
                    {
                        "status": "BLOCKED",
                        "error": {
                            "code": "BLOCKED_CANONICAL_API_UNAVAILABLE",
                            "detail": "canonical API upstream is unavailable",
                        },
                    },
                )
            finally:
                connection.close()

        def _static(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/gateway-healthz":
                self._json(
                    200,
                    {
                        "status": "HEALTHY",
                        "service": "canonical-v13-ui",
                        "api_prefix": CANONICAL_API_PREFIX,
                        "legacy_fallback": "DISABLED",
                        "trading_capability": "TRADING_DISABLED",
                    },
                )
                return
            decoded = unquote(parsed.path)
            relative = decoded.lstrip("/")
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self._json(404, {"status": "BLOCKED", "error": {"code": "BLOCKED_UI_PATH"}})
                return
            if not candidate.is_file():
                candidate = root / "index.html"
            if not candidate.is_file():
                self._json(
                    503,
                    {
                        "status": "BLOCKED",
                        "error": {"code": "BLOCKED_CANONICAL_UI_BUILD_MISSING"},
                    },
                )
                return
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "application/json",
            }:
                content_type += "; charset=utf-8"
            self._headers(200, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path.startswith("/api"):
                self._proxy()
            else:
                self._static()

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy()

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy()

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy()

    return CanonicalUIHandler


def serve(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise CanonicalUIBlocked("BLOCKED_INVALID_LOOPBACK_PORT")
    if not (DIST_ROOT / "index.html").is_file():
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_UI_BUILD_MISSING")
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(DIST_ROOT))
    server.serve_forever()


def install(port: int) -> dict[str, object]:
    _require_release_checkout()
    if shutil.which("launchctl") is None:
        raise CanonicalUIBlocked("BLOCKED_LAUNCHCTL_REQUIRED")
    if not _port_available(port):
        raise CanonicalUIBlocked("BLOCKED_LOOPBACK_PORT_IN_USE")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PLIST_PATH.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(_plist_payload(port), handle, sort_keys=True)
    temporary.replace(PLIST_PATH)
    _run(["launchctl", "bootout", _launchctl_target()])
    completed = _run(
        ["launchctl", "bootstrap", _launchctl_domain(), str(PLIST_PATH)]
    )
    if completed.returncode != 0:
        raise CanonicalUIBlocked("BLOCKED_LAUNCHCTL_BOOTSTRAP_FAILED")
    return {"status": "INSTALLED", "label": LABEL, "port": port}


def status(port: int) -> dict[str, object]:
    launch = _run(["launchctl", "print", _launchctl_target()])
    loaded = launch.returncode == 0
    health = "UNAVAILABLE"
    if loaded:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/gateway-healthz", timeout=2
            ) as response:
                health = str(json.loads(response.read()).get("status", "UNKNOWN"))
        except (OSError, ValueError, json.JSONDecodeError):
            health = "UNAVAILABLE"
    return {
        "status": "READY" if loaded and health == "HEALTHY" else "BLOCKED",
        "label": LABEL,
        "loaded": loaded,
        "health": health,
        "port": port,
    }


def restart(port: int) -> dict[str, object]:
    _require_release_checkout()
    if not PLIST_PATH.is_file():
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_UI_LAUNCH_AGENT_MISSING")
    completed = _run(["launchctl", "kickstart", "-k", _launchctl_target()])
    if completed.returncode != 0:
        raise CanonicalUIBlocked("BLOCKED_CANONICAL_UI_RESTART_FAILED")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = status(port)
        if observed["status"] == "READY":
            return {**observed, "status": "RESTARTED"}
        time.sleep(0.25)
    raise CanonicalUIBlocked("BLOCKED_CANONICAL_UI_RESTART_TIMEOUT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "install", "status", "restart"))
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.port)
            return 0
        if args.command == "install":
            payload = install(args.port)
        elif args.command == "restart":
            payload = restart(args.port)
        else:
            payload = status(args.port)
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["status"] in {"INSTALLED", "READY", "RESTARTED"} else 1
    except CanonicalUIBlocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
