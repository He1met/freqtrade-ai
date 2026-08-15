from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.canonical_v13.freqtrade_production import (
    PRODUCTION_LOOKAHEAD_ACTIVATION,
    PRODUCTION_RESEARCH_ACTIVATION,
    CanonicalProductionResearchBlocked,
    FreqtradeProductionLookaheadAdapter,
    FreqtradeProductionResearchAdapter,
    ProductionLookaheadInputSet,
    ProductionResearchLimits,
    SandboxCommandResult,
    execute_production_static_lookahead_gate,
    materialize_production_research_inputs,
)
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import MARKET_ARTIFACTS_TABLE
from app.canonical_v13 import research_scoring
from app.canonical_v13.research_authorization import (
    authorize_research_execution,
    consume_research_execution_authorization,
)
from app.canonical_v13.research_execution import start_consumed_research_attempt
from app.canonical_v13.research_orchestration import (
    execute_production_research_chain,
    plan_serial_research_batches,
    read_research_chain_projection,
)
from app.canonical_v13.research_validation import (
    ResearchLineage,
    StaticValidationReceipt,
    build_ephemeral_launch_spec,
    build_ephemeral_attempt_receipt,
    build_lookahead_receipt,
    canonical_research_digest,
    start_validation_attempt,
    validate_lookahead_receipt,
)
from app.canonical_v13.runtime_reader import read_frozen_research_bundle
from tests.test_canonical_v13_research_validation import (
    EXECUTOR_IMAGE_DIGEST,
    _prepare_ready_plan,
    canonical_connection,
)


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class CapturingRunner:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.argv: tuple[str, ...] | None = None
        self.timeout_seconds: int | None = None
        self.max_output_bytes: int | None = None

    def run(self, argv, *, timeout_seconds, max_output_bytes):
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        return SandboxCommandResult(
            return_code=0,
            stdout=json.dumps(self.payload, sort_keys=True).encode(),
            stderr=b"",
        )


def _running(connection):
    prepared = _prepare_ready_plan(connection)
    spec = build_ephemeral_launch_spec(
        connection,
        validation_plan_id=prepared.plan_id,
        expected_plan_digest=prepared.plan_digest,
        executor_identity="canonical-v13-freqtrade-worker-v1",
        executor_image_digest=EXECUTOR_IMAGE_DIGEST,
    )
    return prepared, start_validation_attempt(connection, launch_spec=spec)


def _output(running, metrics=None):
    values = metrics or {
        "required-a": {"trade_count": 2, "profit_factor": 2.0},
        "required-b": {"trade_count": 3, "profit_factor": 1.8},
    }
    by_key = {
        item.window_key: item for item in running.launch_spec.windows if item.required
    }
    return {
        "contract": "canonical-v13-freqtrade-backtest-output-v1",
        "validation_attempt_id": str(running.validation_attempt_id),
        "attempt_request_digest": running.request_digest,
        "status": "SUCCEEDED",
        "windows": [
            {
                "window_key": key,
                "window_member_digest": by_key[key].window_member_digest,
                "metrics": value,
            }
            for key, value in sorted(values.items())
        ],
    }


