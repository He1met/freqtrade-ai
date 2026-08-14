"""Fail-closed mapping from logical capabilities to PostgreSQL role names."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Final, Mapping

from app.canonical_v13.manifest import READER_IDENTITIES, WRITER_IDENTITIES


LOGICAL_ROLE_IDENTITIES: Final[tuple[str, ...]] = (
    *WRITER_IDENTITIES,
    *READER_IDENTITIES,
)
POSTGRESQL_ROLE_PATTERN: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class CanonicalRoleMappingBlocked(RuntimeError):
    """A role mapping is incomplete, ambiguous, or unsafe."""


@dataclass(frozen=True)
class CanonicalRoleMapping:
    """One total and injective logical-to-physical role mapping."""

    roles: Mapping[str, str]
    mapping_digest: str

    @classmethod
    def exact(cls, roles: Mapping[str, str]) -> "CanonicalRoleMapping":
        observed = dict(roles)
        expected = set(LOGICAL_ROLE_IDENTITIES)
        missing = sorted(expected - set(observed))
        extra = sorted(set(observed) - expected)
        if missing or extra:
            raise CanonicalRoleMappingBlocked(
                "BLOCKED_ROLE_MAPPING_KEYS: "
                f"missing={missing!r} extra={extra!r}"
            )
        invalid = sorted(
            physical
            for physical in observed.values()
            if not POSTGRESQL_ROLE_PATTERN.fullmatch(physical)
        )
        if invalid:
            raise CanonicalRoleMappingBlocked(
                f"BLOCKED_ROLE_MAPPING_IDENTIFIER: invalid={invalid!r}"
            )
        if len(set(observed.values())) != len(observed):
            raise CanonicalRoleMappingBlocked(
                "BLOCKED_ROLE_MAPPING_DUPLICATE: physical roles must be unique"
            )
        canonical = {
            logical: observed[logical] for logical in LOGICAL_ROLE_IDENTITIES
        }
        encoded = json.dumps(
            canonical, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return cls(
            roles=MappingProxyType(canonical),
            mapping_digest=sha256(encoded).hexdigest(),
        )

    @classmethod
    def identity(cls) -> "CanonicalRoleMapping":
        """Logical-name rendering for offline design review only."""

        return cls.exact({role: role for role in LOGICAL_ROLE_IDENTITIES})

    @classmethod
    def from_prefix(cls, prefix: str) -> "CanonicalRoleMapping":
        if not prefix or not POSTGRESQL_ROLE_PATTERN.fullmatch(prefix + "x"):
            raise CanonicalRoleMappingBlocked(
                "BLOCKED_ROLE_MAPPING_PREFIX: prefix is not PostgreSQL-safe"
            )
        return cls.exact(
            {
                role: prefix + role.removeprefix("canonical_")
                for role in LOGICAL_ROLE_IDENTITIES
            }
        )

    def physical(self, logical_role: str) -> str:
        try:
            return self.roles[logical_role]
        except KeyError as exc:
            raise CanonicalRoleMappingBlocked(
                f"BLOCKED_ROLE_MAPPING_UNKNOWN_CAPABILITY: {logical_role!r}"
            ) from exc


__all__ = [
    "LOGICAL_ROLE_IDENTITIES",
    "CanonicalRoleMapping",
    "CanonicalRoleMappingBlocked",
]
