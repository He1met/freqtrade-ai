from __future__ import annotations

import importlib.util
import inspect
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "containers/canonical-v13-research/canonical_v13_research_worker.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("canonical_v13_research_worker", WORKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_source_is_self_contained_and_containerfile_pins_reviewed_base() -> None:
    containerfile = (WORKER.parent / "Containerfile").read_text()
    assert "freqtradeorg/freqtrade@sha256:c730f60992863baafc8f13469291731e4b5e0ada82d8a9b449dcf5db24d00f76" in containerfile
    assert "ENTRYPOINT []" in containerfile
    assert "/opt/freqtrade-ai/bin/canonical-v13-research-worker" in containerfile
    assert (
        "ENV PYTHONPATH=/freqtrade:/home/ftuser/.local/lib/python3.14/site-packages"
        in containerfile
    )
    worker = _load_worker()
    assert worker.FREQTRADE_SUBPROCESS_ENV["HOME"] == "/work/home"
    assert worker.FREQTRADE_SUBPROCESS_ENV["PYTHONPATH"] == (
        "/freqtrade:/home/ftuser/.local/lib/python3.14/site-packages"
    )


def test_worker_rejects_noncanonical_json(tmp_path: Path) -> None:
    worker = _load_worker()
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"a": 1}, indent=2))
    with pytest.raises(worker.Blocked, match="canonical JSON"):
        worker._load_object(path)


def test_worker_preserves_intraday_window_with_freqtrade_unix_timerange() -> None:
    worker = _load_worker()
    start = datetime(2026, 7, 16, 2, 45, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 2, 45, tzinfo=timezone.utc)
    assert worker._freqtrade_timerange(start, end) == "1784169900-1786761900"


def test_worker_preserves_freqtrade_main_return_code(monkeypatch) -> None:
    worker = _load_worker()
    freqtrade = ModuleType("freqtrade")
    main_module = ModuleType("freqtrade.main")
    main_module.main = lambda _argv: 17
    monkeypatch.setitem(sys.modules, "freqtrade", freqtrade)
    monkeypatch.setitem(sys.modules, "freqtrade.main", main_module)
    monkeypatch.setattr(worker, "_install_offline_exchange_patch", lambda _path: None)
    monkeypatch.setattr(worker.Path, "mkdir", lambda *_args, **_kwargs: None)
    assert (
        worker._run_freqtrade_offline(
            ["--metadata", "/input/metadata", "lookahead-analysis"]
        )
        == 17
    )


@pytest.mark.parametrize(
    "pair",
    ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"),
)
def test_worker_hydrates_each_supported_single_market_offline_metadata(
    monkeypatch, tmp_path: Path, pair: str
) -> None:
    worker = _load_worker()
    metadata = tmp_path / "exchange-metadata.json"
    metadata.write_bytes(
        worker._canonical_bytes(
            {
                "contract": "canonical-v13-okx-offline-exchange-metadata-v1",
                "freqtrade_version": "2026.6",
                "ccxt_version": "4.5.61",
                "credential_access": "NONE",
                "network_access": "PUBLIC_MARKET_DATA_ONLY",
                "markets": {pair: {"symbol": pair}},
                "leverage_tiers": {pair: [{"tier": 1}]},
            }
        )
    )

    class Api:
        def __init__(self) -> None:
            self.markets: dict[str, object] = {}

        def set_markets(self, rows) -> None:
            self.markets = {str(row["symbol"]): row for row in rows}

    exchange = SimpleNamespace(
        _api=Api(),
        _api_async=Api(),
        _markets={},
        _leverage_tiers={},
        parse_leverage_tier=lambda row: row,
    )
    resolver_module = ModuleType("freqtrade.resolvers.exchange_resolver")

    class ExchangeResolver:
        @staticmethod
        def load_exchange(*_args, **_kwargs):
            return exchange

    resolver_module.ExchangeResolver = ExchangeResolver
    monkeypatch.setitem(sys.modules, "freqtrade", ModuleType("freqtrade"))
    monkeypatch.setitem(sys.modules, "freqtrade.resolvers", ModuleType("freqtrade.resolvers"))
    monkeypatch.setitem(
        sys.modules, "freqtrade.resolvers.exchange_resolver", resolver_module
    )

    worker._install_offline_exchange_patch(metadata)
    hydrated = ExchangeResolver.load_exchange({})

    assert set(hydrated._markets) == {pair}
    assert set(hydrated._leverage_tiers) == {pair}


