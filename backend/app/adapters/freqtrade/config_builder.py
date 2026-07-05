from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from app.adapters.freqtrade.exceptions import FreqtradeConfigError
from app.core.config import get_settings
from app.core.paths import resolve_repo_path
from app.schemas.backtest_profile import BacktestProfileV2
from app.schemas.dry_run_profile import DryRunProfile


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
DEFAULT_DRY_RUN_REQUIRED_ENV_VARS = (
    "FREQTRADE_DRY_RUN_API_KEY",
    "FREQTRADE_DRY_RUN_API_SECRET",
)
DEFAULT_DRY_RUN_OPTIONAL_ENV_VARS = ("FREQTRADE_DRY_RUN_API_PASSPHRASE",)
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class DryRunEnvPreflight:
    status: Literal["READY", "BLOCKED"]
    required_env_present: tuple[str, ...]
    required_env_missing: tuple[str, ...]
    optional_env_present: tuple[str, ...]
    optional_env_missing: tuple[str, ...]
    blocked_reason: str | None = None

    def to_report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "required_env_present": list(self.required_env_present),
            "required_env_missing": list(self.required_env_missing),
            "optional_env_present": list(self.optional_env_present),
            "optional_env_missing": list(self.optional_env_missing),
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True)
class DryRunConfigBuildResult:
    config_path: Path
    config: dict[str, Any]
    env_preflight: DryRunEnvPreflight


