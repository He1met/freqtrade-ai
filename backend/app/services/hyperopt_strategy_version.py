from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from pprint import pformat
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.adapters.freqtrade.result_parser import HyperoptResultParsed
from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.models.strategy import StrategyVersion
from app.repositories import StrategyRepository
from app.schemas import StrategyVersionCreate
from app.services.strategy_file_validation import (
    StrategyFileValidationResult,
    StrategyFileValidationService,
)


FORBIDDEN_HYPEROPT_PARAM_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "secret",
    "passphrase",
    "password",
    "private_key",
    "token",
)


class HyperoptStrategyVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HyperoptStrategyVersionResult:
    optimized_version: StrategyVersion
    parent_version_id: int
    hyperopt_run_id: str
    best_params_snapshot: dict[str, Any]
    artifact_manifest_path: Optional[str]
    strategy_file: StrategyFileValidationResult


class HyperoptStrategyVersionService:
    """Creates traceable strategy child versions from parsed Hyperopt results."""

    def __init__(
        self,
        db: Session,
        file_manager: Optional[StrategyFileManager] = None,
    ) -> None:
        self.repository = StrategyRepository(db)
        self.file_manager = file_manager or StrategyFileManager()
        self.file_validation_service = StrategyFileValidationService(self.file_manager)

    def create_optimized_version(
        self,
        *,
        parent_version_id: int,
        hyperopt_run_id: str,
        hyperopt_result: HyperoptResultParsed,
        artifact_manifest_path: Optional[str] = None,
    ) -> HyperoptStrategyVersionResult:
        if not hyperopt_run_id.strip():
            raise HyperoptStrategyVersionError("BLOCKED: hyperopt_run_id is required")

        parent = self.repository.get_version(parent_version_id)
        if parent is None:
            raise HyperoptStrategyVersionError("BLOCKED: parent StrategyVersion was not found")

        best_params = deepcopy(hyperopt_result.best_params)
        if not best_params:
            raise HyperoptStrategyVersionError("BLOCKED: Hyperopt best params are required")
        self._reject_forbidden_param_keys(best_params)

        next_version_number = self._next_version_number(parent)
        derivation = self._hyperopt_derivation_snapshot(
            hyperopt_run_id=hyperopt_run_id,
            hyperopt_result=hyperopt_result,
            artifact_manifest_path=artifact_manifest_path,
        )
        blueprint = self._optimized_blueprint(parent.blueprint, derivation)
        generated_code = self._optimized_generated_code(parent.generated_code, derivation)
        file_result = self._write_optimized_strategy_file(
            parent=parent,
            generated_code=generated_code,
            hyperopt_run_id=hyperopt_run_id,
            next_version_number=next_version_number,
        )

        optimized_version = self.repository.create_version(
            StrategyVersionCreate(
                strategy_id=parent.strategy_id,
                parent_version_id=parent.id,
                version_number=next_version_number,
                blueprint=blueprint,
                generated_code=generated_code,
                code_hash=file_result.code_hash,
                file_path=str(file_result.file_path),
                validation_status=file_result.validation_status,
                validation_errors=file_result.validation_errors,
                change_summary=(
                    f"Derived from StrategyVersion {parent.id} using Hyperopt run "
                    f"{hyperopt_run_id} best epoch {hyperopt_result.best_epoch}."
                ),
                diff_snapshot=self._diff_snapshot(parent, derivation, file_result),
            )
        )
        if optimized_version is None:
            raise HyperoptStrategyVersionError(
                "BLOCKED: optimized StrategyVersion could not be created"
            )

        return HyperoptStrategyVersionResult(
            optimized_version=optimized_version,
            parent_version_id=parent.id,
            hyperopt_run_id=hyperopt_run_id,
            best_params_snapshot=best_params,
            artifact_manifest_path=artifact_manifest_path,
            strategy_file=file_result,
        )

    def _next_version_number(self, parent: StrategyVersion) -> int:
        latest = self.repository.get_latest_version(parent.strategy_id)
        if latest is None:
            return parent.version_number + 1
        return latest.version_number + 1

    def _hyperopt_derivation_snapshot(
        self,
        *,
        hyperopt_run_id: str,
        hyperopt_result: HyperoptResultParsed,
        artifact_manifest_path: Optional[str],
    ) -> dict[str, Any]:
        return {
            "source": "freqtrade_hyperopt",
            "hyperopt_run_id": hyperopt_run_id,
            "result_path": hyperopt_result.result_path,
            "artifact_manifest_path": artifact_manifest_path,
            "strategy_name": hyperopt_result.strategy_name,
            "best_epoch": hyperopt_result.best_epoch,
            "loss": hyperopt_result.loss,
            "score": hyperopt_result.score,
            "is_best": hyperopt_result.is_best,
            "spaces": list(hyperopt_result.spaces),
            "best_params": deepcopy(hyperopt_result.best_params),
            "metrics_snapshot": deepcopy(hyperopt_result.metrics_snapshot),
            "safety": {
                "dry_run_enabled": False,
                "live_trading_enabled": False,
                "exchange_connection_enabled": False,
                "download_enabled": False,
            },
        }

    def _optimized_blueprint(
        self,
        parent_blueprint: dict[str, Any],
        derivation: dict[str, Any],
    ) -> dict[str, Any]:
        blueprint = deepcopy(parent_blueprint)
        blueprint["hyperopt_derivation"] = deepcopy(derivation)
        return blueprint

    def _optimized_generated_code(
        self,
        parent_code: str,
        derivation: dict[str, Any],
    ) -> str:
        metadata = {
            "hyperopt_run_id": derivation["hyperopt_run_id"],
            "best_epoch": derivation["best_epoch"],
            "loss": derivation["loss"],
            "score": derivation["score"],
            "spaces": derivation["spaces"],
            "best_params": derivation["best_params"],
        }
        return "\n".join(
            [
                parent_code.rstrip(),
                "",
                "# Hyperopt optimization metadata for offline research review only.",
                "# This does not enable dry-run, live trading, exchange access, or deployment.",
                f"HYPEROPT_DERIVATION = {pformat(metadata, width=100)}",
                "",
            ]
        )

    def _write_optimized_strategy_file(
        self,
        *,
        parent: StrategyVersion,
        generated_code: str,
        hyperopt_run_id: str,
        next_version_number: int,
    ) -> StrategyFileValidationResult:
        class_name = str(parent.blueprint.get("class_name") or "OptimizedStrategy")
        if not class_name.isidentifier():
            class_name = "OptimizedStrategy"
        parent_stem = Path(parent.file_path).stem or class_name
        run_stem = "".join(character if character.isalnum() else "_" for character in hyperopt_run_id)
        return self.file_validation_service.write_validated_strategy_file(
            class_name=class_name,
            code=generated_code,
            file_stem=f"{parent_stem}_hyperopt_{run_stem}_v{next_version_number}",
        )

    def _diff_snapshot(
        self,
        parent: StrategyVersion,
        derivation: dict[str, Any],
        file_result: StrategyFileValidationResult,
    ) -> dict[str, Any]:
        return {
            "changed_fields": [
                "hyperopt_derivation",
                "generated_code.HYPEROPT_DERIVATION",
                "file_path",
                "code_hash",
                "strategy_file_validation",
            ],
            "before": {
                "parent_version_id": parent.parent_version_id,
                "version_number": parent.version_number,
                "file_path": parent.file_path,
                "code_hash": parent.code_hash,
            },
            "after": {
                "parent_version_id": parent.id,
                "hyperopt_run_id": derivation["hyperopt_run_id"],
                "best_epoch": derivation["best_epoch"],
                "loss": derivation["loss"],
                "score": derivation["score"],
                "spaces": derivation["spaces"],
                "best_params": deepcopy(derivation["best_params"]),
                "artifact_manifest_path": derivation["artifact_manifest_path"],
                "file_path": file_result.file_path,
                "code_hash": file_result.code_hash,
                "strategy_file_validation": file_result.to_snapshot(),
            },
        }

    def _reject_forbidden_param_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).lower().replace("-", "_")
                if any(
                    fragment in normalized_key
                    for fragment in FORBIDDEN_HYPEROPT_PARAM_KEY_FRAGMENTS
                ):
                    raise HyperoptStrategyVersionError(
                        f"BLOCKED: Hyperopt best params contain forbidden key: {key}"
                    )
                self._reject_forbidden_param_keys(child)
            return
        if isinstance(value, list):
            for child in value:
                self._reject_forbidden_param_keys(child)
