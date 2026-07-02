from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from app.adapters.freqtrade.market_data_index import (
    FreqtradeMarketDataIndex,
    SUPPORTED_DATA_SUFFIXES,
)
from app.core.config import get_settings
from app.core.paths import resolve_repo_path


MarketDataQualityStatus = Literal["available", "missing", "unsupported", "incomplete"]


@dataclass(frozen=True)
class MarketDataCatalogEntry:
    exchange: str
    path: Path
    relative_path: Path
    status: MarketDataQualityStatus
    data_format: Optional[str] = None
    pair: Optional[str] = None
    timeframe: Optional[str] = None
    timerange: Optional[str] = None
    file_size_bytes: int = 0
    reason: Optional[str] = None


@dataclass(frozen=True)
class MarketDataCatalogReport:
    market_data_dir: Path
    status: MarketDataQualityStatus
    entries: list[MarketDataCatalogEntry] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def available_entries(self) -> list[MarketDataCatalogEntry]:
        return [entry for entry in self.entries if entry.status == "available"]


class MarketDataCatalog:
    """Builds a local-only market data quality report for Freqtrade data files."""

    def __init__(self, market_data_dir: Optional[Path] = None) -> None:
        configured_dir = market_data_dir or get_settings().market_data_dir
        self._market_data_dir = resolve_repo_path(configured_dir)
        self._index = FreqtradeMarketDataIndex(market_data_dir=self._market_data_dir)

    def inspect(self, exchange: Optional[str] = None) -> MarketDataCatalogReport:
        if not self._market_data_dir.exists():
            return self._missing_report(
                f"market data directory does not exist: {self._market_data_dir}"
            )

        exchange_dirs = (
            [self._market_data_dir / exchange] if exchange else self._exchange_dirs()
        )
        if not exchange_dirs:
            return self._missing_report(f"no exchange directories found under {self._market_data_dir}")

        entries: list[MarketDataCatalogEntry] = []
        for exchange_dir in exchange_dirs:
            if not exchange_dir.is_dir():
                continue
            for path in sorted(exchange_dir.rglob("*")):
                if path.is_file():
                    entries.append(self._inspect_file(exchange_dir, path))

        if not entries:
            return self._missing_report(f"no local market data files found under {self._market_data_dir}")

        available = [entry for entry in entries if entry.status == "available"]
        if available:
            return MarketDataCatalogReport(
                market_data_dir=self._market_data_dir,
                status="available",
                entries=entries,
            )

        blockers = [
            f"no usable local market data files found under {self._market_data_dir}"
        ]
        statuses = {entry.status for entry in entries}
        if statuses == {"unsupported"}:
            status: MarketDataQualityStatus = "unsupported"
        elif "incomplete" in statuses:
            status = "incomplete"
        else:
            status = "missing"
        return MarketDataCatalogReport(
            market_data_dir=self._market_data_dir,
            status=status,
            entries=entries,
            blockers=blockers,
        )

    def _exchange_dirs(self) -> list[Path]:
        return sorted(path for path in self._market_data_dir.iterdir() if path.is_dir())

    def _inspect_file(self, exchange_dir: Path, path: Path) -> MarketDataCatalogEntry:
        relative_path = path.relative_to(self._market_data_dir)
        data_format = self._data_format(path)
        file_size_bytes = path.stat().st_size

        if data_format is None:
            return MarketDataCatalogEntry(
                exchange=exchange_dir.name,
                path=path,
                relative_path=relative_path,
                status="unsupported",
                file_size_bytes=file_size_bytes,
                reason="unsupported file extension",
            )

        parsed = self._index._parse_data_filename(path.name)
        if parsed is None:
            return MarketDataCatalogEntry(
                exchange=exchange_dir.name,
                path=path,
                relative_path=relative_path,
                status="incomplete",
                data_format=data_format,
                file_size_bytes=file_size_bytes,
                reason="could not infer pair and timeframe from filename",
            )

        pair, timeframe = parsed
        if file_size_bytes <= 0:
            return MarketDataCatalogEntry(
                exchange=exchange_dir.name,
                path=path,
                relative_path=relative_path,
                status="incomplete",
                data_format=data_format,
                pair=pair,
                timeframe=timeframe,
                timerange=self._timerange_from_filename(path.name),
                file_size_bytes=file_size_bytes,
                reason="market data file is empty",
            )

        return MarketDataCatalogEntry(
            exchange=exchange_dir.name,
            path=path,
            relative_path=relative_path,
            status="available",
            data_format=data_format,
            pair=pair,
            timeframe=timeframe,
            timerange=self._timerange_from_filename(path.name),
            file_size_bytes=file_size_bytes,
        )

    def _missing_report(self, blocker: str) -> MarketDataCatalogReport:
        return MarketDataCatalogReport(
            market_data_dir=self._market_data_dir,
            status="missing",
            blockers=[blocker],
        )

    def _data_format(self, path: Path) -> Optional[str]:
        name = path.name.lower()
        for suffix in SUPPORTED_DATA_SUFFIXES:
            if name.endswith(suffix):
                return suffix.lstrip(".")
        return None

    def _timerange_from_filename(self, filename: str) -> Optional[str]:
        stem = self._index._strip_supported_suffix(filename)
        parts = stem.split("-")
        for index in range(len(parts)):
            if self._index._timeframe_index(parts[: index + 1]) == index:
                remaining = parts[index + 1 :]
                if remaining and all(token.isdigit() for token in remaining):
                    return "-".join(remaining)
                return None
        return None
