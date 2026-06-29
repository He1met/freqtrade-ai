from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.paths import resolve_repo_path


class StrategyFileManager:
    """Owns generated strategy files under `user_data/strategies/generated`."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        configured_dir = output_dir or get_settings().strategy_output_dir
        self.output_dir = resolve_repo_path(configured_dir)

    def write_strategy_file(
        self,
        class_name: str,
        code: str,
        file_stem: Optional[str] = None,
    ) -> Path:
        if not class_name.isidentifier():
            raise ValueError("class_name must be a valid Python identifier")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = file_stem or class_name
        path = self.output_dir / f"{self._safe_file_stem(stem)}.py"
        path.write_text(code, encoding="utf-8")
        return path

    def _safe_file_stem(self, value: str) -> str:
        cleaned = "".join(character if character.isalnum() else "_" for character in value)
        cleaned = cleaned.strip("_").lower()
        if not cleaned:
            raise ValueError("file stem cannot be empty")
        return cleaned
