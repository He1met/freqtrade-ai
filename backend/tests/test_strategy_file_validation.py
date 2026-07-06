import hashlib

import pytest

from app.adapters.freqtrade.strategy_file_manager import StrategyFileManager
from app.services.strategy_file_validation import (
    StrategyFileValidationBlocked,
    StrategyFileValidationService,
)


def runnable_strategy_code(class_name: str = "SafeStrategy") -> str:
    return "\n".join(
        [
            "from freqtrade.strategy import IStrategy",
            "",
            "",
            f"class {class_name}(IStrategy):",
            "    minimal_roi = {'0': 0.01}",
            "    stoploss = -0.1",
            "",
        ]
    )


def validation_service(output_dir, approved_roots):
    return StrategyFileValidationService(
        StrategyFileManager(output_dir=output_dir, approved_roots=approved_roots)
    )


def test_validated_strategy_file_write_records_traceable_status(tmp_path) -> None:
    output_dir = tmp_path / "user_data" / "strategies" / "generated"
    output_dir.mkdir(parents=True)
    code = runnable_strategy_code()

    result = validation_service(output_dir, [output_dir]).write_validated_strategy_file(
        class_name="SafeStrategy",
        code=code,
        file_stem="safe_strategy_run_1",
    )

    expected_path = output_dir / "safe_strategy_run_1.py"
    expected_checksum = hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert result.file_path == str(expected_path)
    assert result.checksum == expected_checksum
    assert result.code_hash == expected_checksum
    assert result.write_status == "written"
    assert result.validation_status == "passed"
    assert result.validation_errors == []
    assert result.blocked_reasons == []
    assert result.approved_root == str(output_dir)
    assert expected_path.read_text(encoding="utf-8") == code


def test_validated_strategy_file_write_uses_collision_safe_suffix(tmp_path) -> None:
    output_dir = tmp_path / "user_data" / "strategies" / "generated"
    output_dir.mkdir(parents=True)
    existing_path = output_dir / "safe_strategy_run_1.py"
    existing_path.write_text("class ExistingStrategy:\n    pass\n", encoding="utf-8")
    code = runnable_strategy_code()

    result = validation_service(output_dir, [output_dir]).write_validated_strategy_file(
        class_name="SafeStrategy",
        code=code,
        file_stem="safe_strategy_run_1",
    )

    expected_path = output_dir / "safe_strategy_run_1_2.py"
    assert result.file_path == str(expected_path)
    assert result.write_status == "written"
    assert result.validation_status == "passed"
    assert existing_path.read_text(encoding="utf-8") == "class ExistingStrategy:\n    pass\n"
    assert expected_path.read_text(encoding="utf-8") == code


def test_blocks_missing_strategy_directory_without_creating_it(tmp_path) -> None:
    output_dir = tmp_path / "missing" / "generated"

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(output_dir, [output_dir]).write_validated_strategy_file(
            class_name="SafeStrategy",
            code=runnable_strategy_code(),
            file_stem="safe_strategy",
        )

    result = exc_info.value.result
    assert result.write_status == "blocked"
    assert result.validation_status == "failed"
    assert "strategy output directory does not exist" in result.blocked_reasons
    assert not output_dir.exists()


def test_blocks_strategy_directory_outside_approved_root(tmp_path) -> None:
    approved_dir = tmp_path / "approved" / "generated"
    outside_dir = tmp_path / "outside" / "generated"
    approved_dir.mkdir(parents=True)
    outside_dir.mkdir(parents=True)

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(outside_dir, [approved_dir]).write_validated_strategy_file(
            class_name="SafeStrategy",
            code=runnable_strategy_code(),
            file_stem="safe_strategy",
        )

    result = exc_info.value.result
    assert "strategy output directory is outside approved local runnable directories" in result.blocked_reasons
    assert "strategy file path is outside approved local runnable directories" in result.blocked_reasons
    assert not (outside_dir / "safe_strategy.py").exists()


def test_blocks_output_path_that_is_not_a_directory(tmp_path) -> None:
    output_path = tmp_path / "generated"
    output_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(output_path, [tmp_path]).write_validated_strategy_file(
            class_name="SafeStrategy",
            code=runnable_strategy_code(),
            file_stem="safe_strategy",
        )

    result = exc_info.value.result
    assert "strategy output path is not a directory" in result.blocked_reasons


def test_blocks_unsafe_file_stem_before_path_is_accepted(tmp_path) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(output_dir, [output_dir]).write_validated_strategy_file(
            class_name="SafeStrategy",
            code=runnable_strategy_code(),
            file_stem="../escape",
        )

    result = exc_info.value.result
    assert result.file_path is None
    assert result.write_status == "blocked"
    assert result.blocked_reasons == ["file stem contains unsafe path characters"]


def test_blocks_static_review_failures_without_writing_file(tmp_path) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    code = "\n".join(
        [
            "import os",
            "",
            "",
            "class UnsafeStrategy:",
            "    api_key = os.getenv('EXCHANGE_API_KEY')",
            "",
        ]
    )

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(output_dir, [output_dir]).write_validated_strategy_file(
            class_name="UnsafeStrategy",
            code=code,
            file_stem="unsafe_strategy",
        )

    result = exc_info.value.result
    rule_ids = {error["rule_id"] for error in result.validation_errors}
    assert result.write_status == "blocked"
    assert result.validation_status == "failed"
    assert "import.os" in rule_ids
    assert "call.os.getenv" in rule_ids
    assert not (output_dir / "unsafe_strategy.py").exists()


def test_blocks_code_that_does_not_define_requested_strategy_class(tmp_path) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()

    with pytest.raises(StrategyFileValidationBlocked) as exc_info:
        validation_service(output_dir, [output_dir]).write_validated_strategy_file(
            class_name="MissingStrategy",
            code="class OtherStrategy:\n    pass\n",
            file_stem="missing_strategy",
        )

    result = exc_info.value.result
    assert result.validation_errors[0]["rule_id"] == "class.missing"
    assert "Strategy code does not define class MissingStrategy." in result.blocked_reasons
    assert not (output_dir / "missing_strategy.py").exists()
