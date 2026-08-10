from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ALLOWED_RESEARCH_PAIRS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
)
ALLOWED_RESEARCH_TIMEFRAMES = ("5m", "15m")


@dataclass(frozen=True)
class ResearchTarget:
    pair: str
    timeframe: str

    @property
    def key(self) -> str:
        return f"{self.pair}|{self.timeframe}"

    @property
    def market_filename(self) -> str:
        pair = self.pair.replace("/", "_").replace(":", "_")
        return f"{pair}-{self.timeframe}-futures.feather"

    def market_path(self, datadir: Path) -> Path:
        return datadir / "futures" / self.market_filename


RESEARCH_TARGETS = tuple(
    ResearchTarget(pair=pair, timeframe=timeframe)
    for pair in ALLOWED_RESEARCH_PAIRS
    for timeframe in ALLOWED_RESEARCH_TIMEFRAMES
)


def target_for(pair: str, timeframe: str) -> ResearchTarget:
    for target in RESEARCH_TARGETS:
        if target.pair == pair and target.timeframe == timeframe:
            return target
    raise ValueError(f"unsupported formal research target: {pair} {timeframe}")
