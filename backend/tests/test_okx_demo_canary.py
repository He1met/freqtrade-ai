import json
from pathlib import Path

import pytest

from app.adapters.okx_demo import demo_canary as canary
from app.adapters.okx_demo.credential_preflight import account_fingerprint


CLIENT_ORDER_ID = "FTAICANARYOFFLINE001"
ORDER_ID = "private-order-id"


@pytest.fixture(autouse=True)
def historical_canary_entrypoint(monkeypatch):
    """Keep the legacy transport suite explicitly offline-only.

    The production module entrypoint is permanently blocked; these tests cover
    the retired state-machine semantics through its private historical helper.
    The public boundary is asserted separately in
    ``test_okx_demo_canary_entrypoint.py``.
    """

    monkeypatch.setattr(canary, "run_canary", canary._historical_run_canary)


def environment() -> dict[str, str]:
    account = account_payload()["data"][0]
    return {
        canary.ALLOW_DEMO_ORDER_ENV: "true",
        canary.EXECUTION_TARGET_ENV: "OKX_DEMO",
        canary.ALLOW_REAL_FUNDS_ENV: "false",
        canary.REST_URL_ENV: canary.OKX_DEMO_REST_URL,
        "OKX_DEMO_API_KEY": "credential-key",
        "OKX_DEMO_API_SECRET": "credential-secret",
        "OKX_DEMO_API_PASSPHRASE": "credential-passphrase",
        canary.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV: account_fingerprint(account),
    }


def account_payload(position_mode: str = "long_short_mode") -> dict:
    return {
        "code": "0",
        "data": [
            {
                "uid": "private-uid",
                "mainUid": "private-main-uid",
                "acctLv": "2",
                "posMode": position_mode,
                "perm": "read_only,trade",
            }
        ],
    }


def instrument_payload(**overrides) -> dict:
    item = {
        "instId": canary.DEFAULT_INSTRUMENT,
        "state": "live",
        "tickSz": "0.1",
        "lotSz": "0.01",
        "minSz": "0.01",
        "ctVal": "0.01",
    }
    item.update(overrides)
    return {"code": "0", "data": [item]}


def ticker_payload() -> dict:
    return {
        "code": "0",
        "data": [{"instId": canary.DEFAULT_INSTRUMENT, "bidPx": "60000"}],
    }


def empty_payload() -> dict:
    return {"code": "0", "data": []}


def fill_payload(order_id: str = ORDER_ID) -> dict:
    return {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "ordId": order_id,
            }
        ],
    }


def zero_position_payload() -> dict:
    return {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "long",
                "mgnMode": "isolated",
                "pos": "0",
            }
        ],
    }


def write_ack(cl_ord_id: str = CLIENT_ORDER_ID, order_id: str = ORDER_ID) -> dict:
    return {
        "code": "0",
        "data": [
            {
                "sCode": "0",
                "sMsg": "",
                "ordId": order_id,
                "clOrdId": cl_ord_id,
            }
        ],
    }


def order_payload(
    state: str,
    *,
    accumulated_fill: str = "0",
    cl_ord_id: str = CLIENT_ORDER_ID,
    order_id: str = ORDER_ID,
    **overrides,
) -> dict:
    item = {
        "instId": canary.DEFAULT_INSTRUMENT,
        "ordId": order_id,
        "clOrdId": cl_ord_id,
        "state": state,
        "tdMode": "isolated",
        "ordType": "post_only",
        "side": "buy",
        "posSide": "long",
        "px": "57000",
        "sz": "0.01",
        "accFillSz": accumulated_fill,
    }
    item.update(overrides)
    return {"code": "0", "data": [item]}


def cleanup_order_payload(
    cleanup_cl_ord_id: str,
    *,
    side: str = "sell",
    size: str = "0.005",
    state: str = "filled",
    position_side: str = "long",
) -> dict:
    return {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "ordId": "cleanup-order",
                "clOrdId": cleanup_cl_ord_id,
                "state": state,
                "tdMode": "isolated",
                "ordType": "market",
                "side": side,
                "posSide": position_side,
                "reduceOnly": "true",
                "sz": size,
            }
        ],
    }


class ScriptedTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, path, *, params=None, body=None, write=False):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "body": body,
                "write": write,
            }
        )
        if not self.script:
            pytest.fail("unexpected transport request: {} {}".format(method, path))
        expected_method, expected_path, response = self.script.pop(0)
        assert (method, path) == (expected_method, expected_path)
        if isinstance(response, BaseException):
            raise response
        return response


def success_script():
    return [
        ("GET", "/api/v5/account/config", account_payload()),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
        ("GET", "/api/v5/public/instruments", instrument_payload()),
        ("GET", "/api/v5/market/ticker", ticker_payload()),
        ("POST", "/api/v5/trade/order", write_ack()),
        ("GET", "/api/v5/trade/order", order_payload("live")),
        ("POST", "/api/v5/trade/cancel-order", write_ack()),
        ("GET", "/api/v5/trade/order", order_payload("canceled")),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/trade/fills-history", empty_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
    ]


def test_successful_canary_is_sanitized_and_persisted(tmp_path: Path) -> None:
    transport = ScriptedTransport(success_script())

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "PASSED"
    place = next(
        call
        for call in transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    )
    assert place["body"] == {
        "instId": canary.DEFAULT_INSTRUMENT,
        "tdMode": "isolated",
        "side": "buy",
        "ordType": "post_only",
        "px": "57000",
        "sz": "0.01",
        "posSide": "long",
        "clOrdId": CLIENT_ORDER_ID,
    }
    artifact = json.loads(
        (tmp_path / "{}.json".format(result["artifact_id"])).read_text(
            encoding="utf-8"
        )
    )
    rendered = json.dumps({"result": result, "artifact": artifact})
    for forbidden in (
        CLIENT_ORDER_ID,
        ORDER_ID,
        "credential-key",
        "credential-secret",
        "credential-passphrase",
        "private-uid",
        "private-main-uid",
        environment()[canary.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV],
    ):
        assert forbidden not in rendered
    assert transport.script == []


def test_legacy_net_account_blocks_dual_side_canary_before_any_write(tmp_path: Path) -> None:
    account = account_payload("net_mode")
    environment_values = environment()
    environment_values[canary.OKX_DEMO_ACCOUNT_FINGERPRINT_ENV] = account_fingerprint(
        account["data"][0]
    )
    transport = ScriptedTransport(
        [("GET", "/api/v5/account/config", account)]
    )

    result = canary.run_canary(
        environment_values,
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "ACCOUNT_POSITION_MODE_INVALID"
    assert all(call["method"] != "POST" for call in transport.calls)


def test_opposite_side_position_is_drift_and_never_cleaned_as_long(tmp_path: Path) -> None:
    opposite_position = {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "short",
                "mgnMode": "isolated",
                "pos": "0.01",
            }
        ],
    }
    transport = ScriptedTransport(
        [
            ("GET", "/api/v5/account/config", account_payload()),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/account/positions", opposite_position),
        ]
    )

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "OPPOSITE_SIDE_POSITION_DRIFT"
    assert all(call["method"] != "POST" for call in transport.calls)


def test_short_canary_uses_a_distinct_side_identity_and_records_tp_sl_scope(
    tmp_path: Path,
) -> None:
    script = success_script()
    script[6] = (
        "GET",
        "/api/v5/trade/order",
        order_payload("live", side="sell", posSide="short"),
    )
    script[8] = (
        "GET",
        "/api/v5/trade/order",
        order_payload("canceled", side="sell", posSide="short"),
    )
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        position_side="short",
        artifact_dir=tmp_path,
    )

    assert result["status"] == "PASSED"
    assert result["position_side"] == "short"
    assert result["evidence"]["position_side"] == "short"
    assert (
        result["evidence"]["tp_sl_scope"]
        == "not_attached_post_only_no_fill_canary"
    )
    place = next(
        call
        for call in transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    )
    assert place["body"]["side"] == "sell"
    assert place["body"]["posSide"] == "short"


