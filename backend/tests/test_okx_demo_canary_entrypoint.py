import json

import pytest

from app.adapters.okx_demo import demo_canary as canary


def test_public_direct_canary_entrypoint_is_permanently_blocked() -> None:
    with pytest.raises(canary.OkxDemoCanaryBlocked, match="DIRECT_CANARY_DISABLED"):
        canary.run_canary(
            {"OKX_DEMO_API_KEY": "must-not-be-read"},
            transport=object(),
        )


def test_direct_canary_cli_is_blocked_before_credentials_or_network(capsys) -> None:
    assert canary.main(["--allow-demo-order"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "execution_target": "OKX_DEMO",
        "reason_code": "DIRECT_CANARY_DISABLED_USE_CANONICAL_RUNTIME_ONE_SHOT_GRANT",
        "status": "BLOCKED",
    }
