from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from app.services.operator_authorization import OPERATOR_TOKEN_ENV


@dataclass(frozen=True)
class OwnerReadAccess:
    actor: str = "local-owner"


def require_owner_read_access(
    operator_token: Optional[str] = Header(default=None, alias="X-Operator-Token"),
) -> OwnerReadAccess:
    """Authorize local owner reads without adding write idempotency semantics."""

    configured_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
    if not configured_token:
        raise _owner_read_error(
            503,
            "OWNER_READ_AUTHORIZATION_UNAVAILABLE",
            "Local owner read authorization is not configured in ENV.",
        )
    supplied_token = operator_token or ""
    if not supplied_token or not hmac.compare_digest(configured_token, supplied_token):
        raise _owner_read_error(
            401,
            "OWNER_READ_UNAUTHORIZED",
            "Local owner read authorization was rejected.",
        )
    return OwnerReadAccess()


def _owner_read_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "operation_status": "BLOCKED",
            "context": {"credential_values_recorded": False},
        },
    )
