from __future__ import annotations

from unittest.mock import Mock

import app.canonical_v13.bootstrap as bootstrap


def _verification() -> bootstrap.BootstrapVerification:
    return bootstrap.BootstrapVerification(
        accepted=True,
        problems=(),
        table_count=59,
        business_row_count=None,
        capability_role_count=18,
        explicit_acl_count=366,
    )


def test_optional_service_principal_groups_compose_only_when_complete(
    monkeypatch,
) -> None:
    connection = Mock()
    connection.execute.return_value.scalars.return_value = (
        "phase9_approval_login",
        "phase9_order_login",
        "runtime_login",
    )
    observed: dict[str, str] = {}

    def fake_verify(_connection, **kwargs):
        observed.update(kwargs["service_principals"])
        return _verification()

    monkeypatch.setattr(bootstrap, "verify_postgresql_bootstrap", fake_verify)
    result = bootstrap.verify_postgresql_bootstrap_with_optional_service_principals(
        connection,
        role_mapping=bootstrap.local_role_mapping(),
        required_service_principals={"api_login": "canonical_api_reader"},
        optional_service_principal_groups={
            "phase9": {
                "phase9_approval_login": "canonical_approval_writer",
                "phase9_order_login": "canonical_order_writer",
            },
            "runtime": {"runtime_login": "canonical_runtime_reader"},
        },
        require_zero_business_rows=False,
    )

    assert result.accepted is True
    assert observed == {
        "api_login": "canonical_api_reader",
        "phase9_approval_login": "canonical_approval_writer",
        "phase9_order_login": "canonical_order_writer",
        "runtime_login": "canonical_runtime_reader",
    }


def test_partial_optional_service_principal_group_fails_closed(monkeypatch) -> None:
    connection = Mock()
    connection.execute.return_value.scalars.return_value = ("phase9_order_login",)
    observed: dict[str, str] = {}

    def fake_verify(_connection, **kwargs):
        observed.update(kwargs["service_principals"])
        return _verification()

    monkeypatch.setattr(bootstrap, "verify_postgresql_bootstrap", fake_verify)
    result = bootstrap.verify_postgresql_bootstrap_with_optional_service_principals(
        connection,
        role_mapping=bootstrap.local_role_mapping(),
        required_service_principals={"api_login": "canonical_api_reader"},
        optional_service_principal_groups={
            "phase9": {
                "phase9_approval_login": "canonical_approval_writer",
                "phase9_order_login": "canonical_order_writer",
            }
        },
        require_zero_business_rows=False,
    )

    assert result.accepted is False
    assert result.problems == (
        "partial optional service principal group phase9: observed=1 expected=2",
    )
    assert observed == {"api_login": "canonical_api_reader"}


def test_absent_optional_service_principal_groups_preserve_required_contract(
    monkeypatch,
) -> None:
    connection = Mock()
    connection.execute.return_value.scalars.return_value = ()
    observed: dict[str, str] = {}

    def fake_verify(_connection, **kwargs):
        observed.update(kwargs["service_principals"])
        return _verification()

    monkeypatch.setattr(bootstrap, "verify_postgresql_bootstrap", fake_verify)
    result = bootstrap.verify_postgresql_bootstrap_with_optional_service_principals(
        connection,
        role_mapping=bootstrap.local_role_mapping(),
        required_service_principals={"api_login": "canonical_api_reader"},
        optional_service_principal_groups={
            "phase9": {"phase9_order_login": "canonical_order_writer"},
            "runtime": {"runtime_login": "canonical_runtime_reader"},
        },
        require_zero_business_rows=False,
    )

    assert result.accepted is True
    assert observed == {"api_login": "canonical_api_reader"}
