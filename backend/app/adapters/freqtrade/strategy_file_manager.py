from pathlib import Path
from typing import Optional, Sequence

from app.core.config import get_settings
from app.core.paths import resolve_repo_path


class StrategyFileManager:
    """Owns generated strategy files under `user_data/strategies/generated`."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        approved_roots: Optional[Sequence[Path]] = None,
    ) -> None:
        settings = get_settings()
        configured_dir = output_dir or settings.strategy_output_dir
        configured_approved_roots = (
            approved_roots
            if approved_roots is not None
            else [configured_dir]
        )
        self.output_dir = resolve_repo_path(configured_dir).resolve(strict=False)
        self.approved_roots = [
            resolve_repo_path(approved_root).resolve(strict=False)
            for approved_root in configured_approved_roots
        ]

    def write_strategy_file(
        self,
        class_name: str,
        code: str,
        file_stem: Optional[str] = None,
    ) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.strategy_file_path(class_name, file_stem=file_stem)
        path.write_text(code, encoding="utf-8")
        return path

    def strategy_file_path(self, class_name: str, file_stem: Optional[str] = None) -> Path:
        if not class_name.isidentifier():
            raise ValueError("class_name must be a valid Python identifier")
        stem = file_stem or class_name
        return self.output_dir / f"{self._safe_file_stem(stem)}.py"

    def _safe_file_stem(self, value: str) -> str:
        cleaned = "".join(character if character.isalnum() else "_" for character in value)
        cleaned = cleaned.strip("_").lower()
        if not cleaned:
            raise ValueError("file stem cannot be empty")
        return cleaned