def test_short_fill_cleanup_is_reduce_only_buy_on_short_identity(tmp_path: Path) -> None:
    cleanup_id = "FTAICLEAN{}".format(
        canary.hashlib.sha256(CLIENT_ORDER_ID.encode()).hexdigest()[:20]
    )
    short_position = {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "short",
                "mgnMode": "isolated",
                "pos": "0.005",
            }
        ],
    }
    script = success_script()[:6] + [
        (
            "GET",
            "/api/v5/trade/order",
            order_payload(
                "filled",
                accumulated_fill="0.005",
                side="sell",
                posSide="short",
            ),
        ),
        ("GET", "/api/v5/account/positions", short_position),
        ("POST", "/api/v5/trade/order", write_ack(cleanup_id, "cleanup-order")),
        (
            "GET",
            "/api/v5/trade/order",
            cleanup_order_payload(cleanup_id, side="buy", position_side="short"),
        ),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
        (
            "GET",
            "/api/v5/trade/order",
            order_payload(
                "filled",
                accumulated_fill="0.005",
                side="sell",
                posSide="short",
            ),
        ),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/trade/fills-history", fill_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        position_side="short",
        artifact_dir=tmp_path,
    )

    assert result["status"] == "FAILED"
    cleanup = [
        call
        for call in transport.calls
        if call["method"] == "POST"
        and call["path"] == "/api/v5/trade/order"
        and call["body"]["clOrdId"] == cleanup_id
    ]
    assert cleanup[0]["body"] == {
        "instId": canary.DEFAULT_INSTRUMENT,
        "tdMode": "isolated",
        "side": "buy",
        "posSide": "short",
        "ordType": "market",
        "sz": "0.005",
        "reduceOnly": True,
        "clOrdId": cleanup_id,
    }


def test_missing_explicit_authorization_is_zero_network_and_zero_artifact(
    tmp_path: Path,
) -> None:
    active_environment = environment()
    active_environment.pop(canary.ALLOW_DEMO_ORDER_ENV)
    transport = ScriptedTransport([])

    with pytest.raises(
        canary.OkxDemoCanaryBlocked,
        match="EXPLICIT_DEMO_ORDER_AUTHORIZATION_REQUIRED",
    ):
        canary.run_canary(
            active_environment,
            transport=transport,
            artifact_dir=tmp_path,
        )

    assert transport.calls == []
    assert list(tmp_path.iterdir()) == []


def test_place_timeout_reconciles_by_cl_ord_id_without_second_place(
    tmp_path: Path,
) -> None:
    script = success_script()
    script[5] = (
        "POST",
        "/api/v5/trade/order",
        canary.OkxDemoTransportError(unknown_write_outcome=True),
    )
    script.insert(6, ("GET", "/api/v5/trade/order", order_payload("live")))
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "PASSED"
    place_calls = [
        call
        for call in transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    ]
    assert len(place_calls) == 1
    assert "place_reconciled_by_cl_ord_id" in result["evidence"]["sequence"]


@pytest.mark.parametrize(
    ("bad_item", "expected_status", "expected_reason"),
    [
        (
            {"sCode": "51008", "ordId": "", "clOrdId": CLIENT_ORDER_ID},
            "BLOCKED",
            "PLACE_WRITE_FAILED",
        ),
        (
            {"sCode": "0", "ordId": ORDER_ID, "clOrdId": "other"},
            "RECOVERY_REQUIRED",
            "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY",
        ),
    ],
)
def test_http_200_write_item_failure_blocks(
    bad_item,
    expected_status,
    expected_reason,
    tmp_path: Path,
) -> None:
    script = success_script()[:6]
    script[-1] = ("POST", "/api/v5/trade/order", {"code": "0", "data": [bad_item]})
    if expected_status == "RECOVERY_REQUIRED":
        script.append(
            (
                "GET",
                "/api/v5/trade/order",
                canary.OkxDemoTransportError(unknown_write_outcome=False),
            )
        )
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == expected_status
    assert result["reason_code"] == expected_reason


