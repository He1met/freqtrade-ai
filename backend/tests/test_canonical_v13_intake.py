from __future__ import annotations

from dataclasses import replace
import builtins
import inspect as python_inspect

import pytest
from sqlalchemy import create_engine, func, select

from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.intake import (
    CanonicalIntakeBlocked,
    ExternalSourceEntrySnapshot,
    ExternalVersionSnapshot,
    controlled_submit_latest,
    inspect_intake_artifact,
    select_latest_source_artifact,
)


ALPHA_SOURCE = b"from freqtrade.strategy import IStrategy\nclass Alpha(IStrategy):\n    pass\n"
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import (
    AUDIT_EVENTS_TABLE,
    IDEMPOTENCY_RECEIPTS_TABLE,
    STRATEGIES_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_INTAKE_RECEIPTS_TABLE,
    STRATEGY_SUBMISSIONS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)


def _snapshot(
    *,
    entry: str = "legacy/alpha.py",
    strategy: str = "Alpha",
    current: str = "version-2",
    content: bytes = ALPHA_SOURCE,
) -> ExternalSourceEntrySnapshot:
    return ExternalSourceEntrySnapshot(
        archive_snapshot_digest="a" * 64,
        source_entry_key=entry,
        source_strategy_key=strategy,
        current_version_id=current,
        versions=(
            ExternalVersionSnapshot(strategy, "version-1", 1, b"old source is not selected"),
            ExternalVersionSnapshot(strategy, "version-2", 2, content),
        ),
    )


def _connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    with connection.begin():
        install_canonical_genesis(connection, installer_identity="phase2-intake-test")
    translated = connection.execution_options(
        schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
    )
    return engine, connection, translated


def _count(connection, table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_latest_selector_proves_current_owner_and_highest_version() -> None:
    selected = select_latest_source_artifact(_snapshot())
    assert selected.version_id == "version-2"
    assert selected.version_number == 2
    assert b"class Alpha(IStrategy)" in selected.artifact_bytes
    assert len(selected.source_entry_digest) == 64


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (replace(_snapshot(), current_version_id="missing"), "BLOCKED_CURRENT_VERSION_OWNERSHIP"),
        (replace(_snapshot(), current_version_id="version-1"), "BLOCKED_AMBIGUOUS_LATEST_SOURCE"),
        (
            replace(
                _snapshot(),
                versions=(
                    ExternalVersionSnapshot("other", "version-1", 1, b"x"),
                    _snapshot().versions[1],
                ),
            ),
            "BLOCKED_CURRENT_VERSION_OWNERSHIP",
        ),
        (
            replace(
                _snapshot(),
                versions=(
                    _snapshot().versions[0],
                    replace(_snapshot().versions[1], version_number=1),
                ),
            ),
            "BLOCKED_AMBIGUOUS_LATEST_SOURCE",
        ),
        (replace(_snapshot(), source_entry_key="../escape.py"), "BLOCKED_PATH_TRAVERSAL"),
        (replace(_snapshot(), source_entry_key="legacy\\escape.py"), "BLOCKED_PATH_TRAVERSAL"),
    ],
)
def test_latest_selector_fails_closed_on_ambiguity_or_traversal(
    snapshot: ExternalSourceEntrySnapshot, code: str
) -> None:
    with pytest.raises(CanonicalIntakeBlocked) as raised:
        select_latest_source_artifact(snapshot)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "REJECTED_EMPTY_ARTIFACT"),
        (b"\xff\xfe", "REJECTED_INVALID_UTF8"),
        (b"x = '\x00'", "REJECTED_CONTROL_CHARACTER"),
        (b"api_key = 'this-is-a-real-looking-secret'", "REJECTED_SECRET_SHAPED_CONTENT"),
        (b"-----BEGIN PRIVATE KEY-----\nabc", "REJECTED_SECRET_SHAPED_CONTENT"),
    ],
)
def test_intake_inspection_rejects_unsafe_bytes(content: bytes, code: str) -> None:
    with pytest.raises(CanonicalIntakeBlocked) as raised:
        inspect_intake_artifact(content)
    assert raised.value.code == code


