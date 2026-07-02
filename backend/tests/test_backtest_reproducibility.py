from app.adapters.freqtrade.market_data_catalog import MarketDataCatalog
from app.schemas.backtest import BacktestResultCreate
from app.schemas.backtest_profile import BacktestProfileV2
from app.services.backtest_reproducibility import BacktestReproducibilityService


def profile_payload(pair: str = "BTC/USDT:USDT") -> dict:
    return {
        "profile_name": "phase3_reproducibility",
        "pair": pair,
        "timeframe": "15m",
        "timerange": "20240101-20240201",
        "strategy": {"name": "MvpRsiStrategy", "path": "user_data/strategies/generated"},
        "data_source": {
            "kind": "local",
            "exchange": "okx",
            "datadir": "user_data/data",
            "data_format": "feather",
        },
    }


def result_payload(
    *,
    profit_total: float = 12.5,
    profit_pct: float = 0.125,
    max_drawdown_pct: float = 0.03,
    win_rate: float = 0.6,
    total_trades: int = 10,
) -> BacktestResultCreate:
    return BacktestResultCreate(
        result_path="reports/backtests/fixture-result.json",
        metrics_snapshot={"fixture": True},
        profit_total=profit_total,
        profit_pct=profit_pct,
        max_drawdown_pct=max_drawdown_pct,
        win_rate=win_rate,
        total_trades=total_trades,
        timerange="20240101-20240201",
    )


def write_market_data(tmp_path):
    market_data_dir = tmp_path / "user_data" / "data"
    okx_dir = market_data_dir / "okx"
    okx_dir.mkdir(parents=True)
    (okx_dir / "BTC_USDT_USDT-15m-futures.feather").write_bytes(b"candles")
    return MarketDataCatalog(market_data_dir=market_data_dir).inspect()


def test_reproducibility_fingerprint_is_stable_for_same_inputs(tmp_path) -> None:
    report = write_market_data(tmp_path)
    profile = BacktestProfileV2.model_validate(profile_payload())
    service = BacktestReproducibilityService()

    first = service.build_fingerprint(profile, strategy_version=7, catalog_report=report)
    second = service.build_fingerprint(profile, strategy_version=7, catalog_report=report)

    assert first == second
    assert first.profile_name == "phase3_reproducibility"
    assert first.pair == "BTC/USDT:USDT"
    assert first.timeframe == "15m"
    assert first.strategy_version == "7"
    assert first.data_relative_path.as_posix() == "okx/BTC_USDT_USDT-15m-futures.feather"
    assert len(first.profile_hash) == 64
    assert len(first.data_fingerprint) == 64
    assert len(first.fingerprint_hash) == 64


def test_comparison_marks_same_fixture_results_stable(tmp_path) -> None:
    report = write_market_data(tmp_path)
    service = BacktestReproducibilityService()

    comparison = service.compare_results(
        profile=profile_payload(),
        strategy_version="strategy-v7",
        catalog_report=report,
        baseline_result=result_payload(),
        candidate_result=result_payload(),
    )

    assert comparison.status == "STABLE"
    assert comparison.fingerprint is not None
    assert comparison.metric_diffs == []
    assert comparison.warnings == []


def test_comparison_reports_metric_diffs_for_changed_fixture_result(tmp_path) -> None:
    report = write_market_data(tmp_path)
    service = BacktestReproducibilityService()

    comparison = service.compare_results(
        profile=profile_payload(),
        strategy_version="strategy-v7",
        catalog_report=report,
        baseline_result=result_payload(profit_pct=0.1, total_trades=10),
        candidate_result=result_payload(profit_pct=0.15, total_trades=12),
    )

    assert comparison.status == "CHANGED"
    assert [diff.metric for diff in comparison.metric_diffs] == ["profit_pct", "total_trades"]
    assert comparison.metric_diffs[0].delta == 0.04999999999999999
    assert comparison.metric_diffs[0].delta_pct == 0.4999999999999999
    assert "profit_pct changed" in comparison.warnings[0]
    assert "total_trades changed" in comparison.warnings[1]


def test_comparison_reports_missing_baseline_clearly(tmp_path) -> None:
    report = write_market_data(tmp_path)
    service = BacktestReproducibilityService()

    comparison = service.compare_results(
        profile=profile_payload(),
        strategy_version="strategy-v7",
        catalog_report=report,
        baseline_result=None,
        candidate_result=result_payload(),
    )

    assert comparison.status == "MISSING_BASELINE"
    assert comparison.fingerprint is not None
    assert comparison.metric_diffs == []
    assert comparison.warnings == [
        "No baseline result is available for this reproducibility fingerprint."
    ]


def test_comparison_blocks_when_local_market_data_is_missing(tmp_path) -> None:
    report = MarketDataCatalog(market_data_dir=tmp_path / "missing-data").inspect()
    service = BacktestReproducibilityService()

    comparison = service.compare_results(
        profile=profile_payload(),
        strategy_version="strategy-v7",
        catalog_report=report,
        baseline_result=result_payload(),
        candidate_result=result_payload(),
    )

    assert comparison.status == "BLOCKED"
    assert comparison.fingerprint is None
    assert comparison.metric_diffs == []
    assert "no available local market data for okx BTC/USDT:USDT 15m" in (
        comparison.blocked_reason or ""
    )
