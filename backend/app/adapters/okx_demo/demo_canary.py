from __future__ import annotations

import argparse
import base64
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from app.adapters.okx_demo.credential_preflight import (
    ALLOW_REAL_FUNDS_ENV,
    EXECUTION_TARGET_ENV,
    OKX_DEMO_ACCOUNT_FINGERPRINT_ENV,
    OKX_DEMO_CREDENTIAL_ENV_NAMES,
    OKX_DEMO_REST_URL,
    OkxDemoPreflightBlocked,
    REST_URL_ENV,
    account_fingerprint,
)
from app.adapters.okx_demo.write_semantics import (
    CLIENT_ORDER_ID_PATTERN,
    OkxDemoRecoveryRequired,
    OkxDemoTransportError,
    OkxDemoWriteRejected,
    validate_write_item,
)


ALLOW_DEMO_ORDER_ENV = "FREQTRADE_AI_ALLOW_DEMO_ORDER"
DEFAULT_INSTRUMENT = "BTC-USDT-SWAP"
ALLOWED_INSTRUMENTS = frozenset({DEFAULT_INSTRUMENT})
SIMULATED_TRADING_HEADER = ("x-simulated-trading", "1")
REQUEST_TIMEOUT_SECONDS = 10
MAX_NOTIONAL_USDT = Decimal("2000")
ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[4]
    / ".freqtrade-ai"
    / "runtime"
    / "okx-demo-canary"
)


class OkxDemoCanaryBlocked(RuntimeError):
    """A prerequisite or reconciliation result is unsafe or unknown."""


class CanaryTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
        write: bool = False,
    ) -> Any:
        ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OkxDemoHttpTransport:
    """Minimal signed REST transport fixed to OKX Demo."""

    def __init__(self, environment: Mapping[str, str], *, opener=None) -> None:
        self._signing_bundle = tuple(
            _required(environment, name)
            for name in (
                "OKX_DEMO_API_KEY",
                "OKX_DEMO_API_SECRET",
                "OKX_DEMO_API_PASSPHRASE",
            )
        )
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
        write: bool = False,
    ) -> Any:
        normalized_method = method.upper()
        query = urlencode(list((params or {}).items()))
        request_path = path + ("?" + query if query else "")
        body_text = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if body is not None
            else ""
        )
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        message = timestamp + normalized_method + request_path + body_text
        signature = base64.b64encode(
            hmac.new(
                self._signing_bundle[1].encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        request = Request(
            OKX_DEMO_REST_URL + request_path,
            method=normalized_method,
            data=body_text.encode("utf-8") if body_text else None,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "OK-ACCESS-KEY": self._signing_bundle[0],
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._signing_bundle[2],
                SIMULATED_TRADING_HEADER[0]: SIMULATED_TRADING_HEADER[1],
            },
        )
        try:
            with self._opener.open(
                request,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status != 200:
                    raise OkxDemoTransportError(
                        unknown_write_outcome=write and response.status >= 500
                    )
                raw_payload = response.read()
        except HTTPError as exc:
            raise OkxDemoTransportError(
                unknown_write_outcome=write
            ) from None
        except (URLError, TimeoutError, OSError):
            raise OkxDemoTransportError(unknown_write_outcome=write) from None
        try:
            return json.loads(raw_payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise OkxDemoTransportError(unknown_write_outcome=write) from None


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if (
        not value
        or len(value) > 16384
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise OkxDemoCanaryBlocked("CREDENTIAL_BUNDLE_INCOMPLETE")
    return value


def _validate_environment(environment: Mapping[str, str]) -> None:
    if environment.get(ALLOW_DEMO_ORDER_ENV) != "true":
        raise OkxDemoCanaryBlocked("EXPLICIT_DEMO_ORDER_AUTHORIZATION_REQUIRED")
    if environment.get(EXECUTION_TARGET_ENV) != "OKX_DEMO":
        raise OkxDemoCanaryBlocked("EXECUTION_TARGET_MUST_BE_OKX_DEMO")
    if environment.get(ALLOW_REAL_FUNDS_ENV) != "false":
        raise OkxDemoCanaryBlocked("REAL_FUNDS_MUST_BE_DISABLED")
    if environment.get(REST_URL_ENV) != OKX_DEMO_REST_URL:
        raise OkxDemoCanaryBlocked("REST_URL_MUST_BE_FIXED_OKX_DEMO")
    for name in (*OKX_DEMO_CREDENTIAL_ENV_NAMES, OKX_DEMO_ACCOUNT_FINGERPRINT_ENV):
        _required(environment, name)


def _validate_instrument(instrument: str) -> str:
    if instrument not in ALLOWED_INSTRUMENTS:
        raise OkxDemoCanaryBlocked("INSTRUMENT_NOT_ALLOWLISTED")
    return instrument


def _validate_client_order_id(value: str) -> str:
    if not CLIENT_ORDER_ID_PATTERN.fullmatch(value):
        raise OkxDemoCanaryBlocked("INVALID_PREDETERMINED_CLIENT_ORDER_ID")
    return value


def _hash_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decimal(value: Any, reason: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OkxDemoCanaryBlocked(reason) from None
    if not parsed.is_finite():
        raise OkxDemoCanaryBlocked(reason)
    return parsed


def _top_level_data(payload: Any, reason: str) -> list[Mapping[str, Any]]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise OkxDemoCanaryBlocked(reason)
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise OkxDemoCanaryBlocked(reason)
    return data


def _write_item(
    payload: Any,
    *,
    expected_cl_ord_id: str,
    reason: str,
) -> Mapping[str, Any]:
    return validate_write_item(
        payload,
        expected_client_order_id=expected_cl_ord_id,
        reason=reason,
    )


def _query_order(
    transport: CanaryTransport,
    instrument: str,
    cl_ord_id: str,
    *,
    expected_price: str,
    expected_size: str,
) -> Mapping[str, Any]:
    payload = transport.request(
        "GET",
        "/api/v5/trade/order",
        params={"instId": instrument, "clOrdId": cl_ord_id},
    )
    data = _top_level_data(payload, "ORDER_QUERY_FAILED")
    if len(data) != 1:
        raise OkxDemoCanaryBlocked("ORDER_QUERY_FAILED")
    item = data[0]
    if (
        item.get("instId") != instrument
        or item.get("clOrdId") != cl_ord_id
        or item.get("tdMode") != "isolated"
        or item.get("ordType") != "post_only"
        or item.get("side") != "buy"
        or item.get("posSide") != "net"
        or item.get("px") != expected_price
        or item.get("sz") != expected_size
    ):
        raise OkxDemoCanaryBlocked("ORDER_QUERY_IDENTITY_MISMATCH")
    order_id = item.get("ordId")
    if not isinstance(order_id, str) or not order_id:
        raise OkxDemoCanaryBlocked("ORDER_QUERY_IDENTITY_MISMATCH")
    state = item.get("state")
    if state not in {
        "live",
        "partially_filled",
        "filled",
        "canceled",
        "mmp_canceled",
    }:
        raise OkxDemoCanaryBlocked("ORDER_STATE_UNKNOWN")
    accumulated_fill = _decimal(
        item.get("accFillSz"),
        "ORDER_FILL_STATE_UNKNOWN",
    )
    requested_size = _decimal(expected_size, "ORDER_QUERY_IDENTITY_MISMATCH")
    if accumulated_fill < 0 or accumulated_fill > requested_size:
        raise OkxDemoCanaryBlocked("ORDER_FILL_STATE_UNKNOWN")
    return item


def _position_size(payload: Any, instrument: str) -> Decimal:
    data = _top_level_data(payload, "POSITION_QUERY_FAILED")
    total = Decimal("0")
    for item in data:
        if item.get("instId") != instrument:
            continue
        if item.get("posSide") != "net" or item.get("mgnMode") != "isolated":
            raise OkxDemoCanaryBlocked("POSITION_QUERY_FAILED")
        total += _decimal(item.get("pos", "0"), "POSITION_QUERY_FAILED")
    return total


def _pending_count(payload: Any, instrument: str) -> int:
    data = _top_level_data(payload, "PENDING_ORDER_QUERY_FAILED")
    return sum(1 for item in data if item.get("instId") == instrument)


def _order_has_fill(order: Mapping[str, Any]) -> bool:
    filled = _decimal(order.get("accFillSz", "0"), "ORDER_FILL_STATE_UNKNOWN")
    return filled > 0 or order.get("state") in {"filled", "partially_filled"}


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _order_parameters(
    instrument_payload: Any,
    ticker_payload: Any,
    instrument: str,
) -> tuple[str, str]:
    instruments = _top_level_data(instrument_payload, "INSTRUMENT_METADATA_UNAVAILABLE")
    if (
        len(instruments) != 1
        or instruments[0].get("instId") != instrument
        or instruments[0].get("state") != "live"
    ):
        raise OkxDemoCanaryBlocked("INSTRUMENT_METADATA_UNAVAILABLE")
    metadata = instruments[0]
    tick_size = _decimal(metadata.get("tickSz"), "INSTRUMENT_METADATA_UNAVAILABLE")
    lot_size = _decimal(metadata.get("lotSz"), "INSTRUMENT_METADATA_UNAVAILABLE")
    minimum_size = _decimal(metadata.get("minSz"), "INSTRUMENT_METADATA_UNAVAILABLE")
    contract_value = _decimal(
        metadata.get("ctVal"),
        "INSTRUMENT_METADATA_UNAVAILABLE",
    )
    if tick_size <= 0 or lot_size <= 0 or minimum_size <= 0 or contract_value <= 0:
        raise OkxDemoCanaryBlocked("INSTRUMENT_METADATA_UNAVAILABLE")

    tickers = _top_level_data(ticker_payload, "MARKET_PRICE_UNAVAILABLE")
    if len(tickers) != 1 or tickers[0].get("instId") != instrument:
        raise OkxDemoCanaryBlocked("MARKET_PRICE_UNAVAILABLE")
    bid = _decimal(tickers[0].get("bidPx"), "MARKET_PRICE_UNAVAILABLE")
    if bid <= 0:
        raise OkxDemoCanaryBlocked("MARKET_PRICE_UNAVAILABLE")
    price = ((bid * Decimal("0.95")) / tick_size).to_integral_value(
        rounding=ROUND_DOWN
    ) * tick_size
    if price <= 0 or price >= bid:
        raise OkxDemoCanaryBlocked("SAFE_LIMIT_PRICE_UNAVAILABLE")
    size = (minimum_size / lot_size).to_integral_value(rounding=ROUND_UP) * lot_size
    if size * contract_value * price > MAX_NOTIONAL_USDT:
        raise OkxDemoCanaryBlocked("CANARY_NOTIONAL_EXCEEDS_LIMIT")
    return _format_decimal(price), _format_decimal(size)


def _query_pending(transport: CanaryTransport, instrument: str) -> Any:
    return transport.request(
        "GET",
        "/api/v5/trade/orders-pending",
        params={"instType": "SWAP", "instId": instrument},
    )


def _query_positions(transport: CanaryTransport, instrument: str) -> Any:
    return transport.request(
        "GET",
        "/api/v5/account/positions",
        params={"instType": "SWAP", "instId": instrument},
    )


def _query_fills(
    transport: CanaryTransport,
    instrument: str,
    order_id: str,
) -> Any:
    return transport.request(
        "GET",
        "/api/v5/trade/fills-history",
        params={"instType": "SWAP", "instId": instrument, "ordId": order_id},
    )


def _fill_count(payload: Any, instrument: str, order_id: str) -> int:
    data = _top_level_data(payload, "FILL_QUERY_FAILED")
    count = 0
    for item in data:
        if item.get("instId") == instrument and item.get("ordId") == order_id:
            count += 1
    return count


def _cancel_with_reconciliation(
    transport: CanaryTransport,
    instrument: str,
    cl_ord_id: str,
    *,
    expected_price: str,
    expected_size: str,
) -> Mapping[str, Any]:
    body = {"instId": instrument, "clOrdId": cl_ord_id}
    for attempt in range(2):
        try:
            payload = transport.request(
                "POST",
                "/api/v5/trade/cancel-order",
                body=body,
                write=True,
            )
            _write_item(
                payload,
                expected_cl_ord_id=cl_ord_id,
                reason="CANCEL_WRITE_FAILED",
            )
            break
        except OkxDemoTransportError as exc:
            if not exc.unknown_write_outcome:
                raise OkxDemoCanaryBlocked("CANCEL_TRANSPORT_FAILED") from None
            reconciled = _query_order(
                transport,
                instrument,
                cl_ord_id,
                expected_price=expected_price,
                expected_size=expected_size,
            )
            if reconciled.get("state") in {"canceled", "mmp_canceled"}:
                return reconciled
            if reconciled.get("state") not in {"live", "partially_filled"}:
                return reconciled
            if attempt == 1:
                return reconciled
    final_order: Optional[Mapping[str, Any]] = None
    for _attempt in range(3):
        final_order = _query_order(
            transport,
            instrument,
            cl_ord_id,
            expected_price=expected_price,
            expected_size=expected_size,
        )
        if final_order.get("state") in {"canceled", "mmp_canceled"}:
            return final_order
    if final_order is None:
        raise OkxDemoCanaryBlocked("ORDER_CANCEL_RECONCILIATION_FAILED")
    return final_order


def _cleanup_unexpected_position(
    transport: CanaryTransport,
    instrument: str,
    cleanup_cl_ord_id: str,
) -> bool:
    position = _position_size(_query_positions(transport, instrument), instrument)
    if position == 0:
        return True
    body = {
        "instId": instrument,
        "tdMode": "isolated",
        "side": "sell" if position > 0 else "buy",
        "posSide": "net",
        "ordType": "market",
        "sz": _format_decimal(abs(position)),
        "reduceOnly": True,
        "clOrdId": cleanup_cl_ord_id,
    }
    expected_side = "sell" if position > 0 else "buy"
    expected_size = _format_decimal(abs(position))
    try:
        payload = transport.request(
            "POST",
            "/api/v5/trade/order",
            body=body,
            write=True,
        )
        _write_item(
            payload,
            expected_cl_ord_id=cleanup_cl_ord_id,
            reason="REDUCE_ONLY_CLEANUP_FAILED",
        )
    except OkxDemoTransportError as exc:
        if not exc.unknown_write_outcome:
            return False
    except (
        OkxDemoCanaryBlocked,
        OkxDemoRecoveryRequired,
        OkxDemoWriteRejected,
    ):
        return False
    try:
        cleanup_order = _query_cleanup_order(
            transport,
            instrument,
            cleanup_cl_ord_id,
            expected_side=expected_side,
            expected_size=expected_size,
        )
        if cleanup_order.get("state") not in {
            "filled",
            "canceled",
            "mmp_canceled",
        }:
            return False
        return (
            _position_size(_query_positions(transport, instrument), instrument) == 0
        )
    except (OkxDemoCanaryBlocked, OkxDemoTransportError):
        return False


def _query_cleanup_order(
    transport: CanaryTransport,
    instrument: str,
    cleanup_cl_ord_id: str,
    *,
    expected_side: str,
    expected_size: str,
) -> Mapping[str, Any]:
    payload = transport.request(
        "GET",
        "/api/v5/trade/order",
        params={"instId": instrument, "clOrdId": cleanup_cl_ord_id},
    )
    data = _top_level_data(payload, "REDUCE_ONLY_CLEANUP_QUERY_FAILED")
    if len(data) != 1:
        raise OkxDemoCanaryBlocked("REDUCE_ONLY_CLEANUP_QUERY_FAILED")
    item = data[0]
    if (
        item.get("instId") != instrument
        or item.get("clOrdId") != cleanup_cl_ord_id
        or item.get("tdMode") != "isolated"
        or item.get("ordType") != "market"
        or item.get("side") != expected_side
        or item.get("posSide") != "net"
        or str(item.get("reduceOnly")).lower() != "true"
        or item.get("sz") != expected_size
        or not isinstance(item.get("ordId"), str)
        or not item.get("ordId")
    ):
        raise OkxDemoCanaryBlocked("REDUCE_ONLY_CLEANUP_IDENTITY_MISMATCH")
    if item.get("state") not in {
        "live",
        "partially_filled",
        "filled",
        "canceled",
        "mmp_canceled",
    }:
        raise OkxDemoCanaryBlocked("REDUCE_ONLY_CLEANUP_STATE_UNKNOWN")
    return item


def _reconcile_existing_cleanup(
    transport: CanaryTransport,
    instrument: str,
    cleanup_cl_ord_id: str,
) -> bool:
    try:
        payload = transport.request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": instrument, "clOrdId": cleanup_cl_ord_id},
        )
        data = _top_level_data(payload, "REDUCE_ONLY_CLEANUP_QUERY_FAILED")
        if len(data) != 1:
            return False
        side = data[0].get("side")
        size = str(data[0].get("sz", ""))
        if side not in {"buy", "sell"} or _decimal(
            size,
            "REDUCE_ONLY_CLEANUP_IDENTITY_MISMATCH",
        ) <= 0:
            return False
        cleanup_order = _query_cleanup_order(
            transport,
            instrument,
            cleanup_cl_ord_id,
            expected_side=side,
            expected_size=size,
        )
        if cleanup_order.get("state") not in {
            "filled",
            "canceled",
            "mmp_canceled",
        }:
            return False
        return (
            _position_size(_query_positions(transport, instrument), instrument) == 0
        )
    except (OkxDemoCanaryBlocked, OkxDemoTransportError):
        return False


def _persist_result(
    result: Dict[str, Any],
    artifact_dir: Optional[Path],
) -> Dict[str, Any]:
    target_dir = artifact_dir or ARTIFACT_ROOT
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = target_dir / "{}.json".format(result["artifact_id"])
    temporary_path = artifact_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary_path), str(artifact_path))
    return result


def _acquire_writer_lock(target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    handle = (target_dir / "writer.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise OkxDemoCanaryBlocked("ANOTHER_CANARY_WRITER_IS_ACTIVE") from None
    return handle


def _reserve_canary(
    *,
    target_dir: Path,
    artifact_id: str,
    instrument: str,
    cl_ord_id: str,
) -> None:
    cl_ord_id_hash = _hash_identifier(cl_ord_id)
    for path in target_dir.glob("*.json"):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise OkxDemoCanaryBlocked("CANARY_HISTORY_UNREADABLE") from None
        evidence = existing.get("evidence")
        if (
            isinstance(evidence, dict)
            and evidence.get("cl_ord_id_sha256") == cl_ord_id_hash
        ):
            raise OkxDemoCanaryBlocked("CLIENT_ORDER_ID_ALREADY_USED")
    _persist_result(
        _result(
            status="RESERVED",
            artifact_id=artifact_id,
            instrument=instrument,
            cl_ord_id=cl_ord_id,
            order_id=None,
            sequence=[],
            reason_code="DURABLE_INTENT_BEFORE_NETWORK_WRITE",
        ),
        target_dir,
    )


def _nonterminal_history(target_dir: Path) -> Optional[Dict[str, Any]]:
    nonterminal: list[Dict[str, Any]] = []
    for path in target_dir.glob("*.json"):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise OkxDemoCanaryBlocked("CANARY_HISTORY_UNREADABLE") from None
        if existing.get("status") in {
            "RESERVED",
            "RUNNING",
            "RECOVERY_REQUIRED",
            "UNKNOWN",
        }:
            nonterminal.append(existing)
    if len(nonterminal) > 1:
        raise OkxDemoCanaryBlocked("MULTIPLE_NONTERMINAL_CANARIES_PRESENT")
    return nonterminal[0] if nonterminal else None


def _result(
    *,
    status: str,
    artifact_id: str,
    instrument: str,
    cl_ord_id: str,
    order_id: Optional[str],
    sequence: list[str],
    reason_code: Optional[str],
    cleanup_cl_ord_id: Optional[str] = None,
    recovery_attempt_count: Optional[int] = None,
    recovery_last_attempt_at: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": status,
        "execution_target": "OKX_DEMO",
        "artifact_id": artifact_id,
        "instrument": instrument,
        "evidence": {
            "cl_ord_id_sha256": _hash_identifier(cl_ord_id),
            "order_id_sha256": _hash_identifier(order_id),
            "cleanup_cl_ord_id_sha256": _hash_identifier(cleanup_cl_ord_id),
            "simulated_trading_header": True,
            "sequence": sequence,
        },
    }
    if reason_code:
        payload["reason_code"] = reason_code
    if recovery_attempt_count is not None:
        payload["recovery_attempt_count"] = recovery_attempt_count
    if recovery_last_attempt_at is not None:
        payload["recovery_last_attempt_at"] = recovery_last_attempt_at
    return payload


def _append_sequence_once(sequence: list[str], value: str) -> None:
    if value not in sequence:
        sequence.append(value)


def _attest_account(
    transport: CanaryTransport,
    active_environment: Mapping[str, str],
) -> None:
    account_payload = transport.request("GET", "/api/v5/account/config")
    if (
        not isinstance(account_payload, dict)
        or account_payload.get("code") != "0"
        or not isinstance(account_payload.get("data"), list)
        or len(account_payload["data"]) != 1
        or not isinstance(account_payload["data"][0], dict)
    ):
        raise OkxDemoCanaryBlocked("ACCOUNT_ATTESTATION_FAILED")
    account = account_payload["data"][0]
    try:
        observed_fingerprint = account_fingerprint(account)
    except OkxDemoPreflightBlocked:
        raise OkxDemoCanaryBlocked("ACCOUNT_ATTESTATION_FAILED") from None
    if not hmac.compare_digest(
        observed_fingerprint,
        _required(active_environment, OKX_DEMO_ACCOUNT_FINGERPRINT_ENV),
    ):
        raise OkxDemoCanaryBlocked("ACCOUNT_ATTESTATION_FAILED")
    permissions = account.get("perm")
    if not isinstance(permissions, str) or {
        item.strip().lower() for item in permissions.split(",") if item.strip()
    } != {"read_only", "trade"} or account.get("acctLv") != "2":
        raise OkxDemoCanaryBlocked("ACCOUNT_ATTESTATION_FAILED")
    if account.get("posMode") != "net_mode":
        raise OkxDemoCanaryBlocked("DUAL_SIDE_CANARY_NOT_IMPLEMENTED")


def _query_recovery_order(
    transport: CanaryTransport,
    instrument: str,
    cl_ord_id: str,
) -> tuple[Mapping[str, Any], str, str]:
    payload = transport.request(
        "GET",
        "/api/v5/trade/order",
        params={"instId": instrument, "clOrdId": cl_ord_id},
    )
    data = _top_level_data(payload, "RECOVERY_ORDER_QUERY_FAILED")
    if len(data) != 1:
        raise OkxDemoRecoveryRequired("RECOVERY_ORDER_QUERY_FAILED")
    item = data[0]
    price = str(item.get("px", ""))
    size = str(item.get("sz", ""))
    if (
        item.get("instId") != instrument
        or item.get("clOrdId") != cl_ord_id
        or item.get("tdMode") != "isolated"
        or item.get("ordType") != "post_only"
        or item.get("side") != "buy"
        or item.get("posSide") != "net"
        or _decimal(price, "RECOVERY_ORDER_IDENTITY_MISMATCH") <= 0
        or _decimal(size, "RECOVERY_ORDER_IDENTITY_MISMATCH") <= 0
    ):
        raise OkxDemoRecoveryRequired("RECOVERY_ORDER_IDENTITY_MISMATCH")
    validated = _query_order(
        transport,
        instrument,
        cl_ord_id,
        expected_price=price,
        expected_size=size,
    )
    return validated, price, size


def _recover_nonterminal_canary(
    active_environment: Mapping[str, str],
    *,
    transport: Optional[CanaryTransport],
    artifact_dir: Path,
    existing: Mapping[str, Any],
) -> Dict[str, Any]:
    artifact_id = existing.get("artifact_id")
    instrument = existing.get("instrument")
    evidence = existing.get("evidence")
    raw_sequence = evidence.get("sequence", []) if isinstance(evidence, dict) else []
    sequence: list[str] = []
    if isinstance(raw_sequence, list):
        for value in raw_sequence:
            if isinstance(value, str):
                _append_sequence_once(sequence, value)
    if (
        not isinstance(artifact_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", artifact_id) is None
        or instrument not in ALLOWED_INSTRUMENTS
        or not isinstance(evidence, dict)
    ):
        raise OkxDemoCanaryBlocked("NONTERMINAL_CANARY_EVIDENCE_INVALID")
    cl_ord_id = _validate_client_order_id(
        "FTAICANARY{}".format(artifact_id[:20])
    )
    if evidence.get("cl_ord_id_sha256") != _hash_identifier(cl_ord_id):
        raise OkxDemoCanaryBlocked("NONTERMINAL_CANARY_EVIDENCE_INVALID")
    cleanup_cl_ord_id = _validate_client_order_id(
        "FTAICLEAN{}".format(hashlib.sha256(cl_ord_id.encode()).hexdigest()[:20])
    )
    cleanup_was_intended = (
        evidence.get("cleanup_cl_ord_id_sha256")
        == _hash_identifier(cleanup_cl_ord_id)
    )
    cleanup_intent_active = cleanup_was_intended
    previous_attempt_count = existing.get("recovery_attempt_count", 0)
    if (
        not isinstance(previous_attempt_count, int)
        or isinstance(previous_attempt_count, bool)
        or previous_attempt_count < 0
    ):
        previous_attempt_count = 0
    recovery_attempt_count = min(previous_attempt_count + 1, 1_000_000_000)
    recovery_last_attempt_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    if existing.get("status") == "RESERVED" and "place_intent_persisted" not in sequence:
        _append_sequence_once(sequence, "reservation_recovered_before_write")
        return _persist_result(
            _result(
                status="FAILED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=cl_ord_id,
                order_id=None,
                sequence=sequence,
                reason_code="RESERVATION_ABORTED_BEFORE_WRITE",
                recovery_attempt_count=recovery_attempt_count,
                recovery_last_attempt_at=recovery_last_attempt_at,
            ),
            artifact_dir,
        )

    active_transport = transport or OkxDemoHttpTransport(active_environment)
    order_id: Optional[str] = None
    try:
        _attest_account(active_transport, active_environment)
        _append_sequence_once(sequence, "recovery_started")
        order, price, size = _query_recovery_order(
            active_transport,
            instrument,
            cl_ord_id,
        )
        order_id = str(order["ordId"])
        if order.get("state") in {"live", "partially_filled"}:
            try:
                order = _cancel_with_reconciliation(
                    active_transport,
                    instrument,
                    cl_ord_id,
                    expected_price=price,
                    expected_size=size,
                )
            except (
                OkxDemoCanaryBlocked,
                OkxDemoRecoveryRequired,
                OkxDemoWriteRejected,
                OkxDemoTransportError,
            ):
                _append_sequence_once(sequence, "cancel_reconciliation_uncertain")

        fills = _fill_count(
            _query_fills(active_transport, instrument, order_id),
            instrument,
            order_id,
        )
        position = _position_size(
            _query_positions(active_transport, instrument),
            instrument,
        )
        if cleanup_was_intended:
            if not _reconcile_existing_cleanup(
                active_transport,
                instrument,
                cleanup_cl_ord_id,
            ):
                raise OkxDemoRecoveryRequired("RECOVERY_CLEANUP_UNVERIFIED")
        elif _order_has_fill(order) or fills or position != 0:
            cleanup_intent_active = True
            _persist_result(
                _result(
                    status="RECOVERY_REQUIRED",
                    artifact_id=artifact_id,
                    instrument=instrument,
                    cl_ord_id=cl_ord_id,
                    order_id=order_id,
                    sequence=sequence,
                    reason_code="RECOVERY_CLEANUP_INTENT_PERSISTED",
                    cleanup_cl_ord_id=cleanup_cl_ord_id,
                    recovery_attempt_count=recovery_attempt_count,
                    recovery_last_attempt_at=recovery_last_attempt_at,
                ),
                artifact_dir,
            )
            if not _cleanup_unexpected_position(
                active_transport,
                instrument,
                cleanup_cl_ord_id,
            ):
                raise OkxDemoRecoveryRequired("RECOVERY_CLEANUP_UNVERIFIED")
        order, _price, _size = _query_recovery_order(
            active_transport,
            instrument,
            cl_ord_id,
        )
        if order.get("state") not in {"canceled", "mmp_canceled", "filled"}:
            raise OkxDemoRecoveryRequired("ORIGINAL_ORDER_NOT_TERMINAL")
        if _pending_count(_query_pending(active_transport, instrument), instrument):
            raise OkxDemoRecoveryRequired("RECOVERY_PENDING_ORDERS_PRESENT")
        _query_fills(active_transport, instrument, order_id)
        if _position_size(_query_positions(active_transport, instrument), instrument) != 0:
            raise OkxDemoRecoveryRequired("RECOVERY_POSITION_PRESENT")
        _append_sequence_once(sequence, "recovery_verified")
        return _persist_result(
            _result(
                status="FAILED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=cl_ord_id,
                order_id=order_id,
                sequence=sequence,
                reason_code="PRIOR_CANARY_RECOVERED",
                cleanup_cl_ord_id=cleanup_cl_ord_id,
                recovery_attempt_count=recovery_attempt_count,
                recovery_last_attempt_at=recovery_last_attempt_at,
            ),
            artifact_dir,
        )
    except (
        OkxDemoCanaryBlocked,
        OkxDemoPreflightBlocked,
        OkxDemoRecoveryRequired,
        OkxDemoWriteRejected,
        OkxDemoTransportError,
    ):
        return _persist_result(
            _result(
                status="RECOVERY_REQUIRED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=cl_ord_id,
                order_id=order_id,
                sequence=sequence,
                reason_code="NONTERMINAL_OUTCOME_REQUIRES_RECOVERY",
                cleanup_cl_ord_id=(
                    cleanup_cl_ord_id if cleanup_intent_active else None
                ),
                recovery_attempt_count=recovery_attempt_count,
                recovery_last_attempt_at=recovery_last_attempt_at,
            ),
            artifact_dir,
        )


def _execute_canary(
    active_environment: Mapping[str, str],
    *,
    transport: Optional[CanaryTransport] = None,
    instrument: str,
    client_order_id: str,
    artifact_dir: Path,
    artifact_id: str,
) -> Dict[str, Any]:
    cleanup_cl_ord_id = _validate_client_order_id(
        "FTAICLEAN{}".format(hashlib.sha256(client_order_id.encode()).hexdigest()[:20])
    )
    sequence: list[str] = []
    order_id: Optional[str] = None
    active_transport = transport or OkxDemoHttpTransport(active_environment)

    def finish_unexpected_exposure(
        sequence_value: str,
        *,
        expected_price: str,
        expected_size: str,
    ) -> Dict[str, Any]:
        _persist_result(
            _result(
                status="RUNNING",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code="REDUCE_ONLY_CLEANUP_INTENT_PERSISTED",
                cleanup_cl_ord_id=cleanup_cl_ord_id,
            ),
            artifact_dir,
        )
        cleaned = _cleanup_unexpected_position(
            active_transport,
            instrument,
            cleanup_cl_ord_id,
        )
        sequence.append(sequence_value)
        original_terminal = False
        final_scope_verified = False
        try:
            reconciled_original = _query_order(
                active_transport,
                instrument,
                client_order_id,
                expected_price=expected_price,
                expected_size=expected_size,
            )
            original_terminal = reconciled_original.get("state") in {
                "canceled",
                "mmp_canceled",
                "filled",
            }
            pending_empty = (
                _pending_count(
                    _query_pending(active_transport, instrument),
                    instrument,
                )
                == 0
            )
            _query_fills(active_transport, instrument, str(order_id))
            position_empty = (
                _position_size(
                    _query_positions(active_transport, instrument),
                    instrument,
                )
                == 0
            )
            final_scope_verified = pending_empty and position_empty
        except (OkxDemoCanaryBlocked, OkxDemoTransportError):
            final_scope_verified = False
        verified = cleaned and original_terminal and final_scope_verified
        return _persist_result(
            _result(
                status="FAILED" if verified else "RECOVERY_REQUIRED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code=(
                    "UNEXPECTED_FILL_CLEANED"
                    if verified
                    else "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
                ),
                cleanup_cl_ord_id=cleanup_cl_ord_id,
            ),
            artifact_dir,
        )

    try:
        _attest_account(active_transport, active_environment)
        sequence.append("account_attested")
        if _pending_count(_query_pending(active_transport, instrument), instrument):
            raise OkxDemoCanaryBlocked("INITIAL_PENDING_ORDERS_PRESENT")
        if _position_size(_query_positions(active_transport, instrument), instrument) != 0:
            raise OkxDemoCanaryBlocked("INITIAL_POSITION_PRESENT")
        sequence.append("initial_scope_empty")

        instrument_payload = active_transport.request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": instrument},
        )
        ticker_payload = active_transport.request(
            "GET",
            "/api/v5/market/ticker",
            params={"instId": instrument},
        )
        price, size = _order_parameters(instrument_payload, ticker_payload, instrument)
        order_body = {
            "instId": instrument,
            "tdMode": "isolated",
            "side": "buy",
            "ordType": "post_only",
            "px": price,
            "sz": size,
            "posSide": "net",
            "clOrdId": client_order_id,
        }
        sequence.append("place_intent_persisted")
        _persist_result(
            _result(
                status="RUNNING",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=None,
                sequence=sequence,
                reason_code="PLACE_INTENT_PERSISTED",
            ),
            artifact_dir,
        )
        try:
            placed = active_transport.request(
                "POST",
                "/api/v5/trade/order",
                body=order_body,
                write=True,
            )
            place_item = _write_item(
                placed,
                expected_cl_ord_id=client_order_id,
                reason="PLACE_WRITE_FAILED",
            )
            order_id = str(place_item["ordId"])
            sequence.append("limit_order_accepted")
        except OkxDemoRecoveryRequired:
            sequence.append("place_outcome_unknown")
            try:
                reconciled = _query_order(
                    active_transport,
                    instrument,
                    client_order_id,
                    expected_price=price,
                    expected_size=size,
                )
            except (OkxDemoCanaryBlocked, OkxDemoTransportError):
                raise OkxDemoRecoveryRequired("PLACE_OUTCOME_UNRESOLVED") from None
            order_id = str(reconciled["ordId"])
            sequence.append("place_reconciled_by_cl_ord_id")
        except OkxDemoTransportError as exc:
            if not exc.unknown_write_outcome:
                raise OkxDemoCanaryBlocked("PLACE_TRANSPORT_FAILED") from None
            sequence.append("place_outcome_unknown")
            try:
                reconciled = _query_order(
                    active_transport,
                    instrument,
                    client_order_id,
                    expected_price=price,
                    expected_size=size,
                )
            except (OkxDemoCanaryBlocked, OkxDemoTransportError):
                raise OkxDemoRecoveryRequired("PLACE_OUTCOME_UNRESOLVED") from None
            order_id = str(reconciled["ordId"])
            sequence.append("place_reconciled_by_cl_ord_id")

        order = _query_order(
            active_transport,
            instrument,
            client_order_id,
            expected_price=price,
            expected_size=size,
        )
        order_id = str(order["ordId"])
        sequence.append("order_queried")
        if _order_has_fill(order):
            if order.get("state") in {"live", "partially_filled"}:
                try:
                    _cancel_with_reconciliation(
                        active_transport,
                        instrument,
                        client_order_id,
                        expected_price=price,
                        expected_size=size,
                    )
                except (
                    OkxDemoCanaryBlocked,
                    OkxDemoRecoveryRequired,
                    OkxDemoWriteRejected,
                    OkxDemoTransportError,
                ):
                    sequence.append("cancel_reconciliation_uncertain")
            return finish_unexpected_exposure(
                "unexpected_fill_cleanup_attempted",
                expected_price=price,
                expected_size=size,
            )
        if order.get("state") != "live":
            raise OkxDemoCanaryBlocked("ORDER_NOT_LIVE_BEFORE_CANCEL")

        final_order = _cancel_with_reconciliation(
            active_transport,
            instrument,
            client_order_id,
            expected_price=price,
            expected_size=size,
        )
        sequence.extend(("cancel_requested", "cancel_state_queried"))
        if _order_has_fill(final_order):
            return finish_unexpected_exposure(
                "post_cancel_fill_cleanup_attempted",
                expected_price=price,
                expected_size=size,
            )
        if final_order.get("state") not in {"canceled", "mmp_canceled"}:
            raise OkxDemoRecoveryRequired("ORDER_NOT_CANCELED")
        if _pending_count(_query_pending(active_transport, instrument), instrument):
            raise OkxDemoCanaryBlocked("FINAL_PENDING_ORDERS_PRESENT")
        final_fill_count = _fill_count(
            _query_fills(active_transport, instrument, order_id),
            instrument,
            order_id,
        )
        final_position = _position_size(
            _query_positions(active_transport, instrument),
            instrument,
        )
        if final_fill_count or final_position != 0:
            return finish_unexpected_exposure(
                "final_scope_cleanup_attempted",
                expected_price=price,
                expected_size=size,
            )
        sequence.append("final_scope_empty")
        return _persist_result(
            _result(
                status="PASSED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code=None,
            ),
            artifact_dir,
        )
    except OkxDemoRecoveryRequired:
        return _persist_result(
            _result(
                status="RECOVERY_REQUIRED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code="NONTERMINAL_OUTCOME_REQUIRES_RECOVERY",
            ),
            artifact_dir,
        )
    except OkxDemoPreflightBlocked:
        return _persist_result(
            _result(
                status="BLOCKED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code="ACCOUNT_ATTESTATION_FAILED",
            ),
            artifact_dir,
        )
    except OkxDemoWriteRejected as exc:
        placement_was_explicitly_rejected = (
            str(exc) == "PLACE_WRITE_FAILED" and order_id is None
        )
        return _persist_result(
            _result(
                status=(
                    "BLOCKED"
                    if placement_was_explicitly_rejected
                    else "RECOVERY_REQUIRED"
                ),
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code=(
                    "PLACE_WRITE_FAILED"
                    if placement_was_explicitly_rejected
                    else "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
                ),
            ),
            artifact_dir,
        )
    except OkxDemoCanaryBlocked as exc:
        recovery_required = "place_intent_persisted" in sequence
        return _persist_result(
            _result(
                status="RECOVERY_REQUIRED" if recovery_required else "BLOCKED",
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code=(
                    "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
                    if recovery_required
                    else str(exc)
                ),
            ),
            artifact_dir,
        )
    except OkxDemoTransportError:
        return _persist_result(
            _result(
                status=(
                    "RECOVERY_REQUIRED"
                    if "place_intent_persisted" in sequence
                    else "BLOCKED"
                ),
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
                order_id=order_id,
                sequence=sequence,
                reason_code=(
                    "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
                    if "place_intent_persisted" in sequence
                    else "TRANSPORT_OR_RECONCILIATION_FAILED"
                ),
            ),
            artifact_dir,
        )


def run_canary(
    environment: Optional[Mapping[str, str]] = None,
    *,
    transport: Optional[CanaryTransport] = None,
    instrument: str = DEFAULT_INSTRUMENT,
    cl_ord_id: Optional[str] = None,
    artifact_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    active_environment = os.environ if environment is None else environment
    _validate_environment(active_environment)
    instrument = _validate_instrument(instrument)
    artifact_id = uuid.uuid4().hex
    client_order_id = _validate_client_order_id(
        cl_ord_id or "FTAICANARY{}".format(artifact_id[:20])
    )
    target_dir = artifact_dir or ARTIFACT_ROOT
    try:
        writer_lock = _acquire_writer_lock(target_dir)
    except OSError:
        raise OkxDemoCanaryBlocked("CANARY_ARTIFACT_STORAGE_UNAVAILABLE") from None
    try:
        existing = _nonterminal_history(target_dir)
        if existing is not None:
            return _recover_nonterminal_canary(
                active_environment,
                transport=transport,
                artifact_dir=target_dir,
                existing=existing,
            )
        try:
            _reserve_canary(
                target_dir=target_dir,
                artifact_id=artifact_id,
                instrument=instrument,
                cl_ord_id=client_order_id,
            )
        except OSError:
            raise OkxDemoCanaryBlocked(
                "CANARY_ARTIFACT_STORAGE_UNAVAILABLE"
            ) from None
        return _execute_canary(
            active_environment,
            transport=transport,
            instrument=instrument,
            client_order_id=client_order_id,
            artifact_dir=target_dir,
            artifact_id=artifact_id,
        )
    finally:
        fcntl.flock(writer_lock.fileno(), fcntl.LOCK_UN)
        writer_lock.close()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-demo-order", action="store_true")
    parser.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not args.allow_demo_order:
        payload = {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "reason_code": "EXPLICIT_DEMO_ORDER_AUTHORIZATION_REQUIRED",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        payload = run_canary(instrument=args.instrument)
    except OkxDemoCanaryBlocked as exc:
        payload = {
            "status": "BLOCKED",
            "execution_target": "OKX_DEMO",
            "reason_code": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload["status"] == "PASSED":
        return 0
    return 1 if payload["status"] == "FAILED" else 2


if __name__ == "__main__":
    sys.exit(main())