def test_inspection_parses_ast_without_compiling_importing_or_executing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("strategy source was executed")

    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    result = inspect_intake_artifact(ALPHA_SOURCE, expected_strategy_class="Alpha")
    assert result.strategy_class == "Alpha"
    assert result.checks["ast_parse"] == "PASSED"
    assert result.checks["static_validation"] == "PASSED"
    assert result.checks["lookahead_validation"] == "NOT_RUN"
    assert result.checks["backtest"] == "NOT_RUN"
    assert result.checks["execution"] == "NOT_AUTHORIZED"

    source = python_inspect.getsource(__import__("app.canonical_v13.intake", fromlist=["*"]))
    for forbidden in (
        "import subprocess",
        "import importlib",
        "builtins.compile(",
        "builtins.exec(",
        "builtins.eval(",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("content", "expected", "code"),
    [
        (b"class Alpha(:\n", "Alpha", "REJECTED_INVALID_PYTHON_AST"),
        (b"class Alpha:\n    pass\n", "Alpha", "REJECTED_STRATEGY_BASE"),
        (
            b"from freqtrade.strategy import IStrategy\nclass Other(IStrategy):\n    pass\n",
            "Alpha",
            "REJECTED_STRATEGY_CLASS_MISMATCH",
        ),
        (
            b"import os\n"
            b"from freqtrade.strategy import IStrategy\n"
            b"class Alpha(IStrategy):\n    pass\n",
            "Alpha",
            "REJECTED_IMPORT_NOT_ALLOWED",
        ),
        (
            b"from freqtrade.strategy import IStrategy\n"
            b"open('x')\n"
            b"class Alpha(IStrategy):\n    pass\n",
            "Alpha",
            "REJECTED_MODULE_LEVEL_EXECUTION",
        ),
        (
            b"from freqtrade.strategy import IStrategy\n"
            b"class Alpha(IStrategy):\n"
            b"    def x(self):\n        return open('x')\n",
            "Alpha",
            "REJECTED_DANGEROUS_CALL",
        ),
    ],
)
def test_inspection_rejects_unsafe_ast(
    content: bytes, expected: str, code: str
) -> None:
    with pytest.raises(CanonicalIntakeBlocked) as raised:
        inspect_intake_artifact(content, expected_strategy_class=expected)
    assert raised.value.code == code


def test_controlled_submission_is_atomic_idempotent_and_stops_unvalidated() -> None:
    engine, raw, connection = _connection()
    try:
        with connection.begin():
            first = controlled_submit_latest(
                connection,
                caller_identity="phase2-test-caller",
                idempotency_key="phase2-alpha-1",
                display_name="Imported Alpha",
                snapshot=_snapshot(),
            )
        assert first.idempotent_replay is False
        assert first.status == "INTAKE_ACCEPTED"
        assert first.catalog_status == "DRAFT"
        assert first.validation_status == "UNVALIDATED"
        assert first.execution_authorized is False

        with connection.begin():
            repeated = controlled_submit_latest(
                connection,
                caller_identity="phase2-test-caller",
                idempotency_key="phase2-alpha-1",
                display_name="Imported Alpha",
                snapshot=_snapshot(),
            )
        assert repeated.idempotent_replay is True
        assert repeated.submission_id == first.submission_id
        assert repeated.strategy_id == first.strategy_id
        assert repeated.strategy_version_id == first.strategy_version_id
        assert repeated.receipt_digest == first.receipt_digest

        for table in (
            STRATEGY_ARTIFACTS_TABLE,
            STRATEGY_SUBMISSIONS_TABLE,
            STRATEGY_INTAKE_RECEIPTS_TABLE,
            STRATEGIES_TABLE,
            STRATEGY_VERSIONS_TABLE,
            IDEMPOTENCY_RECEIPTS_TABLE,
            AUDIT_EVENTS_TABLE,
        ):
            assert _count(connection, table) == 1
    finally:
        raw.close()
        engine.dispose()


