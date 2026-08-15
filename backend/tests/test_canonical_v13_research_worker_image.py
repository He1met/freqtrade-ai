from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


def test_worker_rejects_noncanonical_json(tmp_path: Path) -> None:
    worker = _load_worker()
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"a": 1}, indent=2))
    with pytest.raises(worker.Blocked, match="canonical JSON"):
        worker._load_object(path)


def test_worker_extracts_exactly_one_strategy_class(tmp_path: Path) -> None:
    worker = _load_worker()
    path = tmp_path / "strategy.py"
    path.write_text("from freqtrade.strategy import IStrategy\nclass Exact(IStrategy):\n    pass\n")
    assert worker._strategy_class(path) == "Exact"
    path.write_text("class A(IStrategy): pass\nclass B(IStrategy): pass\n")
    with pytest.raises(worker.Blocked, match="exactly one"):
        worker._strategy_class(path)
