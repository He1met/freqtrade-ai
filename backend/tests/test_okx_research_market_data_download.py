from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "download_okx_research_market_data.py"
SPEC = spec_from_file_location("download_okx_research_market_data", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    ("instrument", "stem"),
    [
        ("BTC-USDT-SWAP", "BTC_USDT_USDT"),
        ("ETH-USDT-SWAP", "ETH_USDT_USDT"),
        ("SOL-USDT-SWAP", "SOL_USDT_USDT"),
    ],
)
def test_locked_okx_instruments_use_freqtrade_futures_stem(
    instrument: str, stem: str
) -> None:
    assert MODULE._freqtrade_stem(instrument) == stem


def test_freqtrade_stem_rejects_non_usdt_perpetual() -> None:
    with pytest.raises(RuntimeError, match="locked USDT perpetual"):
        MODULE._freqtrade_stem("XRP-USDC-SWAP")


def test_15m_derivation_uses_only_complete_utc_aligned_buckets() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-08-10T00:00:00Z", periods=7, freq="5min"),
            "open": [1, 2, 3, 4, 5, 6, 7],
            "high": [2, 3, 4, 5, 6, 7, 8],
            "low": [0.5, 1, 2, 3, 4, 5, 6],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5],
            "volume": [10, 20, 30, 40, 50, 60, 70],
        }
    )

    derived = MODULE.derive_15m(frame)

    assert len(derived) == 2
    assert list(derived["date"]) == list(
        pd.to_datetime(["2026-08-10T00:00:00Z", "2026-08-10T00:15:00Z"])
    )
    assert derived.iloc[0].to_dict() == {
        "date": pd.Timestamp("2026-08-10T00:00:00Z"),
        "open": 1,
        "high": 4,
        "low": 0.5,
        "close": 3.5,
        "volume": 60,
    }
