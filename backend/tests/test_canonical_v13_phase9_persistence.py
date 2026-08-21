from __future__ import annotations

import pytest

from app.canonical_v13.bootstrap import local_role_mapping
from app.canonical_v13.phase9_persistence import (
    PHASE9_PERSISTENCE_ENV_BY_CAPABILITY,
    CanonicalPhase9PersistenceBlocked,
    phase9_service_principal,
    resolve_phase9_persistence_urls,
)


def _environment(*, database: str = "freqtrade_ai_v13") -> dict[str, str]:
    mapping = local_role_mapping()
    return {
        environment_name: (
            "postgresql+psycopg://"
            f"{phase9_service_principal(mapping, capability)}:redacted@"
            f"127.0.0.1:5432/{database}"
        )
        for capability, environment_name in PHASE9_PERSISTENCE_ENV_BY_CAPABILITY.items()
    }


def test_phase9_persistence_requires_eight_distinct_exact_logins_on_one_database() -> (
    None
):
    resolved = resolve_phase9_persistence_urls(
        _environment(), role_mapping=local_role_mapping()
    )
    assert len(resolved.urls) == 8
    assert len({url.username for url in resolved.urls.values()}) == 8
    assert {url.database for url in resolved.urls.values()} == {"freqtrade_ai_v13"}


def test_phase9_persistence_fails_closed_on_missing_wrong_or_split_identity() -> None:
    mapping = local_role_mapping()
    environment = _environment()
    first_capability, first_name = next(
        iter(PHASE9_PERSISTENCE_ENV_BY_CAPABILITY.items())
    )
    missing = dict(environment)
    missing.pop(first_name)
    with pytest.raises(CanonicalPhase9PersistenceBlocked, match="DATABASE_URL_UNSET"):
        resolve_phase9_persistence_urls(missing, role_mapping=mapping)

    wrong = dict(environment)
    wrong[first_name] = wrong[first_name].replace(
        phase9_service_principal(mapping, first_capability),
        "freqtrade_ai_v13_control_login",
    )
    with pytest.raises(CanonicalPhase9PersistenceBlocked, match="ROLE_IDENTITY"):
        resolve_phase9_persistence_urls(wrong, role_mapping=mapping)

    split = dict(environment)
    split[first_name] = split[first_name].rsplit("/", 1)[0] + "/another_database"
    with pytest.raises(
        CanonicalPhase9PersistenceBlocked,
        match="DATABASE_OR_IDENTITY_SEPARATION",
    ):
        resolve_phase9_persistence_urls(split, role_mapping=mapping)
