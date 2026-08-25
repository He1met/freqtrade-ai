from __future__ import annotations

import re
from typing import Any, Mapping, Optional


CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")


class OkxDemoWriteBlocked(RuntimeError):
    """The requested write is unsafe or explicitly rejected."""


class OkxDemoRecoveryRequired(RuntimeError):
    """A write may have happened and must be reconciled before another write."""


class OkxDemoWriteRejected(OkxDemoWriteBlocked):
    """The exchange explicitly rejected a single write."""


class OkxDemoTransportError(RuntimeError):
    """A sanitized transport failure with write-outcome classification."""

    _FAILURE_KINDS = frozenset(
        {
            "UNCLASSIFIED",
            "HTTP_ERROR_AMBIGUOUS",
            "NETWORK_ERROR",
            "HTTP_STATUS_NON_200",
            "RESPONSE_DECODE_ERROR",
        }
    )
    _CLIENT_ORDER_ID_STATES = frozenset(
        {"UNKNOWN", "MATCH", "MISSING", "MISMATCH"}
    )

    def __init__(
        self,
        *,
        unknown_write_outcome: bool,
        failure_kind: str = "UNCLASSIFIED",
        http_status_code: int | None = None,
        okx_code: str | None = None,
        okx_s_code: str | None = None,
        client_order_id_state: str = "UNKNOWN",
    ) -> None:
        if (
            failure_kind not in self._FAILURE_KINDS
            or client_order_id_state not in self._CLIENT_ORDER_ID_STATES
            or (
                http_status_code is not None
                and (
                    isinstance(http_status_code, bool)
                    or not isinstance(http_status_code, int)
                    or not 100 <= http_status_code <= 599
                )
            )
            or any(
                value is not None
                and (not isinstance(value, str) or not value.isdigit())
                for value in (okx_code, okx_s_code)
            )
        ):
            raise ValueError("invalid sanitized OKX transport classification")
        super().__init__("OKX Demo transport failed")
        self.unknown_write_outcome = unknown_write_outcome
        self.failure_kind = failure_kind
        self.http_status_code = http_status_code
        self.okx_code = okx_code
        self.okx_s_code = okx_s_code
        self.client_order_id_state = client_order_id_state

    @property
    def safe_diagnostic(self) -> str:
        parts = [self.failure_kind]
        for name, value in (
            ("http_status", self.http_status_code),
            ("okx_code", self.okx_code),
            ("okx_s_code", self.okx_s_code),
        ):
            if value is not None:
                parts.append(f"{name}={value}")
        if self.client_order_id_state != "UNKNOWN":
            parts.append(f"client_order_id={self.client_order_id_state}")
        return ":".join(parts)


def validate_client_order_id(value: str) -> str:
    if not isinstance(value, str) or not CLIENT_ORDER_ID_PATTERN.fullmatch(value):
        raise OkxDemoWriteBlocked(
            "client order ID must be 1-32 case-sensitive alphanumeric characters"
        )
    return value


def validate_write_item(
    payload: Any,
    *,
    expected_client_order_id: Optional[str],
    reason: str,
    client_order_id_field: str = "clOrdId",
    require_order_id: bool = True,
) -> Mapping[str, Any]:
    """Classify one OKX write acknowledgement using the #444 contract.

    Only an explicit top-level non-zero code or a single-item explicit non-zero
    sCode is a proven rejection. Once top-level code=0, every missing,
    duplicated, malformed, or identity-mismatched field is an unknown outcome.
    """

    if not isinstance(payload, dict) or "code" not in payload:
        raise OkxDemoRecoveryRequired(reason + "_OUTCOME_UNKNOWN")
    if str(payload.get("code")) != "0":
        raise OkxDemoWriteRejected(reason)
    data = payload.get("data")
    if (
        not isinstance(data, list)
        or len(data) != 1
        or not isinstance(data[0], dict)
    ):
        raise OkxDemoRecoveryRequired(reason + "_OUTCOME_UNKNOWN")
    item = data[0]
    if "sCode" not in item:
        raise OkxDemoRecoveryRequired(reason + "_OUTCOME_UNKNOWN")
    if str(item.get("sCode")) != "0":
        raise OkxDemoWriteRejected(reason)
    if (
        expected_client_order_id is not None
        and item.get(client_order_id_field) != expected_client_order_id
    ):
        raise OkxDemoRecoveryRequired(reason + "_OUTCOME_UNKNOWN")
    if require_order_id:
        order_id = item.get("ordId")
        if not isinstance(order_id, str) or not order_id:
            raise OkxDemoRecoveryRequired(reason + "_OUTCOME_UNKNOWN")
    return item
