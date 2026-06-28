from pathlib import Path
from typing import Any


class FreqtradeConfigBuilder:
    """Builds temporary Freqtrade config files from YAML snapshots."""

    def build_backtest_config(self, snapshot: dict[str, Any], output_dir: Path) -> Path:
        raise NotImplementedError("Phase 0 does not generate runtime Freqtrade configs yet.")
