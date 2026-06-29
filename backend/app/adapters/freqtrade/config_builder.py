from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.adapters.freqtrade.exceptions import FreqtradeConfigError
from app.core.config import get_settings
from app.core.paths import resolve_repo_path


SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_secret",
        "key",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)


class FreqtradeConfigBuilder:
    """Build temporary Freqtrade backtest config files without credentials."""

    def __init__(self, default_output_dir: Path | None = None) -> None:
        configured_dir = default_output_dir or get_settings().tmp_freqtrade_config_dir
        self._default_output_dir = resolve_repo_path(configured_dir)

    def build_backtest_config(
        self,
        snapshot: dict[str, Any],
        output_dir: Path | None = None,
    ) -> Path:
        config = self.build_backtest_config_dict(snapshot)
        target_dir = resolve_repo_path(output_dir or self._default_output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / f"{self._config_stem(snapshot)}.json"
        target_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target_path

    def build_backtest_config_dict(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        pair = self._required_text(snapshot, "pair")
        timeframe = self._required_text(snapshot, "timeframe")
        strategy = self._required_text(snapshot, "strategy")
        exchange_snapshot = snapshot.get("exchange") or {}
        if not isinstance(exchange_snapshot, dict):
            raise FreqtradeConfigError("exchange must be an object when provided")

        exchange_name = self._optional_text(exchange_snapshot, "name", "okx")
        config: dict[str, Any] = {
            "max_open_trades": int(snapshot.get("max_open_trades", 1)),
            "stake_currency": self._optional_text(snapshot, "stake_currency", "USDT"),
            "stake_amount": snapshot.get("stake_amount", 100),
            "tradable_balance_ratio": float(snapshot.get("tradable_balance_ratio", 0.99)),
            "fiat_display_currency": self._optional_text(snapshot, "fiat_display_currency", "USD"),
            "timeframe": timeframe,
            "exchange": {
                "name": exchange_name,
                "pair_whitelist": [pair],
                "pair_blacklist": [],
            },
            "strategy": strategy,
            "user_data_dir": str(snapshot.get("user_data_dir", get_settings().freqtrade_user_data)),
            "datadir": str(snapshot.get("datadir", get_settings().market_data_dir)),
            "internals": {"process_throttle_secs": 5},
        }

        for optional_key in ("timerange", "strategy_path"):
            value = snapshot.get(optional_key)
            if value:
                config[optional_key] = str(value)

        for optional_key in ("trading_mode", "margin_mode"):
            value = exchange_snapshot.get(optional_key) or snapshot.get(optional_key)
            if value:
                config[optional_key] = str(value)

        self._reject_secret_keys(config)
        return config

    def _required_text(self, snapshot: dict[str, Any], key: str) -> str:
        value = snapshot.get(key)
        if not isinstance(value, str) or not value.strip():
            raise FreqtradeConfigError(f"{key} is required")
        return value.strip()

    def _optional_text(self, snapshot: dict[str, Any], key: str, default: str) -> str:
        value = snapshot.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise FreqtradeConfigError(f"{key} must be a non-empty string")
        return value.strip()

    def _config_stem(self, snapshot: dict[str, Any]) -> str:
        profile_name = str(snapshot.get("profile_name") or "backtest")
        pair = self._required_text(snapshot, "pair")
        timeframe = self._required_text(snapshot, "timeframe")
        strategy = self._required_text(snapshot, "strategy")
        return "-".join(
            [
                self._safe_filename_part(profile_name),
                self._safe_filename_part(strategy),
                self._safe_filename_part(pair),
                self._safe_filename_part(timeframe),
            ]
        )

    def _safe_filename_part(self, value: str) -> str:
        characters = [character.lower() if character.isalnum() else "-" for character in value]
        return "-".join("".join(characters).split("-")).strip("-") or "value"

    def _reject_secret_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in SECRET_KEY_NAMES or normalized.endswith("_secret"):
                    raise FreqtradeConfigError(
                        f"Generated config contains forbidden secret key: {key}"
                    )
                self._reject_secret_keys(item)
            return

        if isinstance(value, list):
            for item in value:
                self._reject_secret_keys(item)
