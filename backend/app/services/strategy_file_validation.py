import ast
import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.services.strategy_static_review import StrategyStaticReviewService


@dataclass(frozen=True)
class StrategyFileValidationResult:
    file_path: Optional[str]
    checksum: Optional[str]
    code_hash: Optional[str]
    write_status: str
    validation_status: str
    validation_errors: list[dict[str, Any]]
    blocked_reasons: list[str]
    approved_root: Optional[str]
    validation_method: str

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "checksum": self.checksum,
            "code_hash": self.code_hash,
            "write_status": self.write_status,
            "validation_status": self.validation_status,
            "validation_errors": deepcopy(self.validation_errors),
            "blocked_reasons": list(self.blocked_reasons),
            "approved_root": self.approved_root,
            "validation_method": self.validation_method,
        }


class StrategyFileValidationBlocked(RuntimeError):
    def __init__(self, result: StrategyFileValidationResult) -> None:
        self.result = result
        reasons = "; ".join(result.blocked_reasons) or "strategy file is not runnable"
        super().__init__(f"BLOCKED: {reasons}")


class StrategyFileValidationService:
    """Writes generated strategies only after local runnable-file validation."""

    validation_method = "static_review"

    def __init__(
        self,
        file_manager: Optional[StrategyFileManager] = None,
        static_review: Optional[StrategyStaticReviewService] = None,
    ) -> None:
        self.file_manager = file_manager or StrategyFileManager()
        self.static_review = static_review or StrategyStaticReviewService()

    def write_validated_strategy_file(
        self,
        *,
        class_name: str,
        code: str,
        file_stem: Optional[str] = None,
    ) -> StrategyFileValidationResult:
        result = self._validate_and_write(
            class_name=class_name,
            code=code,
            file_stem=file_stem,
        )
        if result.write_status != "written" or result.validation_status != "passed":
            raise StrategyFileValidationBlocked(result)
        return result

    def _validate_and_write(
        self,
        *,
        class_name: str,
        code: str,
        file_stem: Optional[str],
    ) -> StrategyFileValidationResult:
        checksum = _sha256_text(code)
        candidate_path: Optional[Path] = None
        try:
            unsafe_stem_reason = self._unsafe_file_stem_reason(file_stem)
            if unsafe_stem_reason is not None:
                return self._blocked_result(
                    file_path=None,
                    checksum=checksum,
                    blocked_reasons=[unsafe_stem_reason],
                )
            candidate_path = self.file_manager.strategy_file_path(class_name, file_stem=file_stem)
        except ValueError as exc:
            return self._blocked_result(
                file_path=None,
                checksum=checksum,
                blocked_reasons=[str(exc)],
            )

        path_result = self._validate_runnable_path(candidate_path)
        if path_result is not None:
            blocked_reasons, approved_root = path_result
            return self._blocked_result(
                file_path=str(candidate_path),
                checksum=checksum,
                blocked_reasons=blocked_reasons,
                approved_root=approved_root,
            )

        static_errors = self._static_acceptance_errors(
            code=code,
            class_name=class_name,
            filename=str(candidate_path),
        )
        if static_errors:
            return self._blocked_result(
                file_path=str(candidate_path),
                checksum=checksum,
                validation_errors=static_errors,
                blocked_reasons=[str(error["message"]) for error in static_errors],
                approved_root=self._approved_root_for(candidate_path),
            )

        try:
            candidate_path.write_text(code, encoding="utf-8")
        except OSError as exc:
            return self._blocked_result(
                file_path=str(candidate_path),
                checksum=checksum,
                blocked_reasons=[f"strategy file write failed: {exc.__class__.__name__}"],
                approved_root=self._approved_root_for(candidate_path),
            )

        file_checksum = _sha256_file(candidate_path)
        return StrategyFileValidationResult(
            file_path=str(candidate_path),
            checksum=file_checksum,
            code_hash=file_checksum,
            write_status="written",
            validation_status="passed",
            validation_errors=[],
            blocked_reasons=[],
            approved_root=self._approved_root_for(candidate_path),
            validation_method=self.validation_method,
        )

    def _validate_runnable_path(
        self,
        candidate_path: Path,
    ) -> Optional[tuple[list[str], Optional[str]]]:
        blocked_reasons: list[str] = []
        output_dir = self.file_manager.output_dir.resolve(strict=False)
        approved_root = self._approved_root_for(output_dir)

        if not output_dir.exists():
            blocked_reasons.append("strategy output directory does not exist")
        elif not output_dir.is_dir():
            blocked_reasons.append("strategy output path is not a directory")

        if approved_root is None:
            blocked_reasons.append("strategy output directory is outside approved local runnable directories")

        resolved_candidate = candidate_path.resolve(strict=False)
        if resolved_candidate.parent != output_dir:
            blocked_reasons.append("strategy file path does not stay inside the configured output directory")
        if self._approved_root_for(resolved_candidate) is None:
            blocked_reasons.append("strategy file path is outside approved local runnable directories")
        if resolved_candidate.suffix != ".py":
            blocked_reasons.append("strategy file path must end with .py")
        if resolved_candidate.exists():
            blocked_reasons.append("strategy file path already exists")

        if not blocked_reasons:
            return None
        return blocked_reasons, approved_root

    def _static_acceptance_errors(
        self,
        *,
        code: str,
        class_name: str,
        filename: str,
    ) -> list[dict[str, Any]]:
        review = self.static_review.review_code(code, filename=filename)
        errors = [finding.model_dump(mode="json") for finding in review.findings]
        if review.passed:
            try:
                compile(code, filename, "exec")
            except SyntaxError as exc:
                errors.append(
                    self._validation_error(
                        code="syntax.compile",
                        message=f"Strategy code cannot be statically compiled: {exc.msg}.",
                        details={"line": exc.lineno, "column": exc.offset},
                    )
                )

        if not errors and not self._defines_class(code, class_name):
            errors.append(
                self._validation_error(
                    code="class.missing",
                    message=f"Strategy code does not define class {class_name}.",
                    details={"class_name": class_name},
                )
            )
        return errors

    def _defines_class(self, code: str, class_name: str) -> bool:
        tree = ast.parse(code)
        return any(isinstance(node, ast.ClassDef) and node.name == class_name for node in tree.body)

    def _approved_root_for(self, path: Path) -> Optional[str]:
        resolved_path = path.resolve(strict=False)
        for approved_root in self.file_manager.approved_roots:
            if _is_relative_to(resolved_path, approved_root):
                return str(approved_root)
        return None

    def _unsafe_file_stem_reason(self, file_stem: Optional[str]) -> Optional[str]:
        if file_stem is None:
            return None
        if "/" in file_stem or "\\" in file_stem or ".." in file_stem:
            return "file stem contains unsafe path characters"
        return None

    def _blocked_result(
        self,
        *,
        file_path: Optional[str],
        checksum: Optional[str],
        blocked_reasons: list[str],
        validation_errors: Optional[list[dict[str, Any]]] = None,
        approved_root: Optional[str] = None,
    ) -> StrategyFileValidationResult:
        errors = validation_errors or [
            self._validation_error(code="strategy_file.blocked", message=reason)
            for reason in blocked_reasons
        ]
        return StrategyFileValidationResult(
            file_path=file_path,
            checksum=checksum,
            code_hash=checksum,
            write_status="blocked",
            validation_status="failed",
            validation_errors=errors,
            blocked_reasons=blocked_reasons,
            approved_root=approved_root,
            validation_method=self.validation_method,
        )

    def _validation_error(
        self,
        *,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "rule_id": code,
            "category": "strategy_file_validation",
            "severity": "error",
            "message": message,
            "details": details or {},
        }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
