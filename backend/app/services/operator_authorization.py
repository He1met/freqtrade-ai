from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Optional
from uuid import uuid4

from fastapi import Header, HTTPException


LOGGER = logging.getLogger(__name__)
OPERATOR_TOKEN_ENV = "FREQTRADE_AI_OPERATOR_TOKEN"
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@dataclass(frozen=True)
class OperatorRequestHeaders:
    operator_token: Optional[str]
    idempotency_key: Optional[str]
    provider_authorization: Optional[str]


@dataclass(frozen=True)
class _CachedOutcome:
    result: Any = None
    error_status_code: Optional[int] = None
    error_detail: Any = None


def operator_request_headers(
    operator_token: Optional[str] = Header(default=None, alias="X-Operator-Token"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    provider_authorization: Optional[str] = Header(
        default=None,
        alias="X-Provider-Authorization",
    ),
) -> OperatorRequestHeaders:
    return OperatorRequestHeaders(
        operator_token=operator_token,
        idempotency_key=idempotency_key,
        provider_authorization=provider_authorization,
    )


class OperatorRequestCoordinator:
    """Local-only authorization, idempotency, and provider single-flight boundary."""

    def __init__(self, *, max_cached_outcomes: int = 512) -> None:
        self._lock = Lock()
        self._inflight: set[tuple[str, str]] = set()
        self._outcomes: dict[tuple[str, str], _CachedOutcome] = {}
        self._request_digests: dict[tuple[str, str], str] = {}
        self._provider_inflight: Optional[tuple[str, str]] = None
        self._max_cached_outcomes = max_cached_outcomes

    def execute(
        self,
        headers: OperatorRequestHeaders,
        *,
        operation: str,
        provider_call: bool,
        request_payload: Any,
        handler: Callable[[], Any],
    ) -> Any:
        key = self._authorize_and_validate(headers, operation=operation, provider_call=provider_call)
        cache_key = (operation, key)
        digest = _idempotency_digest(key)
        request_digest = _request_digest(request_payload)

        with self._lock:
            previous_request_digest = self._request_digests.get(cache_key)
            if previous_request_digest is not None and previous_request_digest != request_digest:
                self._audit(operation, "BLOCKED", digest, provider_call, reason="idempotency_payload_mismatch")
                raise _operator_http_error(
                    409,
                    "BLOCKED",
                    "Idempotency-Key was already used with a different request payload.",
                    operation,
                    digest,
                )
            cached = self._outcomes.get(cache_key)
            if cached is not None:
                self._audit(operation, "REPLAYED", digest, provider_call)
                return self._replay(cached)
            if cache_key in self._inflight:
                self._audit(operation, "BLOCKED", digest, provider_call, reason="duplicate_inflight")
                raise _operator_http_error(
                    409,
                    "BLOCKED",
                    "An identical operator request is already in progress.",
                    operation,
                    digest,
                )
            if provider_call and self._provider_inflight is not None:
                self._audit(operation, "BLOCKED", digest, provider_call, reason="provider_single_flight")
                raise _operator_http_error(
                    409,
                    "BLOCKED",
                    "Another real Provider request is already in progress.",
                    operation,
                    digest,
                )
            self._inflight.add(cache_key)
            self._request_digests[cache_key] = request_digest
            if provider_call:
                self._provider_inflight = cache_key

        try:
            result = handler()
        except HTTPException as exc:
            outcome = _CachedOutcome(
                error_status_code=exc.status_code,
                error_detail=copy.deepcopy(exc.detail),
            )
            self._finish(cache_key, outcome, provider_call)
            self._audit(
                operation,
                _http_error_audit_status(exc.detail),
                digest,
                provider_call,
                reason="http_error",
            )
            raise
        except Exception:
            outcome = _CachedOutcome(
                error_status_code=500,
                error_detail={
                    "message": "Operator request failed without a safe application response.",
                    "operation_status": "FAILED",
                    "audit": {
                        "operation": operation,
                        "idempotency_key_digest": digest,
                        "credential_values_recorded": False,
                    },
                },
            )
            self._finish(cache_key, outcome, provider_call)
            self._audit(operation, "FAILED", digest, provider_call, reason="unhandled_error")
            raise

        self._finish(cache_key, _CachedOutcome(result=copy.deepcopy(result)), provider_call)
        self._audit(operation, "SUCCESS", digest, provider_call)
        return result

    def reset_for_tests(self) -> None:
        with self._lock:
            self._inflight.clear()
            self._outcomes.clear()
            self._request_digests.clear()
            self._provider_inflight = None

    def _authorize_and_validate(
        self,
        headers: OperatorRequestHeaders,
        *,
        operation: str,
        provider_call: bool,
    ) -> str:
        configured_token = os.environ.get(OPERATOR_TOKEN_ENV, "")
        if not configured_token:
            self._audit(operation, "BLOCKED", None, provider_call, reason="operator_token_not_configured")
            raise _operator_http_error(
                503,
                "BLOCKED",
                "Local operator authorization is not configured in ENV.",
                operation,
                None,
            )

        supplied_token = headers.operator_token or ""
        if not supplied_token or not hmac.compare_digest(configured_token, supplied_token):
            self._audit(operation, "UNAUTHORIZED", None, provider_call, reason="operator_token_rejected")
            raise _operator_http_error(
                401,
                "UNAUTHORIZED",
                "Local operator authorization was rejected.",
                operation,
                None,
            )

        key = (headers.idempotency_key or "").strip()
        if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
            self._audit(operation, "BLOCKED", None, provider_call, reason="invalid_idempotency_key")
            raise _operator_http_error(
                428,
                "BLOCKED",
                "A valid Idempotency-Key header is required.",
                operation,
                None,
            )

        if provider_call and (headers.provider_authorization or "").strip().lower() != "once":
            digest = _idempotency_digest(key)
            self._audit(operation, "BLOCKED", digest, provider_call, reason="provider_authorization_missing")
            raise _operator_http_error(
                409,
                "BLOCKED",
                "A real Provider attempt requires X-Provider-Authorization: once.",
                operation,
                digest,
            )
        return key

    def _finish(
        self,
        cache_key: tuple[str, str],
        outcome: _CachedOutcome,
        provider_call: bool,
    ) -> None:
        with self._lock:
            self._inflight.discard(cache_key)
            if provider_call and self._provider_inflight == cache_key:
                self._provider_inflight = None
            self._outcomes[cache_key] = outcome
            while len(self._outcomes) > self._max_cached_outcomes:
                oldest_key = next(iter(self._outcomes))
                self._outcomes.pop(oldest_key, None)
                self._request_digests.pop(oldest_key, None)

    @staticmethod
    def _replay(outcome: _CachedOutcome) -> Any:
        if outcome.error_status_code is not None:
            raise HTTPException(
                status_code=outcome.error_status_code,
                detail=copy.deepcopy(outcome.error_detail),
            )
        return copy.deepcopy(outcome.result)

    @staticmethod
    def _audit(
        operation: str,
        status: str,
        digest: Optional[str],
        provider_call: bool,
        *,
        reason: Optional[str] = None,
    ) -> None:
        LOGGER.info(
            "operator_request_audit event_id=%s operation=%s status=%s idempotency_key_digest=%s "
            "provider_call=%s reason=%s credential_values_recorded=false",
            f"operator-{uuid4().hex}",
            operation,
            status,
            digest or "none",
            str(provider_call).lower(),
            reason or "none",
        )


def provider_is_real(provider: Any) -> bool:
    metadata_getter = getattr(provider, "metadata_snapshot", None)
    if callable(metadata_getter):
        metadata = metadata_getter()
        if isinstance(metadata, dict) and "real_provider" in metadata:
            return bool(metadata["real_provider"])
    return getattr(provider, "provider_name", "") != "fake"


def _idempotency_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _request_digest(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _http_error_audit_status(detail: Any) -> str:
    if not isinstance(detail, dict):
        return "FAILED"
    status = detail.get("operation_status")
    if isinstance(status, str) and status in {"UNAUTHORIZED", "BLOCKED", "FAILED"}:
        return status
    evidence = detail.get("evidence")
    if isinstance(evidence, dict):
        evidence_status = evidence.get("status")
        if isinstance(evidence_status, str) and evidence_status in {"BLOCKED", "FAILED"}:
            return evidence_status
    return "FAILED"


def _operator_http_error(
    status_code: int,
    operation_status: str,
    message: str,
    operation: str,
    digest: Optional[str],
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "message": message,
            "operation_status": operation_status,
            "audit": {
                "operation": operation,
                "idempotency_key_digest": digest,
                "credential_values_recorded": False,
            },
            "evidence": {
                "status": "BLOCKED" if operation_status != "FAILED" else "FAILED",
                "acceptance_ready": False,
            },
        },
    )


operator_request_coordinator = OperatorRequestCoordinator()
