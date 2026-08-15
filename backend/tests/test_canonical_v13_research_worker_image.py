from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        command = tuple(command)
        calls.append(command)
        export = Path(command[command.index("--lookahead-analysis-exportfilename") + 1])
        monkeypatch.setattr(
            worker.Path,
            "is_file",
            lambda self: self == export,
        )
        monkeypatch.setattr(
            worker.Path,
            "open",
            lambda self, **_kwargs: __import__("io").StringIO(
                "strategy,has_bias,total_signals,biased_entry_signals,biased_exit_signals\n"
                "Exact,False,20,0,0\n"
            ),
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
    assert len(result["window_results"]) == 2
    evidence = {key: value for key, value in result.items() if key != "evidence_digest"}
    assert result["evidence_digest"] == worker._digest(evidence)