def test_production_adapter_mounts_only_digest_checked_read_only_inputs(
    canonical_connection, tmp_path: Path
) -> None:
    with canonical_connection.begin():
        _prepared, running = _running(canonical_connection)
        market = (
            canonical_connection.execute(select(MARKET_ARTIFACTS_TABLE))
            .mappings()
            .one()
        )

    market_root = tmp_path / "market-root"
    workspace_root = tmp_path / "workspace-root"
    market_root.mkdir()
    workspace_root.mkdir()
    market_path = market_root.joinpath(*market["locator"].split("/"))
    market_path.parent.mkdir(parents=True)
    market_path.write_bytes(b"isolated phase6 market fixture")
    runtime = tmp_path / "docker"
    runtime.write_text("#!/bin/sh\nexit 1\n")
    runtime.chmod(0o755)

    @contextmanager
    def inputs(attempt):
        with materialize_production_research_inputs(
            canonical_connection,
            running_attempt=attempt,
            market_artifact_root=market_root,
            workspace_root=workspace_root,
        ) as materialized:
            assert materialized.request_path.stat().st_mode & 0o222 == 0
            assert all(
                mount.source.parent == materialized.workspace
                and mount.source.stat().st_mode & 0o222 == 0
                for mount in materialized.mounts
            )
            yield materialized

    runner = CapturingRunner(_output(running))
    adapter = FreqtradeProductionResearchAdapter(
        activation=PRODUCTION_RESEARCH_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=runner,
    )
    receipt = adapter.execute(running)
    assert receipt.status == "SUCCEEDED"
    assert len(receipt.window_results) == 2
    assert runner.argv is not None
    assert "--rm" in runner.argv
    assert "--init" in runner.argv
    assert "--network" in runner.argv
    assert runner.argv[runner.argv.index("--network") + 1] == "none"
    assert "--read-only" in runner.argv
    assert "--cap-drop" in runner.argv
    assert "ALL" in runner.argv
    assert runner.argv[runner.argv.index("--security-opt") + 1] == ("no-new-privileges")
    assert runner.argv[runner.argv.index("--pids-limit") + 1] == "64"
    assert runner.argv[runner.argv.index("--cpus") + 1] == "1.0"
    assert runner.argv[runner.argv.index("--memory") + 1] == "1024m"
    assert runner.argv[runner.argv.index("--memory-swap") + 1] == "1024m"
    assert runner.argv[runner.argv.index("--tmpfs") + 1] == (
        "/work:rw,noexec,nosuid,nodev,size=128m"
    )
    assert runner.argv[runner.argv.index("--user") + 1] == (
        f"{os.getuid()}:{os.getgid()}"
    )
    assert runner.timeout_seconds == 60
    assert runner.max_output_bytes == 32_768
    assert not any(
        "DATABASE_URL" in value or "credential" in value for value in runner.argv
    )
    assert list(workspace_root.iterdir()) == []

    unsafe_runtime = tmp_path / "non-executable-runtime"
    unsafe_runtime.write_text("not executable")
    with pytest.raises(
        CanonicalProductionResearchBlocked,
        match="BLOCKED_SANDBOX_RUNTIME_PATH",
    ):
        FreqtradeProductionResearchAdapter(
            activation=PRODUCTION_RESEARCH_ACTIVATION,
            runtime_path=unsafe_runtime,
            image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
            limits=ProductionResearchLimits(
                timeout_seconds=60, max_output_bytes=32_768
            ),
            input_factory=inputs,
            runner=runner,
        )

    relocated_parent = market_path.parent.with_name("digest-checked-real-parent")
    market_path.parent.rename(relocated_parent)
    market_path.parent.symlink_to(relocated_parent, target_is_directory=True)
    symlink_runner = CapturingRunner(_output(running))
    symlink_blocked = FreqtradeProductionResearchAdapter(
        activation=PRODUCTION_RESEARCH_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=symlink_runner,
    ).execute(running)
    assert symlink_blocked.status == "BLOCKED"
    assert symlink_runner.argv is None
    market_path.parent.unlink()
    relocated_parent.rename(market_path.parent)

    bad_runner = CapturingRunner({**_output(running), "unexpected": True})
    blocked = FreqtradeProductionResearchAdapter(
        activation=PRODUCTION_RESEARCH_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=bad_runner,
    ).execute(running)
    assert blocked.status == "BLOCKED"
    assert blocked.window_results == ()


def test_scoring_entrypoint_has_no_qualification_capability() -> None:
    source = inspect.getsource(research_scoring)
    assert "qualify_target" not in source
    assert "persist_qualification_receipt" not in source


