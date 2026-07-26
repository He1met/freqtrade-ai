from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


OkxReadErrorKind = Literal[
    "UNSAFE_TARGET",
    "UNAUTHORIZED",
    "TIMEOUT",
    "NETWORK",
    "RATE_LIMITED",
    "HTTP_ERROR",
    "BUSINESS_ERROR",
    "INVALID_RESPONSE",
    "STALE_DATA",
    "INVALID_REQUEST",
]
OkxReadErrorStatus = Literal["BLOCKED", "FAILED"]


@dataclass(frozen=True)
class OkxReadAdapterError(RuntimeError):
    kind: OkxReadErrorKind
    status: OkxReadErrorStatus
    message: str
    retryable: bool = False
    http_status: Optional[int] = None
    okx_code: Optional[str] = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "okx_code": self.okx_code,
        }
