from pathlib import Path

import pytest

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


def test_secret_scan_allows_boolean_absence_flags_without_allowing_values(
    tmp_path,
) -> None:
    write_file(
        tmp_path / "config" / "evidence.py",
        """
contains_secret_material: False
secret_material_present = False
api_secret: local-credential-value
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )

    assert report.status == "BLOCKED"
    assert [finding.key for finding in report.findings] == ["api_secret"]


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


def test_secret_scan_allows_only_safe_authorization_metadata_values(
    tmp_path,
) -> None:
    write_file(
        tmp_path / "config" / "authorization.yaml",
        """
authorization_schema_version: RISK_V1
authorization_status: BLOCKED
authorization_contract: OKX_DEMO_RISK_V1
authorization_schema_version: LEGACY
authorization_status: ACTIVE
authorization_status: APPROVED
authorization_status: CONSUMED
authorization_status: EXPIRED
authorization_status: PENDING_RISK
authorization_status: REVOKED
authorization_status: UNKNOWN_LEGACY
authorization_contract: RISK_V1
authorization_contract: LEGACY
secret_id: ACTIVE
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )

    assert report.status == "PASS"


def test_secret_scan_allows_non_secret_authorization_references_but_not_headers(
    tmp_path,
) -> None:
    write_file(
        tmp_path / "config" / "authorization.py",
        """
authorization_id = uuid4()
authorization_receipt_digest = receipt.digest
authorization_consumption: ResearchAuthorizationConsumption | None
_SECRET_PATTERNS: Final[tuple[object, ...]] = ()
authorization = sk-live-header-secret
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )

    assert report.status == "BLOCKED"
    assert [finding.key for finding in report.findings] == ["authorization"]


def test_secret_scan_blocks_secret_shaped_authorization_metadata_values(
    tmp_path,
) -> None:
    write_file(
        tmp_path / "config" / "authorization.yaml",
        """
authorization_schema_version: sk-live-schema-secret
authorization_status: sk-live-status-secret
authorization_contract: sk-live-contract-secret
secret_id: sk-live-secret-record
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )

    assert report.status == "BLOCKED"
    assert [finding.key for finding in report.findings] == [
        "authorization_schema_version",
        "authorization_status",
        "authorization_contract",
        "secret_id",
    ]


@pytest.mark.parametrize(
    ("line", "expected_keys"),
    [
        (
            "secret_id = 'ACTIVE' OR hmac_key=sk-live-hidden",
            ["secret_id", "hmac_key"],
        ),
        (
            "secret_id = 'ACTIVE' AND api_secret=sk-live-hidden",
            ["secret_id", "api_secret"],
        ),
        (
            "secret_id: ACTIVE # api_secret: sk-live-hidden",
            ["secret_id", "api_secret"],
        ),
        (
            "api_secret: Mapped[str] = "
            "mapped_column(default='sk-live-hidden')",
            ["api_secret"],
        ),
        (
            "secret_id: Mapped[str] = "
            "mapped_column(default='sk-live-hidden')",
            ["secret_id"],
        ),
        (
            "api_secret: Mapped[str] = mapped_column('sk-live-hidden')",
            ["api_secret"],
        ),
    ],
)
def test_secret_scan_does_not_hide_chained_or_annotated_secrets(
    tmp_path,
    line,
    expected_keys,
) -> None:
    write_file(tmp_path / "config" / "unsafe.py", line)
    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )
    assert report.status == "BLOCKED"
    assert [finding.key for finding in report.findings] == expected_keys


def test_secret_scan_does_not_apply_generic_test_exemptions_to_authorization_metadata(
    tmp_path,
) -> None:
    write_file(
        tmp_path / "config" / "authorization.yaml",
        """
authorization_schema_version: TEST_FAKE_SCHEMA
authorization_status: MOCK_AUTHORIZATION
authorization_contract: DUMMY_CONTRACT
""".strip(),
    )

    report = scan_repo_for_secrets(
        tmp_path,
        scan_paths=["config"],
        tracked_only=False,
    )

    assert report.status == "BLOCKED"
    assert [finding.key for finding in report.findings] == [
        "authorization_schema_version",
        "authorization_status",
        "authorization_contract",
    ]


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