def test_production_adapter_rejects_root_outer_identity(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "docker"
    runtime.write_text("#!/bin/sh\nexit 1\n")
    runtime.chmod(0o755)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(
        CanonicalProductionResearchBlocked,
        match="BLOCKED_SANDBOX_ROOT_IDENTITY",
    ):
        FreqtradeProductionResearchAdapter(
            activation=PRODUCTION_RESEARCH_ACTIVATION,
            runtime_path=runtime,
            image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
            limits=ProductionResearchLimits(
                timeout_seconds=60, max_output_bytes=32_768
            ),
            input_factory=lambda _attempt: None,  # type: ignore[arg-type]
            runner=CapturingRunner({}),
        )


def test_production_lookahead_adapter_is_planless_and_uses_same_sandbox_flags(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "podman-remote"
    runtime.write_text("#!/bin/sh\nexit 1\n")
    runtime.chmod(0o755)
    workspace = tmp_path / "gate-input"
    workspace.mkdir()
    request_digest = "a" * 64
    (workspace / "lookahead-request.json").write_text(
        json.dumps(
            {
                "request_digest": request_digest,
                "windows": [
                    {
                        "window_key": "required-a",
                        "window_member_digest": "d" * 64,
                        "minimum_closed_candles": 10,
                    }
                ],
            }
        )
    )
    lineage = ResearchLineage(
        strategy_version_id=uuid4(),
        research_target_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="b" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="c" * 64,
    )
    evidence = {
        "contract": "canonical-v13-freqtrade-lookahead-output-v3",
        "request_digest": request_digest,
        "strategy_version_id": str(lineage.strategy_version_id),
        "research_target_id": str(lineage.research_target_id),
        "status": "PASSED",
        "has_bias": False,
        "observed_signal_count": 20,
        "blocked_observed_trade_count": None,
        "blocked_required_trade_count": None,
        "window_results": [
            {
                "window_key": "required-a",
                "window_member_digest": "d" * 64,
                "has_bias": False,
                "observed_signal_count": 20,
                "biased_entry_signal_count": 0,
                "biased_exit_signal_count": 0,
            }
        ],
        "failure_stage": None,
        "failure_code": None,
        "tool_return_code": 0,
        "stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "redacted_detail": None,
    }
    payload = {
        **evidence,
        "evidence_digest": canonical_research_digest(evidence),
    }

    @contextmanager
    def inputs(_lineage):
        yield ProductionLookaheadInputSet(
            workspace=workspace,
            request_path=workspace / "lookahead-request.json",
            mounts=(),
            input_manifest_digest="e" * 64,
        )

    runner = CapturingRunner(payload)
    adapter = FreqtradeProductionLookaheadAdapter(
        activation=PRODUCTION_LOOKAHEAD_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=runner,
    )
    receipt = adapter.execute(lineage=lineage, artifact_digest="f" * 64)
    assert receipt.status == "PASSED"
    assert receipt.has_bias is False
    assert receipt.observed_signal_count == 20
    assert receipt.failure_code is None
    assert runner.argv is not None
    assert "lookahead" in runner.argv
    assert "backtest" not in runner.argv
    assert "--network" in runner.argv
    assert runner.argv[runner.argv.index("--network") + 1] == "none"
    assert "--read-only" in runner.argv
    assert runner.argv[runner.argv.index("--cap-drop") + 1] == "ALL"
    assert "validation-plan.json" not in runner.argv

    incomplete_evidence = {**evidence, "window_results": []}
    incomplete_runner = CapturingRunner(
        {
            **incomplete_evidence,
            "evidence_digest": canonical_research_digest(incomplete_evidence),
        }
    )
    incomplete_adapter = FreqtradeProductionLookaheadAdapter(
        activation=PRODUCTION_LOOKAHEAD_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=incomplete_runner,
    )
    with pytest.raises(
        CanonicalProductionResearchBlocked,
        match="required window output is incomplete",
    ):
        incomplete_adapter.execute(lineage=lineage, artifact_digest="f" * 64)

    blocked_evidence = {
        **evidence,
        "status": "BLOCKED",
        "has_bias": None,
        "observed_signal_count": 0,
        "blocked_observed_trade_count": 3,
        "blocked_required_trade_count": 10,
        "window_results": [],
        "failure_stage": "OUTPUT_INTERPRETATION",
        "failure_code": "LOOKAHEAD_INSUFFICIENT_TRADES",
        "tool_return_code": 0,
        "stdout_digest": "f" * 64,
        "stderr_digest": "1" * 64,
        "redacted_detail": "Freqtrade observed fewer trades than required",
    }
    blocked_adapter = FreqtradeProductionLookaheadAdapter(
        activation=PRODUCTION_LOOKAHEAD_ACTIVATION,
        runtime_path=runtime,
        image_reference=f"freqtrade-ai/research@sha256:{EXECUTOR_IMAGE_DIGEST}",
        limits=ProductionResearchLimits(timeout_seconds=60, max_output_bytes=32_768),
        input_factory=inputs,
        runner=CapturingRunner(
            {
                **blocked_evidence,
                "evidence_digest": canonical_research_digest(blocked_evidence),
            }
        ),
    )
    blocked = blocked_adapter.execute(lineage=lineage, artifact_digest="f" * 64)
    assert blocked.status == "BLOCKED"
    assert blocked.failure_code == "LOOKAHEAD_INSUFFICIENT_TRADES"
    assert blocked.blocked_observed_trade_count == 3
    assert blocked.blocked_required_trade_count == 10


def test_blocked_lookahead_diagnostic_is_machine_readable_and_ineligible(
    monkeypatch, tmp_path: Path
) -> None:
    lineage = ResearchLineage(
        strategy_version_id=uuid4(),
        research_target_id=uuid4(),
        configuration_bundle_id=uuid4(),
        configuration_bundle_digest="a" * 64,
        market_snapshot_id=uuid4(),
        market_snapshot_digest="b" * 64,
    )
    lookahead = build_lookahead_receipt(
        lineage=lineage,
        artifact_digest="c" * 64,
        analyzer_identity="production-freqtrade-lookahead-v1",
        analyzer_digest="d" * 64,
        evidence_digest="e" * 64,
        status="BLOCKED",
        has_bias=None,
        observed_signal_count=0,
        failure_stage="OUTPUT_INTERPRETATION",
        failure_code="LOOKAHEAD_INSUFFICIENT_TRADES",
        tool_return_code=0,
        stdout_digest="f" * 64,
        stderr_digest="1" * 64,
        redacted_detail="Freqtrade observed fewer trades than required",
        blocked_observed_trade_count=3,
        blocked_required_trade_count=10,
    )
    decision = validate_lookahead_receipt(
        lookahead,
        expected_lineage=lineage,
        expected_artifact_digest="c" * 64,
    )
    assert decision.status == "BLOCKED"
    assert decision.reason_codes == (
        "LOOKAHEAD_INSUFFICIENT_TRADES",
        "LOOKAHEAD_EVIDENCE_BLOCKED",
        "LOOKAHEAD_OBSERVATIONS_UNSET",
    )
    static = StaticValidationReceipt(
        strategy_version_id=lineage.strategy_version_id,
        artifact_digest="c" * 64,
        validator_identity="canonical-v13-static-validator-v1",
        validator_digest="2" * 64,
        status="PASSED",
        findings=(),
        request_digest="3" * 64,
        receipt_digest="4" * 64,
    )
    monkeypatch.setattr(
        "app.canonical_v13.freqtrade_production.validate_production_static_gate",
        lambda *_args, **_kwargs: static,
    )
    adapter = SimpleNamespace(execute=lambda **_kwargs: lookahead)
    gate = execute_production_static_lookahead_gate(
        object(), lineage=lineage, adapter=adapter
    )
    assert gate.status == "LOOKAHEAD_BLOCKED"
    assert gate.validation_eligible is False


def test_dynamic_single_target_cap_schedules_31_as_five_sixes_and_one(
    canonical_connection,
) -> None:
    with canonical_connection.begin():
        prepared = _prepare_ready_plan(canonical_connection)
        frozen = read_frozen_research_bundle(
            canonical_connection,
            configuration_bundle_id=prepared.lineage.configuration_bundle_id,
            expected_bundle_digest=prepared.lineage.configuration_bundle_digest,
        )
    allocation = replace(frozen.allocations[0], candidate_cap=6)
    frozen = replace(frozen, allocations=(allocation,))
    candidates = tuple(uuid4() for _ in range(31))
    batches = plan_serial_research_batches(
        frozen,
        candidates_by_target={frozen.targets[0].research_target_id: candidates},
    )
    assert [len(item.strategy_version_ids) for item in batches] == [6, 6, 6, 6, 6, 1]
    assert all(item.per_target_cap == 6 for item in batches)
    assert [item.batch_number for item in batches] == [1, 2, 3, 4, 5, 6]


def test_production_chain_uses_consumed_exact_attempt_then_separate_receipts() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as raw:
        install_canonical_genesis(raw, installer_identity="production-chain-test")
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        prepared = _prepare_ready_plan(connection)
        attempt_id = uuid4()
        authorization = authorize_research_execution(
            connection,
            lineage=prepared.lineage,
            attempt_id=attempt_id,
            validation_plan_id=prepared.plan_id,
            validation_plan_digest=prepared.plan_digest,
            actor_identity="production-control-authority",
            purpose="ONE_NO_TRADE_RESEARCH_ATTEMPT",
            authorized_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            environment_class="PRODUCTION_RESEARCH",
        )
        consumption = consume_research_execution_authorization(
            connection,
            authorization_id=authorization.authorization_id,
            expected_lineage=prepared.lineage,
            validation_plan_id=prepared.plan_id,
            validation_plan_digest=prepared.plan_digest,
            attempt_id=attempt_id,
            actor_identity="production-orchestrator",
            consumed_at=NOW + timedelta(seconds=1),
        )
        spec = build_ephemeral_launch_spec(
            connection,
            validation_plan_id=prepared.plan_id,
            expected_plan_digest=prepared.plan_digest,
            executor_identity="production-freqtrade-worker",
            executor_image_digest=EXECUTOR_IMAGE_DIGEST,
        )
        running = start_consumed_research_attempt(
            connection,
            launch_spec=spec,
            authorization_consumption=consumption,
        )

    @contextmanager
    def factory():
        with engine.connect() as connection:
            yield connection

    class Executor:
        environment_class = "PRODUCTION_RESEARCH"
        network_mode = "none"
        credential_mounts = ()
        exchange_capabilities = ()
        order_capabilities = ()
        writer_capabilities = ()

        def execute(self, attempt):
            return build_ephemeral_attempt_receipt(
                attempt,
                metrics_by_window_key={
                    "required-a": {"trade_count": 2, "profit_factor": 2.0},
                    "required-b": {"trade_count": 3, "profit_factor": 1.8},
                },
            )

    result = execute_production_research_chain(
        audit_connection_factory=factory,
        validation_connection_factory=factory,
        scoring_connection_factory=factory,
        qualification_connection_factory=factory,
        validation_attempt_id=running.validation_attempt_id,
        expected_plan_digest=prepared.plan_digest,
        authorization_consumption=consumption,
        executor=Executor(),
        scorer_identity="production-target-scorer",
        qualifier_identity="production-target-qualifier",
    )
    assert result.attempt_status == "SUCCEEDED"
    assert result.scoring_receipt is not None
    assert result.qualification_receipt is not None
    assert result.qualification_receipt.status == "QUALIFIED"
    with engine.connect() as raw:
        projection = read_research_chain_projection(
            raw.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            ),
            validation_plan_id=prepared.plan_id,
        )
    assert projection.plan_status == "COMPLETE"
    assert projection.attempt_status == "SUCCEEDED"
    assert projection.qualification_status == "QUALIFIED"
    engine.dispose()
