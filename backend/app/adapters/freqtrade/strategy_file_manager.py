from pathlib import Path


class StrategyFileManager:
    """Owns generated strategy files under `user_data/strategies/generated`."""

    def write_strategy_file(self, class_name: str, code: str) -> Path:
        raise NotImplementedError("Phase 0 does not write generated strategy code.")
