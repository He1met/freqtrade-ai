from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import download_okx_research_market_data as refresh  # noqa: E402


def _frame(start: str, periods: int) -> pd.DataFrame:
    dates = pd.date_range(start, periods=periods, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "date": dates,
            "open": range(periods),
            "high": range(1, periods + 1),
            "low": range(periods),
            "close": range(1, periods + 1),
            "volume": [1.0] * periods,
        }
    )


def test_incremental_refresh_merges_overlap_and_rejects_older_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_path = tmp_path / "BTC_USDT_USDT-5m-futures.feather"
    current = _frame("2026-08-11T00:00:00Z", 4)
    current.to_feather(current_path)
    newer = _frame("2026-08-11T00:10:00Z", 4)
    monkeypatch.setattr(
        refresh,
        "download_pair",
        lambda _instrument, _start: (newer, {"response_chain_sha256": "a" * 64}),
    )
    merged, evidence = refresh.refresh_pair(
        "BTC-USDT-SWAP",
        int(datetime(2023, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
        current_path,
    )
    assert len(merged) == 6
    assert evidence["refresh_mode"] == "incremental_overlap"

    older = _frame("2026-08-10T23:00:00Z", 2)
    monkeypatch.setattr(
        refresh,
        "download_pair",
        lambda _instrument, _start: (older, {"response_chain_sha256": "b" * 64}),
    )
    with pytest.raises(RuntimeError, match="older than installed data"):
        refresh.refresh_pair("BTC-USDT-SWAP", 0, current_path)


def test_complete_refresh_promotion_rolls_back_all_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.new"
    second = tmp_path / "second.new"
    target_one = tmp_path / "first"
    target_two = tmp_path / "second"
    first.write_text("new-one", encoding="utf-8")
    second.write_text("new-two", encoding="utf-8")
    target_one.write_text("old-one", encoding="utf-8")
    target_two.write_text("old-two", encoding="utf-8")
    original_replace = refresh.os.replace

    def fail_second(source, target):
        if Path(source) == second:
            raise OSError("synthetic promotion failure")
        original_replace(source, target)

    monkeypatch.setattr(refresh.os, "replace", fail_second)
    with pytest.raises(OSError, match="synthetic promotion failure"):
        refresh._promote_complete_refresh(
            [(first, target_one), (second, target_two)]
        )
    assert target_one.read_text(encoding="utf-8") == "old-one"
    assert target_two.read_text(encoding="utf-8") == "old-two"