def test_same_artifact_is_shared_but_strategy_identity_is_never_merged() -> None:
    engine, raw, connection = _connection()
    try:
        with connection.begin():
            alpha = controlled_submit_latest(
                connection,
                caller_identity="phase2-test-caller",
                idempotency_key="alpha",
                display_name="Alpha",
                snapshot=_snapshot(),
            )
            beta = controlled_submit_latest(
                connection,
                caller_identity="phase2-test-caller",
                idempotency_key="beta",
                display_name="Beta",
                snapshot=_snapshot(entry="legacy/beta.py", strategy="Alpha"),
            )
        assert alpha.artifact_id == beta.artifact_id
        assert alpha.strategy_id != beta.strategy_id
        assert alpha.strategy_version_id != beta.strategy_version_id
        assert _count(connection, STRATEGY_ARTIFACTS_TABLE) == 1
        assert _count(connection, STRATEGIES_TABLE) == 2
        assert _count(connection, STRATEGY_VERSIONS_TABLE) == 2
    finally:
        raw.close()
        engine.dispose()


def test_reused_key_or_changed_source_entry_is_blocked_without_row_growth() -> None:
    engine, raw, connection = _connection()
    try:
        with connection.begin():
            controlled_submit_latest(
                connection,
                caller_identity="phase2-test-caller",
                idempotency_key="stable-key",
                display_name="Alpha",
                snapshot=_snapshot(),
            )
        counts_before = {
            table.name: _count(connection, table)
            for table in (STRATEGY_ARTIFACTS_TABLE, STRATEGY_SUBMISSIONS_TABLE, STRATEGIES_TABLE)
        }
        connection.rollback()

        with pytest.raises(CanonicalIntakeBlocked) as reused:
            with connection.begin():
                controlled_submit_latest(
                    connection,
                    caller_identity="phase2-test-caller",
                    idempotency_key="stable-key",
                    display_name="Changed name",
                    snapshot=_snapshot(),
                )
        assert reused.value.code == "BLOCKED_IDEMPOTENCY_KEY_REUSE"

        with pytest.raises(CanonicalIntakeBlocked) as drifted:
            with connection.begin():
                controlled_submit_latest(
                    connection,
                    caller_identity="another-caller",
                    idempotency_key="another-key",
                    display_name="Changed name",
                    snapshot=_snapshot(
                        content=b"from freqtrade.strategy import IStrategy\n"
                        b"class Alpha(IStrategy):\n    changed = True\n"
                    ),
                )
        assert drifted.value.code == "BLOCKED_SOURCE_ENTRY_DRIFT"
        counts_after = {
            table.name: _count(connection, table)
            for table in (STRATEGY_ARTIFACTS_TABLE, STRATEGY_SUBMISSIONS_TABLE, STRATEGIES_TABLE)
        }
        assert counts_after == counts_before
    finally:
        raw.close()
        engine.dispose()


def test_ambiguous_latest_and_unsafe_content_are_complete_noops() -> None:
    engine, raw, connection = _connection()
    try:
        for snapshot in (
            replace(_snapshot(), current_version_id="version-1"),
            _snapshot(content=b"password = 'do-not-persist-this-secret'"),
        ):
            with pytest.raises(CanonicalIntakeBlocked):
                with connection.begin():
                    controlled_submit_latest(
                        connection,
                        caller_identity="phase2-test-caller",
                        idempotency_key="blocked",
                        display_name="Blocked",
                        snapshot=snapshot,
                    )
        for table in (
            STRATEGY_ARTIFACTS_TABLE,
            STRATEGY_SUBMISSIONS_TABLE,
            STRATEGY_INTAKE_RECEIPTS_TABLE,
            STRATEGIES_TABLE,
            STRATEGY_VERSIONS_TABLE,
        ):
            assert _count(connection, table) == 0
    finally:
        raw.close()
        engine.dispose()