class FreqtradeConfigBuilder:
    """Build temporary Freqtrade backtest config files without credentials.

    This builder owns the narrow config shape used by the project. It does not
    read user Freqtrade config files and rejects secret-looking keys before the
    generated JSON is written.
    """

    def __init__(self, default_output_dir: Path | None = None) -> None:
        configured_dir = default_output_dir or get_settings().tmp_freqtrade_config_dir
        self._default_output_dir = resolve_repo_path(configured_dir)

    def build_backtest_config(
        self,
        snapshot: BacktestProfileV2 | dict[str, Any],
        output_dir: Path | None = None,
    ) -> Path:
        snapshot = self._normalize_snapshot(snapshot)
        config = self.build_backtest_config_dict(snapshot)
        target_dir = resolve_repo_path(output_dir or self._default_output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / f"{self._config_stem(snapshot)}.json"
        target_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target_path

    def build_backtest_config_dict(self, snapshot: BacktestProfileV2 | dict[str, Any]) -> dict[str, Any]:
        snapshot = self._normalize_snapshot(snapshot)
        # Keep the snapshot input small and explicit so callers cannot smuggle
        # exchange credentials or unrelated runtime settings into backtesting.
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
            "pairlists": [{"method": "StaticPairList"}],
            "strategy": strategy,
            "user_data_dir": str(snapshot.get("user_data_dir", get_settings().freqtrade_user_data)),
            "datadir": str(snapshot.get("datadir", get_settings().market_data_dir)),
            "bot_name": self._optional_text(
                snapshot,
                "bot_name",
                "freqtrade_ai_backtest",
            ),
            "initial_state": "stopped",
            "internals": {"process_throttle_secs": 5},
            "unfilledtimeout": {
                "entry": 10,
                "exit": 10,
                "exit_timeout_count": 0,
                "unit": "minutes",
            },
            "entry_pricing": {
                "price_side": "same",
                "use_order_book": True,
                "order_book_top": 1,
                "price_last_balance": 0.0,
                "check_depth_of_market": {
                    "enabled": False,
                    "bids_to_ask_delta": 1,
                },
            },
            "exit_pricing": {
                "price_side": "same",
                "use_order_book": True,
                "order_book_top": 1,
            },
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

    def build_dry_run_config(
        self,
        profile: DryRunProfile | dict[str, Any],
        output_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
        required_env_vars: tuple[str, ...] = DEFAULT_DRY_RUN_REQUIRED_ENV_VARS,
        optional_env_vars: tuple[str, ...] = DEFAULT_DRY_RUN_OPTIONAL_ENV_VARS,
    ) -> DryRunConfigBuildResult:
        profile = self._normalize_dry_run_profile(profile)
        env_preflight = self.check_dry_run_env(
            environ=environ,
            required_env_vars=required_env_vars,
            optional_env_vars=optional_env_vars,
        )
        config = self.build_dry_run_config_dict(profile)
        target_dir = self._resolve_dry_run_output_dir(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / f"{self._dry_run_config_stem(profile)}.json"
        target_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return DryRunConfigBuildResult(
            config_path=target_path,
            config=config,
            env_preflight=env_preflight,
        )

    def build_dry_run_config_dict(self, profile: DryRunProfile | dict[str, Any]) -> dict[str, Any]:
        profile = self._normalize_dry_run_profile(profile)
        config: dict[str, Any] = {
            "max_open_trades": profile.stake.max_open_trades,
            "stake_currency": profile.stake.currency,
            "stake_amount": profile.stake.amount,
            "tradable_balance_ratio": profile.stake.tradable_balance_ratio,
            "fiat_display_currency": "USD",
            "timeframe": profile.timeframe,
            "dry_run": True,
            "exchange": {
                "name": profile.exchange.name,
                "pair_whitelist": [profile.pair],
                "pair_blacklist": [],
            },
            "pairlists": [{"method": "StaticPairList"}],
            "strategy": profile.strategy.name,
            "user_data_dir": profile.command_options.user_data_dir,
            "bot_name": f"freqtrade_ai_{self._safe_filename_part(profile.name)}",
            "initial_state": "stopped",
            "internals": {"process_throttle_secs": 5},
        }

        if profile.exchange.trading_mode:
            config["trading_mode"] = profile.exchange.trading_mode
        if profile.command_options.strategy_path:
            config["strategy_path"] = profile.command_options.strategy_path

        self._reject_secret_keys(config)
        return config

    def check_dry_run_env(
        self,
        environ: Mapping[str, str] | None = None,
        required_env_vars: tuple[str, ...] = DEFAULT_DRY_RUN_REQUIRED_ENV_VARS,
        optional_env_vars: tuple[str, ...] = DEFAULT_DRY_RUN_OPTIONAL_ENV_VARS,
    ) -> DryRunEnvPreflight:
        import os

        self._validate_env_names(required_env_vars + optional_env_vars)
        env = os.environ if environ is None else environ
        required_present, required_missing = self._split_env_presence(required_env_vars, env)
        optional_present, optional_missing = self._split_env_presence(optional_env_vars, env)
        status: Literal["READY", "BLOCKED"] = "READY"
        blocked_reason = None
        if required_missing:
            status = "BLOCKED"
            blocked_reason = "required ENV variables are missing or empty: " + ", ".join(
                required_missing
            )

        return DryRunEnvPreflight(
            status=status,
            required_env_present=required_present,
            required_env_missing=required_missing,
            optional_env_present=optional_present,
            optional_env_missing=optional_missing,
            blocked_reason=blocked_reason,
        )

    def _normalize_snapshot(self, snapshot: BacktestProfileV2 | dict[str, Any]) -> dict[str, Any]:
        if isinstance(snapshot, BacktestProfileV2):
            return snapshot.to_config_snapshot()
        if snapshot.get("schema_version") == "2":
            return BacktestProfileV2.model_validate(snapshot).to_config_snapshot()
        return snapshot

    def _normalize_dry_run_profile(self, profile: DryRunProfile | dict[str, Any]) -> DryRunProfile:
        if isinstance(profile, DryRunProfile):
            return profile
        return DryRunProfile.model_validate(profile)

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
        """Recursively reject credential-shaped keys in generated config."""

        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if (
                    normalized in SECRET_KEY_NAMES
                    or normalized.endswith("_secret")
                    or "api_key" in normalized
                    or "api_secret" in normalized
                ):
                    raise FreqtradeConfigError(
                        f"Generated config contains forbidden secret key: {key}"
                    )
                self._reject_secret_keys(item)
            return

        if isinstance(value, list):
            for item in value:
                self._reject_secret_keys(item)

    def _dry_run_config_stem(self, profile: DryRunProfile) -> str:
        return "-".join(
            [
                self._safe_filename_part(profile.name),
                self._safe_filename_part(profile.strategy.name),
                self._safe_filename_part(profile.pair),
                self._safe_filename_part(profile.timeframe),
                "dry-run",
            ]
        )

    def _resolve_dry_run_output_dir(self, output_dir: Path | None) -> Path:
        target_dir = resolve_repo_path(output_dir or self._default_output_dir)
        default_dir = self._default_output_dir.resolve()
        tmp_dir = Path("/tmp").resolve()
        if (
            self._path_is_relative_to(target_dir, default_dir)
            or self._path_is_relative_to(target_dir, tmp_dir)
        ):
            return target_dir
        raise FreqtradeConfigError(
            "Dry-run config output_dir must be under the controlled tmp config directory"
        )

    def _validate_env_names(self, names: tuple[str, ...]) -> None:
        for name in names:
            if not ENV_NAME_PATTERN.fullmatch(name):
                raise FreqtradeConfigError(f"Invalid ENV variable name: {name}")

    def _split_env_presence(
        self,
        names: tuple[str, ...],
        env: Mapping[str, str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        present: list[str] = []
        missing: list[str] = []
        for name in names:
            value = env.get(name)
            if value is None or value.strip() == "":
                missing.append(name)
            else:
                present.append(name)
        return tuple(present), tuple(missing)

    def _path_is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            return False
        return True
