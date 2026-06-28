from pathlib import Path
from typing import Any


class FreqtradeResultParser:
    """Parses Freqtrade JSON reports into project-owned result DTOs."""

    def parse_backtest_result(self, result_path: Path) -> dict[str, Any]:
        raise NotImplementedError("Phase 0 does not parse Freqtrade backtest results.")