def test_worker_rejects_unknown_offline_market(tmp_path: Path) -> None:
    worker = _load_worker()
    metadata = tmp_path / "exchange-metadata.json"
    metadata.write_bytes(
        worker._canonical_bytes(
            {
                "contract": "canonical-v13-okx-offline-exchange-metadata-v1",
                "freqtrade_version": "2026.6",
                "ccxt_version": "4.5.61",
                "credential_access": "NONE",
                "network_access": "PUBLIC_MARKET_DATA_ONLY",
                "markets": {"DOGE/USDT:USDT": {"symbol": "DOGE/USDT:USDT"}},
                "leverage_tiers": {"DOGE/USDT:USDT": [{"tier": 1}]},
            }
        )
    )

    with pytest.raises(worker.Blocked, match="metadata set is invalid"):
        worker._install_offline_exchange_patch(metadata)


def test_worker_creates_freqtrade_futures_data_directory() -> None:
    source = WORKER.read_text()
    assert '(data_root / "futures").mkdir(parents=True, exist_ok=True)' in source
    assert "FeatherDataHandler(data_root).ohlcv_store(" in source


def test_worker_extracts_exactly_one_strategy_class(tmp_path: Path) -> None:
    worker = _load_worker()
    path = tmp_path / "strategy.py"
    path.write_text("from freqtrade.strategy import IStrategy\nclass Exact(IStrategy):\n    pass\n")
    assert worker._strategy_class(path) == "Exact"
    path.write_text("class A(IStrategy): pass\nclass B(IStrategy): pass\n")
    with pytest.raises(worker.Blocked, match="exactly one"):
        worker._strategy_class(path)


def test_worker_backtest_window_uses_frozen_offline_exchange_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    worker = _load_worker()
    request = {
        "validation_attempt_id": "11111111-1111-1111-1111-111111111111",
        "attempt_request_digest": "a" * 64,
        "exchange_metadata": {"path": "/input/exchange-metadata.json"},
    }
    bundle = {"targets": [{"pair": "BTC/USDT:USDT", "timeframe": "15m"}]}
    plan = {
        "windows": [
            {
                "required": True,
                "window_key": "required-a",
                "window_member_digest": "b" * 64,
            }
        ]
    }
    observed: list[tuple[object, ...]] = []
    monkeypatch.setattr(worker, "validate_inputs", lambda *_args: (request, bundle, plan))
    monkeypatch.setattr(worker, "_strategy_class", lambda _path: "Exact")
    monkeypatch.setattr(worker, "_quality_assumptions", lambda _bundle: (0.0005, 0.0002, []))
    monkeypatch.setattr(worker, "_prepare_data", lambda *_args: None)
    monkeypatch.setattr(worker.Path, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "_run_window",
        lambda *args: observed.append(args) or {"total_trades": 1, "trades": []},
    )
    monkeypatch.setattr(
        worker,
        "_result_metrics",
        lambda *_args: {"trade_count": 1},
    )
    args = SimpleNamespace(
        request=tmp_path / "request.json",
        bundle=tmp_path / "bundle.json",
        plan=tmp_path / "plan.json",
        strategy=tmp_path / "strategy.py",
    )
    result = worker.backtest(args)
    assert result["status"] == "SUCCEEDED"
    assert observed[0][-1] == "/input/exchange-metadata.json"


def test_worker_backtest_subprocess_uses_offline_entrypoint() -> None:
    worker = _load_worker()
    source = inspect.getsource(worker._run_window)
    assert '"freqtrade-offline"' in source
    assert '"--metadata"' in source
    assert '"/home/ftuser/.local/bin/freqtrade"' not in source


def test_worker_reads_only_primary_backtest_payload_from_export(tmp_path: Path) -> None:
    worker = _load_worker()
    archive_path = tmp_path / "backtest-result-2026-08-15_00-00-00.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "backtest-result-2026-08-15_00-00-00.json",
            json.dumps({"strategy": {"Exact": {"total_trades": 3}}}),
        )
        archive.writestr("backtest-result-2026-08-15_00-00-00.meta.json", "{}")
        archive.writestr("backtest-result-2026-08-15_00-00-00_config.json", "{}")
    assert worker._read_export(tmp_path, "Exact") == {"total_trades": 3}


