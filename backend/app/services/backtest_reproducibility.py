from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional

from app.adapters.freqtrade.market_data_catalog import (
    MarketDataCatalogEntry,
    MarketDataCatalogReport,
)
from app.schemas.backtest import BacktestResultCreate
from app.schemas.backtest_profile import BacktestProfileV2


BacktestBaselineStatus = Literal["STABLE", "CHANGED", "MISSING_BASELINE", "BLOCKED"]
COMPARABLE_METRICS = (
    "profit_total",
    "profit_pct",
    "max_drawdown_pct",
    "win_rate",
    "total_trades",
    "timerange",
)


@dataclass(frozen=True)
class BacktestReproducibilityFingerprint:
    profile_hash: str
    strategy_version: str
    data_fingerprint: str
    fingerprint_hash: str
    profile_name: str
    pair: str
    timeframe: str
    data_relative_path: Optional[Path] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_hash": self.profile_hash,
            "strategy_version": self.strategy_version,
            "data_fingerprint": self.data_fingerprint,
            "fingerprint_hash": self.fingerprint_hash,
            "profile_name": self.profile_name,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "data_relative_path": (
                self.data_relative_path.as_posix()
                if self.data_relative_path is not None
                else None
            ),
        }


@dataclass(frozen=True)
class BacktestMetricDiff:
    metric: str
    baseline: object
    candidate: object
    delta: Optional[float] = None
    delta_pct: Optional[float] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
        }


@dataclass(frozen=True)
class BacktestBaselineComparison:
    status: BacktestBaselineStatus
    fingerprint: Optional[BacktestReproducibilityFingerprint] = None
    metric_diffs: list[BacktestMetricDiff] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "fingerprint": self.fingerprint.to_dict() if self.fingerprint else None,
            "metric_diffs": [diff.to_dict() for diff in self.metric_diffs],
            "warnings": list(self.warnings),
            "blocked_reason": self.blocked_reason,
        }