def test_explicit_top_level_place_rejection_is_terminal_blocked(
    tmp_path: Path,
) -> None:
    script = success_script()[:6]
    script[-1] = (
        "POST",
        "/api/v5/trade/order",
        {"code": "51008", "data": [], "msg": "not forwarded"},
    )
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "PLACE_WRITE_FAILED"
    assert transport.script == []


def test_unexpected_partial_fill_is_canceled_and_reduce_only_cleaned(
    tmp_path: Path,
) -> None:
    cleanup_id = "FTAICLEAN{}".format(
        canary.hashlib.sha256(CLIENT_ORDER_ID.encode()).hexdigest()[:20]
    )
    long_position = {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "long",
                "mgnMode": "isolated",
                "pos": "0.005",
            }
        ],
    }
    script = success_script()[:6] + [
        (
            "GET",
            "/api/v5/trade/order",
            order_payload("partially_filled", accumulated_fill="0.005"),
        ),
        ("POST", "/api/v5/trade/cancel-order", write_ack()),
        (
            "GET",
            "/api/v5/trade/order",
            order_payload("canceled", accumulated_fill="0.005"),
        ),
        ("GET", "/api/v5/account/positions", long_position),
        ("POST", "/api/v5/trade/order", write_ack(cleanup_id, "cleanup-order")),
        (
            "GET",
            "/api/v5/trade/order",
            cleanup_order_payload(cleanup_id),
        ),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
        (
            "GET",
            "/api/v5/trade/order",
            order_payload("canceled", accumulated_fill="0.005"),
        ),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/trade/fills-history", fill_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "UNEXPECTED_FILL_CLEANED"
    cleanup = next(
        call
        for call in transport.calls
        if call["method"] == "POST"
        and call["path"] == "/api/v5/trade/order"
        and call["body"]["clOrdId"] == cleanup_id
    )
    assert cleanup["body"] == {
        "instId": canary.DEFAULT_INSTRUMENT,
        "tdMode": "isolated",
        "side": "sell",
        "posSide": "long",
        "ordType": "market",
        "sz": "0.005",
        "reduceOnly": True,
        "clOrdId": cleanup_id,
    }


@pytest.mark.parametrize(
    "metadata",
    [
        instrument_payload(state="suspend"),
        instrument_payload(tickSz="0"),
        instrument_payload(ctVal="100"),
    ],
)
def test_unsafe_instrument_metadata_blocks_before_order_write(
    metadata,
    tmp_path: Path,
) -> None:
    script = success_script()[:5]
    script[3] = ("GET", "/api/v5/public/instruments", metadata)
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "BLOCKED"
    assert not any(call["method"] == "POST" for call in transport.calls)


def test_query_contract_mismatch_blocks_before_cancel(tmp_path: Path) -> None:
    script = success_script()[:7]
    script[-1] = (
        "GET",
        "/api/v5/trade/order",
        order_payload("live", tdMode="cross"),
    )
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason_code"] == "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
    assert not any(
        call["path"] == "/api/v5/trade/cancel-order" for call in transport.calls
    )


def test_attached_tp_sl_on_post_only_canary_is_recovery_required_before_cancel(
    tmp_path: Path,
) -> None:
    script = success_script()[:7]
    script[-1] = (
        "GET",
        "/api/v5/trade/order",
        order_payload("live", attachAlgoOrds=[{"attachAlgoId": "unexpected"}]),
    )
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason_code"] == "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"
    assert not any(
        call["path"] == "/api/v5/trade/cancel-order" for call in transport.calls
    )


def test_nonterminal_history_blocks_new_canary_before_network(tmp_path: Path) -> None:
    (tmp_path / "previous.json").write_text(
        json.dumps(
            {
                "status": "RESERVED",
                "evidence": {"cl_ord_id_sha256": "a" * 64},
            }
        ),
        encoding="utf-8",
    )
    transport = ScriptedTransport([])

    with pytest.raises(
        canary.OkxDemoCanaryBlocked,
        match="NONTERMINAL_CANARY_EVIDENCE_INVALID",
    ):
        canary.run_canary(
            environment(),
            transport=transport,
            cl_ord_id=CLIENT_ORDER_ID,
            artifact_dir=tmp_path,
        )

    assert transport.calls == []


def test_cancel_ack_is_polled_until_terminal_state(tmp_path: Path) -> None:
    script = success_script()
    script[8:9] = [
        ("GET", "/api/v5/trade/order", order_payload("live")),
        ("GET", "/api/v5/trade/order", order_payload("live")),
        ("GET", "/api/v5/trade/order", order_payload("canceled")),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "PASSED"
    assert transport.script == []


def test_cancel_never_reaches_terminal_state_blocks(tmp_path: Path) -> None:
    script = success_script()[:8] + [
        ("GET", "/api/v5/trade/order", order_payload("live")),
        ("GET", "/api/v5/trade/order", order_payload("live")),
        ("GET", "/api/v5/trade/order", order_payload("live")),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason_code"] == "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"


def test_each_cancel_timeout_is_reconciled_before_returning(
    tmp_path: Path,
) -> None:
    script = success_script()[:7] + [
        (
            "POST",
            "/api/v5/trade/cancel-order",
            canary.OkxDemoTransportError(unknown_write_outcome=True),
        ),
        ("GET", "/api/v5/trade/order", order_payload("live")),
        (
            "POST",
            "/api/v5/trade/cancel-order",
            canary.OkxDemoTransportError(unknown_write_outcome=True),
        ),
        ("GET", "/api/v5/trade/order", order_payload("canceled")),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/trade/fills-history", empty_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "PASSED"
    cancel_calls = [
        call
        for call in transport.calls
        if call["method"] == "POST"
        and call["path"] == "/api/v5/trade/cancel-order"
    ]
    assert len(cancel_calls) == 2
    assert transport.script == []


def test_cleanup_timeout_reconciles_without_second_cleanup_write(
    tmp_path: Path,
) -> None:
    cleanup_id = "FTAICLEAN{}".format(
        canary.hashlib.sha256(CLIENT_ORDER_ID.encode()).hexdigest()[:20]
    )
    long_position = {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "long",
                "mgnMode": "isolated",
                "pos": "0.005",
            }
        ],
    }
    script = success_script()[:6] + [
        (
            "GET",
            "/api/v5/trade/order",
            order_payload("filled", accumulated_fill="0.005"),
        ),
        ("GET", "/api/v5/account/positions", long_position),
        (
            "POST",
            "/api/v5/trade/order",
            canary.OkxDemoTransportError(unknown_write_outcome=True),
        ),
        (
            "GET",
            "/api/v5/trade/order",
            cleanup_order_payload(cleanup_id, side="sell"),
        ),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
        (
            "GET",
            "/api/v5/trade/order",
            order_payload("filled", accumulated_fill="0.005"),
        ),
        ("GET", "/api/v5/trade/orders-pending", empty_payload()),
        ("GET", "/api/v5/trade/fills-history", fill_payload()),
        ("GET", "/api/v5/account/positions", zero_position_payload()),
    ]
    transport = ScriptedTransport(script)

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "UNEXPECTED_FILL_CLEANED"
    cleanup_writes = [
        call
        for call in transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    ]
    assert len(cleanup_writes) == 2
    assert cleanup_writes[-1]["body"]["side"] == "sell"
    assert cleanup_writes[-1]["body"]["reduceOnly"] is True


def test_artifact_reservation_failure_is_zero_network(tmp_path: Path) -> None:
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied", encoding="utf-8")
    transport = ScriptedTransport([])

    with pytest.raises(
        canary.OkxDemoCanaryBlocked,
        match="CANARY_ARTIFACT_STORAGE_UNAVAILABLE",
    ):
        canary.run_canary(
            environment(),
            transport=transport,
            cl_ord_id=CLIENT_ORDER_ID,
            artifact_dir=unavailable,
        )

    assert transport.calls == []


def test_http_transport_uses_fixed_demo_url_and_header() -> None:
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"code":"0","data":[]}'

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    transport = canary.OkxDemoHttpTransport(environment(), opener=Opener())
    payload = transport.request(
        "GET",
        "/api/v5/account/config",
        params={"a": "b"},
    )

    request = captured["request"]
    assert request.full_url == (
        canary.OKX_DEMO_REST_URL + "/api/v5/account/config?a=b"
    )
    assert request.get_header("X-simulated-trading") == "1"
    assert captured["timeout"] == canary.REQUEST_TIMEOUT_SECONDS
    assert payload == empty_payload()
    assert canary._NoRedirectHandler().redirect_request(
        request, None, 302, "redirect", {}, "https://example.invalid"
    ) is None


@pytest.mark.parametrize("raw_payload", [b'{"code":"0"', b"\xff"])
def test_write_response_parse_failure_is_unknown_outcome(raw_payload) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return raw_payload

    class Opener:
        def open(self, _request, _timeout=None, **_kwargs):
            return Response()

    transport = canary.OkxDemoHttpTransport(environment(), opener=Opener())

    with pytest.raises(canary.OkxDemoTransportError) as captured:
        transport.request(
            "POST",
            "/api/v5/trade/order",
            body={"clOrdId": CLIENT_ORDER_ID},
            write=True,
        )

    assert captured.value.unknown_write_outcome is True


def _running_recovery_artifact(artifact_id: str) -> tuple[dict, str]:
    cl_ord_id = "FTAICANARY{}".format(artifact_id[:20])
    return (
        {
            "status": "RUNNING",
            "execution_target": "OKX_DEMO",
            "artifact_id": artifact_id,
            "instrument": canary.DEFAULT_INSTRUMENT,
            "reason_code": "PLACE_INTENT_PERSISTED",
            "evidence": {
                "cl_ord_id_sha256": canary._hash_identifier(cl_ord_id),
                "order_id_sha256": None,
                "cleanup_cl_ord_id_sha256": None,
                "simulated_trading_header": True,
                "sequence": [
                    "account_attested",
                    "initial_scope_empty",
                    "place_intent_persisted",
                ],
            },
        },
        cl_ord_id,
    )


def test_restart_recovers_original_cl_ord_id_before_new_place(
    tmp_path: Path,
) -> None:
    artifact_id = "1" * 32
    artifact, recovery_cl_ord_id = _running_recovery_artifact(artifact_id)
    (tmp_path / "{}.json".format(artifact_id)).write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    recovery_order = order_payload("live", cl_ord_id=recovery_cl_ord_id)
    recovered_order = order_payload("canceled", cl_ord_id=recovery_cl_ord_id)
    recovery_transport = ScriptedTransport(
        [
            ("GET", "/api/v5/account/config", account_payload()),
            ("GET", "/api/v5/trade/order", recovery_order),
            ("GET", "/api/v5/trade/order", recovery_order),
            ("POST", "/api/v5/trade/cancel-order", write_ack(recovery_cl_ord_id)),
            ("GET", "/api/v5/trade/order", recovered_order),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", recovered_order),
            ("GET", "/api/v5/trade/order", recovered_order),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
        ]
    )

    recovered = canary.run_canary(
        environment(),
        transport=recovery_transport,
        artifact_dir=tmp_path,
    )

    assert recovered["status"] == "FAILED"
    assert recovered["reason_code"] == "PRIOR_CANARY_RECOVERED"
    assert not any(
        call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
        for call in recovery_transport.calls
    )

    fresh_transport = ScriptedTransport(success_script())
    fresh = canary.run_canary(
        environment(),
        transport=fresh_transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )
    all_calls = recovery_transport.calls + fresh_transport.calls
    place_calls = [
        call
        for call in all_calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    ]
    assert fresh["status"] == "PASSED"
    assert len(place_calls) == 1


def test_unresolved_recovery_permanently_blocks_new_place(
    tmp_path: Path,
) -> None:
    artifact_id = "2" * 32
    artifact, _recovery_cl_ord_id = _running_recovery_artifact(artifact_id)
    (tmp_path / "{}.json".format(artifact_id)).write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    transports = []
    result = None
    for _attempt in range(25):
        active = ScriptedTransport(
            [
                ("GET", "/api/v5/account/config", account_payload()),
                (
                    "GET",
                    "/api/v5/trade/order",
                    canary.OkxDemoTransportError(unknown_write_outcome=False),
                ),
            ]
        )
        transports.append(active)
        result = canary.run_canary(
            environment(),
            transport=active,
            artifact_dir=tmp_path,
        )
        assert result["status"] == "RECOVERY_REQUIRED"
    assert result is not None
    artifact = json.loads(
        (tmp_path / "{}.json".format(artifact_id)).read_text(encoding="utf-8")
    )
    assert artifact["recovery_attempt_count"] == 25
    assert artifact["recovery_last_attempt_at"].endswith("Z")
    assert artifact["evidence"]["sequence"].count("recovery_started") == 1
    assert len(artifact["evidence"]["sequence"]) <= 20
    assert not any(
        call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
        for active in transports
        for call in active.calls
    )


def test_place_unknown_then_restart_has_exactly_one_place_post(
    tmp_path: Path,
) -> None:
    first_transport = ScriptedTransport(
        success_script()[:5]
        + [
            (
                "POST",
                "/api/v5/trade/order",
                canary.OkxDemoTransportError(unknown_write_outcome=True),
            ),
            (
                "GET",
                "/api/v5/trade/order",
                canary.OkxDemoTransportError(unknown_write_outcome=False),
            ),
        ]
    )
    first = canary.run_canary(
        environment(),
        transport=first_transport,
        artifact_dir=tmp_path,
    )
    assert first["status"] == "RECOVERY_REQUIRED"

    cl_ord_id = "FTAICANARY{}".format(first["artifact_id"][:20])
    canceled = order_payload("canceled", cl_ord_id=cl_ord_id)
    recovery_transport = ScriptedTransport(
        [
            ("GET", "/api/v5/account/config", account_payload()),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
        ]
    )
    second = canary.run_canary(
        environment(),
        transport=recovery_transport,
        artifact_dir=tmp_path,
    )

    assert second["status"] == "FAILED"
    assert second["reason_code"] == "PRIOR_CANARY_RECOVERED"
    place_posts = [
        call
        for call in first_transport.calls + recovery_transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    ]
    assert len(place_posts) == 1


@pytest.mark.parametrize(
    "malformed_ack",
    [
        [],
        {"data": []},
        {"code": "0", "data": []},
        {
            "code": "0",
            "data": [
                {
                    "sCode": "0",
                    "ordId": ORDER_ID,
                    "clOrdId": CLIENT_ORDER_ID,
                },
                {
                    "sCode": "0",
                    "ordId": "other-order",
                    "clOrdId": CLIENT_ORDER_ID,
                },
            ],
        },
        {"code": "0", "data": {"sCode": "0"}},
        {
            "code": "0",
            "data": [{"sCode": "0", "clOrdId": CLIENT_ORDER_ID}],
        },
        {
            "code": "0",
            "data": [{"ordId": ORDER_ID, "clOrdId": CLIENT_ORDER_ID}],
        },
    ],
)
def test_structurally_incomplete_place_ack_recovers_without_second_place(
    malformed_ack,
    tmp_path: Path,
) -> None:
    first_transport = ScriptedTransport(
        success_script()[:5]
        + [
            ("POST", "/api/v5/trade/order", malformed_ack),
            (
                "GET",
                "/api/v5/trade/order",
                canary.OkxDemoTransportError(unknown_write_outcome=False),
            ),
        ]
    )
    first = canary.run_canary(
        environment(),
        transport=first_transport,
        artifact_dir=tmp_path,
    )
    assert first["status"] == "RECOVERY_REQUIRED"

    cl_ord_id = "FTAICANARY{}".format(first["artifact_id"][:20])
    canceled = order_payload("canceled", cl_ord_id=cl_ord_id)
    recovery_transport = ScriptedTransport(
        [
            ("GET", "/api/v5/account/config", account_payload()),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/order", canceled),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/trade/fills-history", empty_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
        ]
    )
    second = canary.run_canary(
        environment(),
        transport=recovery_transport,
        artifact_dir=tmp_path,
    )

    assert second["status"] == "FAILED"
    place_posts = [
        call
        for call in first_transport.calls + recovery_transport.calls
        if call["method"] == "POST" and call["path"] == "/api/v5/trade/order"
    ]
    assert len(place_posts) == 1


def test_partial_fill_cancel_uncertainty_cannot_report_cleaned(
    tmp_path: Path,
) -> None:
    cleanup_id = "FTAICLEAN{}".format(
        canary.hashlib.sha256(CLIENT_ORDER_ID.encode()).hexdigest()[:20]
    )
    long_position = {
        "code": "0",
        "data": [
            {
                "instId": canary.DEFAULT_INSTRUMENT,
                "posSide": "long",
                "mgnMode": "isolated",
                "pos": "0.005",
            }
        ],
    }
    partial = order_payload("partially_filled", accumulated_fill="0.005")
    transport = ScriptedTransport(
        success_script()[:6]
        + [
            ("GET", "/api/v5/trade/order", partial),
            (
                "POST",
                "/api/v5/trade/cancel-order",
                canary.OkxDemoTransportError(unknown_write_outcome=True),
            ),
            ("GET", "/api/v5/trade/order", partial),
            (
                "POST",
                "/api/v5/trade/cancel-order",
                canary.OkxDemoTransportError(unknown_write_outcome=True),
            ),
            ("GET", "/api/v5/trade/order", partial),
            ("GET", "/api/v5/account/positions", long_position),
            ("POST", "/api/v5/trade/order", write_ack(cleanup_id, "cleanup-order")),
            ("GET", "/api/v5/trade/order", cleanup_order_payload(cleanup_id)),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", partial),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/trade/fills-history", fill_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
        ]
    )

    result = canary.run_canary(
        environment(),
        transport=transport,
        cl_ord_id=CLIENT_ORDER_ID,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason_code"] == "NONTERMINAL_OUTCOME_REQUIRES_RECOVERY"


def test_restart_reconciles_existing_cleanup_without_reposting(
    tmp_path: Path,
) -> None:
    artifact_id = "3" * 32
    artifact, cl_ord_id = _running_recovery_artifact(artifact_id)
    cleanup_id = "FTAICLEAN{}".format(
        canary.hashlib.sha256(cl_ord_id.encode()).hexdigest()[:20]
    )
    artifact["status"] = "RECOVERY_REQUIRED"
    artifact["evidence"]["cleanup_cl_ord_id_sha256"] = canary._hash_identifier(
        cleanup_id
    )
    (tmp_path / "{}.json".format(artifact_id)).write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    original = order_payload(
        "canceled",
        accumulated_fill="0.005",
        cl_ord_id=cl_ord_id,
    )
    cleanup = cleanup_order_payload(cleanup_id)
    transport = ScriptedTransport(
        [
            ("GET", "/api/v5/account/config", account_payload()),
            ("GET", "/api/v5/trade/order", original),
            ("GET", "/api/v5/trade/order", original),
            ("GET", "/api/v5/trade/fills-history", fill_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", cleanup),
            ("GET", "/api/v5/trade/order", cleanup),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
            ("GET", "/api/v5/trade/order", original),
            ("GET", "/api/v5/trade/order", original),
            ("GET", "/api/v5/trade/orders-pending", empty_payload()),
            ("GET", "/api/v5/trade/fills-history", fill_payload()),
            ("GET", "/api/v5/account/positions", zero_position_payload()),
        ]
    )

    result = canary.run_canary(
        environment(),
        transport=transport,
        artifact_dir=tmp_path,
    )

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "PRIOR_CANARY_RECOVERED"
    assert not any(call["method"] == "POST" for call in transport.calls)
