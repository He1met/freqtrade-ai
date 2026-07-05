from pathlib import Path

from app.services.secret_scanning import (
    format_secret_scan_report,
    scan_repo_for_secrets,
)


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_secret_scan_blocks_secret_shaped_values_without_rendering_values(tmp_path) -> None:
    write_file(
        tmp_path / "config" / "local.yaml",
        """
exchange:
  api_secret: local-credential-value
  api_key: local-key-value
""".strip(),
    )

    report = scan_repo_for_secrets(tmp_path, scan_paths=["config"], tracked_only=False)
    rendered = format_secret_scan_report(report)
    rendered_json = report.to_json()

    assert report.status == "BLOCKED"
    assert {finding.key for finding in report.findings} == {"api_secret", "api_key"}
    assert "local-credential-value" not in rendered
    assert "local-key-value" not in rendered
    assert "local-credential-value" not in rendered_json
    assert "local-key-value" not in rendered_json
    assert "config/local.yaml:2: key=api_secret" in rendered


def test_secret_scan_allows_env_references_and_placeholders(tmp_path) -> None:
    write_file(
        tmp_path / ".env.example",
        """
OKX_DEMO_API_KEY=change_me
OKX_DEMO_API_SECRET=${OKX_DEMO_API_SECRET}
OKX_DEMO_API_PASSPHRASE=<OKX_DEMO_API_PASSPHRASE>
""".strip(),
    )
    write_file(
        tmp_path / "config" / "exchange.yaml",
        """
exchange:
  api_key_env: OKX_DEMO_API_KEY
  api_secret_env: OKX_DEMO_API_SECRET
  api_passphrase_env: OKX_DEMO_API_PASSPHRASE
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=[".env.example", "config"],
        tracked_only=False,
    )

    assert report.status == "PASS"
    assert report.findings == ()


def test_secret_scan_allows_documented_safe_examples(tmp_path) -> None:
    write_file(
        tmp_path / "docs" / "security.md",
        """
# Security examples

Use `api_key_env: OPENAI_API_KEY` in YAML and `OPENAI_API_KEY=${OPENAI_API_KEY}`
in local examples. Do not paste values into docs.
""".strip(),
    )

    report = scan_repo_for_secrets(tmp_path, scan_paths=["docs"], tracked_only=False)

    assert report.status == "PASS"


def test_secret_scan_covers_fixture_and_report_paths(tmp_path) -> None:
    write_file(
        tmp_path / "backend" / "tests" / "fixtures" / "unsafe.json",
        '{"api_token": "local-credential-value"}',
    )
    write_file(
        tmp_path / "reports" / "security" / "unsafe.json",
        '{"private_key": "local-report-credential"}',
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["backend/tests/fixtures", "reports"],
        tracked_only=False,
    )

    assert report.status == "BLOCKED"
    assert {finding.path for finding in report.findings} == {
        "backend/tests/fixtures/unsafe.json",
        "reports/security/unsafe.json",
    }
    assert {finding.key for finding in report.findings} == {"api_token", "private_key"}
