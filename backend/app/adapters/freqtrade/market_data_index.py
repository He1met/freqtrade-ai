from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from app.core.config import get_settings
from app.core.paths import resolve_repo_path


TIMEFRAME_PATTERN = re.compile(r"^\d+[smhdwM]$")
SUPPORTED_DATA_SUFFIXES = (".feather", ".parquet", ".json.gz", ".json", ".csv")


@dataclass(frozen=True)
class MarketDataFile:
    exchange: str
    pair: str
    timeframe: str
    path: Path
    relative_path: Path
    data_format: str
    file_size_bytes: int


class FreqtradeMarketDataIndex:
    """Index local Freqtrade market data files without parsing candle rows."""

    def __init__(self, market_data_dir: Path | None = None) -> None:
        configured_dir = market_data_dir or get_settings().market_data_dir
        self._market_data_dir = resolve_repo_path(configured_dir)

    def list_files(self, exchange: str | None = None) -> list[MarketDataFile]:
        if not self._market_data_dir.exists():
            return []

        exchange_dirs = [self._market_data_dir / exchange] if exchange else self._exchange_dirs()
        indexed: list[MarketDataFile] = []
        for exchange_dir in exchange_dirs:
            if not exchange_dir.is_dir():
                continue
            for path in sorted(exchange_dir.rglob("*")):
                if not path.is_file():
                    continue
                data_format = self._data_format(path)
                if not data_format:
                    continue
                parsed = self._parse_data_filename(path.name)
                if parsed is None:
                    continue
                pair, timeframe = parsed
                indexed.append(
                    MarketDataFile(
                        exchange=exchange_dir.name,
                        pair=pair,
                        timeframe=timeframe,
                        path=path,
                        relative_path=path.relative_to(self._market_data_dir),
                        data_format=data_format,
                        file_size_bytes=path.stat().st_size,
                    )
                )
        return indexed

    def _exchange_dirs(self) -> list[Path]:
        return sorted(path for path in self._market_data_dir.iterdir() if path.is_dir())

    def _data_format(self, path: Path) -> str | None:
        name = path.name.lower()
        for suffix in SUPPORTED_DATA_SUFFIXES:
            if name.endswith(suffix):
                return suffix.lstrip(".")
        return None

    def _parse_data_filename(self, filename: str) -> tuple[str, str] | None:
        stem = self._strip_supported_suffix(filename)
        parts = stem.split("-")
        timeframe_index = self._timeframe_index(parts)
        if timeframe_index is None or timeframe_index == 0:
            return None

        pair_token = "-".join(parts[:timeframe_index])
        return self._pair_from_token(pair_token), parts[timeframe_index]

    def _strip_supported_suffix(self, filename: str) -> str:
        lower_name = filename.lower()
        for suffix in SUPPORTED_DATA_SUFFIXES:
            if lower_name.endswith(suffix):
                return filename[: -len(suffix)]
        return filename

    def _timeframe_index(self, parts: list[str]) -> int | None:
        for index in range(len(parts) - 1, -1, -1):
            if TIMEFRAME_PATTERN.match(parts[index]):
                return index
        return None

    def _pair_from_token(self, token: str) -> str:
        segments = token.split("_")
        if len(segments) == 2:
            return f"{segments[0]}/{segments[1]}"
        if len(segments) >= 3:
            return f"{segments[0]}/{segments[1]}:{'_'.join(segments[2:])}"
        return token
