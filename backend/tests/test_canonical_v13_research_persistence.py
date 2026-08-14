from __future__ import annotations

import pytest

from app.canonical_v13.research_persistence import (
    RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY,
    CanonicalResearchPersistenceBlocked,
    research_service_principal,
    resolve_research_persistence_urls,
)
from app.canonical_v13.role_mapping import CanonicalRoleMapping


def _environment(mapping: CanonicalRoleMapping) -> dict[str, str]:
    return {
        environment_name: (
            "postgresql+psycopg://"
            f"{research_service_principal(mapping, logical_role)}"
            ":not-a-secret@127.0.0.1/canonical_v13"
        )
        for logical_role, environment_name in (
            RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY.items()
        )
    }


def test_research_persistence_requires_exact_distinct_roles_on_one_database() -> None:
    mapping = CanonicalRoleMapping.from_prefix("v13_")
    environment = _environment(mapping)
    resolved = resolve_research_persistence_urls(
        environment, role_mapping=mapping
    )
    assert tuple(resolved.urls) == tuple(RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY)
    assert {url.database for url in resolved.urls.values()} == {"canonical_v13"}
    assert len({url.username for url in resolved.urls.values()}) == 4

    missing = dict(environment)
    missing.pop(next(iter(RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY.values())))
    with pytest.raises(
        CanonicalResearchPersistenceBlocked,
        match="BLOCKED_RESEARCH_DATABASE_URL_UNSET",
    ):
        resolve_research_persistence_urls(missing, role_mapping=mapping)

    broad = dict(environment)
    scoring_environment = RESEARCH_PERSISTENCE_ENV_BY_CAPABILITY[
        "canonical_scoring_writer"
    ]
    broad[scoring_environment] = (
        "postgresql+psycopg://v13_research_writer@127.0.0.1/canonical_v13"
    )
    with pytest.raises(
        CanonicalResearchPersistenceBlocked,
        match="BLOCKED_RESEARCH_ROLE_IDENTITY",
    ):
        resolve_research_persistence_urls(broad, role_mapping=mapping)

    split = dict(environment)
    split[scoring_environment] = (
        "postgresql+psycopg://v13_scoring_login@127.0.0.1/other"
    )
    with pytest.raises(
        CanonicalResearchPersistenceBlocked,
        match="BLOCKED_RESEARCH_DATABASE_SPLIT",
    ):
        resolve_research_persistence_urls(split, role_mapping=mapping)
