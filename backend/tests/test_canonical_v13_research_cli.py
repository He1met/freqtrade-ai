from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

from scripts import canonical_v13_research as cli


def test_control_cli_uses_loopback_api_and_exact_command_file(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    command = tmp_path / "plan.json"
    command.write_text(json.dumps({"exact": "payload"}))
    observed = {}

    def request(environment, *, method, path, payload):
        observed.update(method=method, path=path, payload=payload)
        return {"status": "READY", "validation_plan_id": str(uuid4())}

    monkeypatch.setenv(cli.API_BASE_ENV, "http://127.0.0.1:8011")
    monkeypatch.setattr(cli, "_request", request)
    assert cli.main(["plan", "--command-file", str(command)]) == 0
    assert observed == {
        "method": "POST",
        "path": "/research/validation-plans",
        "payload": {"exact": "payload"},
    }
    assert json.loads(capsys.readouterr().out)["status"] == "READY"


def test_cli_rejects_non_loopback_control_and_unactivated_worker(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    plan_id = uuid4()
    monkeypatch.setenv(cli.API_BASE_ENV, "https://example.com")
    assert cli.main(["status", "--id", str(plan_id)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"

    worker = tmp_path / "worker.json"
    worker.write_text(
        json.dumps(
            {
                "validation_attempt_id": str(uuid4()),
                "expected_plan_digest": "a" * 64,
                "authorization_consumption": {},
                "scorer_identity": "scorer",
                "qualifier_identity": "qualifier",
            }
        )
    )
    monkeypatch.delenv(cli.READER_DATABASE_URL_ENV, raising=False)
    assert cli.main(["worker-execute", "--command-file", str(worker)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert "DATABASE_URL" in result["reason_code"]


def test_control_cli_returns_blocked_exit_for_canonical_api_error(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    command = tmp_path / "authorize.json"
    command.write_text(json.dumps({"exact": "payload"}))

    def blocked_request(*_args, **_kwargs):
        payload = json.dumps(
            {"error": {"code": "BLOCKED_AUTHORIZATION_PLAN_NOT_READY"}}
        ).encode()
        raise HTTPError(
            "http://127.0.0.1:8011/api/canonical-v13/research/authorizations",
            409,
            "Conflict",
            hdrs=None,
            fp=BytesIO(payload),
        )

    monkeypatch.setenv(cli.API_BASE_ENV, "http://127.0.0.1:8011")
    monkeypatch.setattr(cli, "urlopen", blocked_request)
    assert cli.main(["authorize", "--command-file", str(command)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "error": {"code": "BLOCKED_AUTHORIZATION_PLAN_NOT_READY"},
        "status": "BLOCKED",
    }