class BacktestReproducibilityService:
    """Builds deterministic backtest fingerprints and compares fixture results."""

    def build_fingerprint(
        self,
        profile: BacktestProfileV2 | dict[str, object],
        strategy_version: str | int,
        catalog_report: MarketDataCatalogReport,
    ) -> BacktestReproducibilityFingerprint:
        validated_profile = self._validate_profile(profile)
        data_entry = self._matching_data_entry(catalog_report, validated_profile)
        if data_entry is None:
            raise ValueError(self._blocked_reason(validated_profile, catalog_report.blockers))

        profile_hash = self._hash_payload(validated_profile.to_snapshot())
        strategy_version_text = str(strategy_version)
        data_fingerprint = self._hash_payload(self._data_fingerprint_payload(data_entry))
        fingerprint_hash = self._hash_payload(
            {
                "profile_hash": profile_hash,
                "strategy_version": strategy_version_text,
                "data_fingerprint": data_fingerprint,
            }
        )
        return BacktestReproducibilityFingerprint(
            profile_hash=profile_hash,
            strategy_version=strategy_version_text,
            data_fingerprint=data_fingerprint,
            fingerprint_hash=fingerprint_hash,
            profile_name=validated_profile.profile_name,
            pair=validated_profile.pair,
            timeframe=validated_profile.timeframe,
            data_relative_path=data_entry.relative_path,
        )

    def compare_results(
        self,
        profile: BacktestProfileV2 | dict[str, object],
        strategy_version: str | int,
        catalog_report: MarketDataCatalogReport,
        baseline_result: BacktestResultCreate | dict[str, object] | None,
        candidate_result: BacktestResultCreate | dict[str, object],
    ) -> BacktestBaselineComparison:
        validated_profile = self._validate_profile(profile)
        data_entry = self._matching_data_entry(catalog_report, validated_profile)
        if data_entry is None:
            return BacktestBaselineComparison(
                status="BLOCKED",
                blocked_reason=self._blocked_reason(validated_profile, catalog_report.blockers),
            )

        fingerprint = self.build_fingerprint(
            validated_profile,
            strategy_version=strategy_version,
            catalog_report=catalog_report,
        )
        if baseline_result is None:
            return BacktestBaselineComparison(
                status="MISSING_BASELINE",
                fingerprint=fingerprint,
                warnings=["No baseline result is available for this reproducibility fingerprint."],
            )

        baseline = self._validate_result(baseline_result)
        candidate = self._validate_result(candidate_result)
        metric_diffs = self._metric_diffs(baseline, candidate)
        status: BacktestBaselineStatus = "CHANGED" if metric_diffs else "STABLE"
        warnings = [
            f"{diff.metric} changed from {diff.baseline!r} to {diff.candidate!r}"
            for diff in metric_diffs
        ]
        return BacktestBaselineComparison(
            status=status,
            fingerprint=fingerprint,
            metric_diffs=metric_diffs,
            warnings=warnings,
        )

    def _validate_profile(self, profile: BacktestProfileV2 | dict[str, object]) -> BacktestProfileV2:
        if isinstance(profile, BacktestProfileV2):
            return profile
        return BacktestProfileV2.model_validate(profile)

    def _validate_result(
        self,
        result: BacktestResultCreate | dict[str, object],
    ) -> BacktestResultCreate:
        if isinstance(result, BacktestResultCreate):
            return result
        return BacktestResultCreate.model_validate(result)

    def _matching_data_entry(
        self,
        catalog_report: MarketDataCatalogReport,
        profile: BacktestProfileV2,
    ) -> Optional[MarketDataCatalogEntry]:
        for entry in catalog_report.available_entries:
            if (
                entry.exchange == profile.data_source.exchange
                and entry.pair == profile.pair
                and entry.timeframe == profile.timeframe
            ):
                return entry
        return None

    def _metric_diffs(
        self,
        baseline: BacktestResultCreate,
        candidate: BacktestResultCreate,
    ) -> list[BacktestMetricDiff]:
        diffs: list[BacktestMetricDiff] = []
        for metric in COMPARABLE_METRICS:
            baseline_value = getattr(baseline, metric)
            candidate_value = getattr(candidate, metric)
            if self._values_equal(baseline_value, candidate_value):
                continue
            delta = self._numeric_delta(baseline_value, candidate_value)
            diffs.append(
                BacktestMetricDiff(
                    metric=metric,
                    baseline=baseline_value,
                    candidate=candidate_value,
                    delta=delta,
                    delta_pct=self._delta_pct(baseline_value, delta),
                )
            )
        return diffs

    def _blocked_reason(self, profile: BacktestProfileV2, blockers: Iterable[str]) -> str:
        base = (
            "no available local market data for "
            f"{profile.data_source.exchange} {profile.pair} {profile.timeframe}"
        )
        blocker_text = "; ".join(blockers)
        if blocker_text:
            return f"{base}: {blocker_text}"
        return base

    def _data_fingerprint_payload(self, entry: MarketDataCatalogEntry) -> dict[str, object]:
        return {
            "exchange": entry.exchange,
            "relative_path": entry.relative_path.as_posix(),
            "status": entry.status,
            "data_format": entry.data_format,
            "pair": entry.pair,
            "timeframe": entry.timeframe,
            "timerange": entry.timerange,
            "file_size_bytes": entry.file_size_bytes,
        }

    def _hash_payload(self, payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    def _values_equal(self, baseline_value: object, candidate_value: object) -> bool:
        if isinstance(baseline_value, float) or isinstance(candidate_value, float):
            if baseline_value is None or candidate_value is None:
                return baseline_value is candidate_value
            return abs(float(baseline_value) - float(candidate_value)) <= 1e-12
        return baseline_value == candidate_value

    def _numeric_delta(self, baseline_value: object, candidate_value: object) -> Optional[float]:
        if isinstance(baseline_value, (int, float)) and isinstance(candidate_value, (int, float)):
            return float(candidate_value) - float(baseline_value)
        return None

    def _delta_pct(self, baseline_value: object, delta: Optional[float]) -> Optional[float]:
        if delta is None or not isinstance(baseline_value, (int, float)) or baseline_value == 0:
            return None
        return delta / float(baseline_value)