def test_worker_lookahead_runs_every_required_window_and_hashes_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    worker = _load_worker()
    strategy = tmp_path / "strategy.py"
    strategy.write_text(
        "from freqtrade.strategy import IStrategy\nclass Exact(IStrategy):\n    pass\n"
    )
    request = {
        "request_digest": "a" * 64,
        "strategy_version_id": "11111111-1111-1111-1111-111111111111",
        "research_target_id": "22222222-2222-2222-2222-222222222222",
        "exchange_metadata": {"path": "/input/exchange-metadata.json"},
        "windows": [
            {
                "window_key": "required-a",
                "window_member_digest": "b" * 64,
                "window_start": "2026-07-01T00:00:00+00:00",
                "window_end": "2026-07-02T00:00:00+00:00",
                "minimum_closed_candles": 1,
            },
            {
                "window_key": "required-b",
                "window_member_digest": "c" * 64,
                "window_start": "2026-07-02T00:00:00+00:00",
                "window_end": "2026-07-03T00:00:00+00:00",
                "minimum_closed_candles": 1,
            },
        ],
    }
    bundle = {
        "targets": [{"pair": "BTC/USDT:USDT", "timeframe": "15m"}],
        "configurations": [
            {
                "configuration_kind": "QUALITY_QUALIFICATION",
                "payload": {
                    "required_window_gates": [
                        {"metric": "fee_rate", "threshold": 0.0005},
                        {"metric": "slippage_rate", "threshold": 0.0002},
                    ]
                },
            }
        ],
    }
    monkeypatch.setattr(
        worker,
        "validate_lookahead_inputs",
        lambda *_args: (request, bundle),
    )

    class CandleMask:
        def __and__(self, _other):
            return self

        def sum(self):
            return 1

    class CandleDates:
        def __ge__(self, _other):
            return CandleMask()

        def __lt__(self, _other):
            return CandleMask()

    monkeypatch.setattr(
        worker,
        "_prepare_data",
        lambda *_args: {"date": CandleDates()},
    )
    monkeypatch.setattr(worker.Path, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.Path, "write_bytes", lambda *_args: 1)
    monkeypatch.setattr(worker.Path, "stat", lambda *_args, **_kwargs: SimpleNamespace(st_size=0))
    original_path_open = worker.Path.open

    exports: set[Path] = set()

    def open_path(path, mode="r", *args, **kwargs):
        if path.suffix == ".stderr":
            return io.BytesIO() if "b" in mode else io.StringIO("")
        if path in exports:
            return io.StringIO(
                "strategy,has_bias,total_signals,biased_entry_signals,biased_exit_signals\n"
                "Exact,False,20,0,0\n"
            )
        return original_path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(worker.Path, "open", open_path)
    calls: list[tuple[str, ...]] = []
    def run(command, **_kwargs):
        command = tuple(command)
        calls.append(command)
        export = Path(command[command.index("--lookahead-analysis-exportfilename") + 1])
        exports.add(export)
        monkeypatch.setattr(
            worker.Path,
            "is_file",
            lambda self: self == export,
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    result = worker.lookahead(
        SimpleNamespace(request=None, bundle=None, strategy=strategy)
    )
    assert len(calls) == 2
    assert result["status"] == "PASSED"
    assert result["has_bias"] is False
    assert result["observed_signal_count"] == 40
    assert result["failure_code"] is None
    assert len(result["window_results"]) == 2
    evidence = {key: value for key, value in result.items() if key != "evidence_digest"}
    assert result["evidence_digest"] == worker._digest(evidence)

def test_worker_classifies_pinned_freqtrade_insufficient_trade_log() -> None:
    worker = _load_worker()
    match = worker._INSUFFICIENT_TRADES.search(
        "found 3 trades which is less than minimum_trade_amount 10. "
        "Cancelling this backtest lookahead bias test."
    )
    assert match is not None
    assert int(match.group("observed")) == 3
    assert int(match.group("required")) == 10
