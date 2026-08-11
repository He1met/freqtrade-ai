#!/usr/bin/env python3
"""Run the natural-signal safety contract against an isolated PostgreSQL cluster.

The default mode creates a disposable local PostgreSQL cluster.  The suite writes
synthetic contract fixtures only inside that cluster and never imports exchange
credentials, calls an exchange, or authorizes order submission.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Iterator
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SAFE_EXTERNAL_DATABASE = re.compile(r"^freqtrade_ai_.*(?:preflight|test).*$")


@dataclass(frozen=True)
class ContractCheck:
    category: str
    nodeids: tuple[str, ...]
    proves: str


CONTRACT_CHECKS = (
    ContractCheck(
        "SCHEMA_ACL_SECURITY_DEFINER",
        (
            "tests/test_risk_chain_postgresql.py::test_20260727_02_upgrades_to_risk_chain_atomically",
            "tests/test_risk_chain_postgresql.py::test_attestation_verifier_detects_role_acl_and_body_tampering",
            "tests/test_risk_chain_postgresql.py::test_database_rejects_direct_authorization_tampering",
            "tests/test_risk_chain_postgresql.py::test_runtime_role_cannot_directly_insert_attestation_rows",
            "tests/test_risk_chain_postgresql.py::test_postgresql_v43_upgrades_budget_initializer_and_acl_idempotently",
            "tests/test_risk_chain_postgresql.py::test_postgresql_v44_upgrades_private_stale_release_idempotently",
            "tests/test_risk_chain_postgresql.py::test_runtime_cannot_call_or_bypass_private_stale_approval_sweep",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_v39_natural_risk_function_is_execute_only_and_fail_closed",
        ),
        "real migrations, role ACLs, private SECURITY DEFINER entrypoint, and runtime DML denial",
    ),
    ContractCheck(
        "SNAPSHOT_SIGNAL_BINDING",
        (
            "tests/test_okx_demo_read_adapter.py::test_signal_bundle_attests_local_as_of_when_exchange_clock_is_ahead",
            "tests/test_risk_chain.py::test_market_snapshot_future_binding_is_blocked_without_permission",
            "tests/test_risk_chain_postgresql.py::test_postgresql_accepts_local_market_observation_with_ahead_exchange_events",
            "tests/test_risk_chain_postgresql.py::test_security_definer_rejects_wrong_pinned_account",
            "tests/test_risk_chain_postgresql.py::test_revoked_or_expired_attested_session_blocks_authorization",
        ),
        "instrument/market/account binding, attested account identity, expiry, and revocation",
    ),
    ContractCheck(
        "RECEIPT_LINEAGE_DEPLOYMENT_POLICY",
        (
            "tests/test_risk_chain_postgresql.py::test_postgresql_v44_owner_initializes_missing_natural_budget_once",
            "tests/test_risk_chain_postgresql.py::test_postgresql_v44_reinstalls_repository_datetime_digest_contract",
            "tests/test_okx_demo_execution_orchestrator.py::test_actionable_signal_completes_signal_then_risk_and_evaluation",
            "tests/test_strategy_deployment_repository.py::test_actionable_evaluation_opens_one_fenced_execution_chain",
        ),
        "ACTIONABLE receipt, immutable signal digest, execution lineage, active deployment, and policy digest",
    ),
    ContractCheck(
        "DEMO_READINESS_RECONCILIATION_GUARD",
        (
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_writer_blocks_incomplete_full_chain_risk_checkpoint",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_continuous_guard_blocks_deployment_set_digest_drift",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_continuous_guard_acl_tamper_fails_readiness",
            "tests/test_okx_demo_order_writer.py::test_set_leverage_recovery_selects_exact_side_from_dual_side_snapshot",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_runtime_get_only_recovers_dual_side_leverage_attempt",
        ),
        "Demo-only execution, completed risk checkpoint, current reconciliation, GET-only writer recovery, and automation guard digests",
    ),
    ContractCheck(
        "ACTIONABLE_CLAIM_EXECUTION_HANDOFF",
        (
            "tests/test_okx_demo_reconciliation_runtime.py::test_runtime_actionable_evaluation_dispatches_exact_approval_in_same_cycle",
            "tests/test_okx_demo_reconciliation_runtime.py::test_runtime_actionable_without_exact_approval_binding_fails_closed",
            "tests/test_okx_demo_reconciliation_runtime.py::test_runtime_actionable_blocked_opening_is_explicit_and_never_places",
            "tests/test_okx_demo_reconciliation_runtime.py::test_runtime_actionable_exact_approval_unavailable_fails_closed",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_runtime_dispatches_fresh_actionable_in_same_cycle_without_exchange",
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_runtime_role_completes_real_writer_happy_lifecycle",
        ),
        "same-cycle exact approval handoff, runtime-role claim, Demo authorization, guarded writer preparation, and zero exchange access",
    ),
    ContractCheck(
        "WRITER_LEASE_FENCING",
        (
            "tests/test_okx_demo_writer_postgresql.py::test_postgresql_concurrent_lease_has_one_winner",
            "tests/test_strategy_deployment_repository.py::test_expired_lease_recovers_with_new_fence_and_rejects_stale_owner",
            "tests/test_okx_demo_execution_orchestrator.py::test_signal_checkpoint_survives_expiry_and_new_fence_without_recapture",
        ),
        "unique writer ownership, evaluation lease fencing, and immutable checkpoint recovery",
    ),
    ContractCheck(
        "RISK_BUDGET_DECISION_IDEMPOTENCY",
        (
            "tests/test_risk_chain_postgresql.py::test_postgresql_budget_lock_allows_only_one_concurrent_permission",
            "tests/test_risk_chain_postgresql.py::test_postgresql_concurrent_idempotent_retry_reads_one_chain",
            "tests/test_risk_chain_postgresql.py::test_owner_sweep_releases_only_unclaimed_expired_natural_approval",
            "tests/test_risk_chain_postgresql.py::test_owner_sweep_preserves_unproven_or_started_execution",
            "tests/test_risk_chain_postgresql.py::test_owner_sweep_is_concurrent_and_idempotent",
            "tests/test_risk_chain_postgresql.py::test_owner_sweep_fails_closed_on_budget_mismatch",
            "tests/test_risk_chain_postgresql.py::test_natural_risk_boundary_invokes_private_stale_approval_sweep",
            "tests/test_risk_chain.py::test_approved_chain_is_deterministic_idempotent_and_never_submits",
            "tests/test_okx_demo_execution_orchestrator.py::test_actionable_completion_failure_blocks_already_risked_chain",
        ),
        "atomic budget reservation and stale release, risk decision, deterministic replay, fail-closed completion, and zero orders",
    ),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the zero-order natural-chain contract in disposable PostgreSQL. "
            "No canonical runtime or exchange is contacted."
        )
    )
    parser.add_argument(
        "--database-url",
        help="URL for a disposable external PostgreSQL cluster; default starts a local temporary cluster",
    )
    parser.add_argument(
        "--external-isolated-cluster",
        action="store_true",
        help="attest that --database-url points to a disposable non-canonical PostgreSQL cluster",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--keep-junit", type=Path)
    return parser


def _database_name(url: str) -> str:
    normalized = url.replace("postgresql+psycopg://", "postgresql://", 1)
    return urlsplit(normalized).path.lstrip("/").split("?", 1)[0]


def validate_external_url(url: str, *, isolated: bool) -> None:
    name = _database_name(url)
    if not isolated:
        raise ValueError(
            "external PostgreSQL requires --external-isolated-cluster; "
            "never point this preflight at canonical runtime storage"
        )
    if name == "freqtrade_ai" or not SAFE_EXTERNAL_DATABASE.fullmatch(name):
        raise ValueError(
            "external database name must be disposable and contain preflight or test; "
            f"refused {name or '<missing>'!r}"
        )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class EphemeralPostgres:
    def __init__(self, parent: Path):
        self.parent = parent
        self.data = parent / "data"
        self.log = parent / "postgres.log"
        self.port = _free_port()
        self.initdb = shutil.which("initdb")
        self.pg_ctl = shutil.which("pg_ctl")

    def __enter__(self) -> str:
        if not self.initdb or not self.pg_ctl:
            raise RuntimeError(
                "initdb and pg_ctl are required for default disposable-cluster mode; "
                "install PostgreSQL or pass an explicitly isolated --database-url"
            )
        self.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                self.initdb,
                "--pgdata",
                str(self.data),
                "--username=postgres",
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                self.pg_ctl,
                "--pgdata",
                str(self.data),
                "--log",
                str(self.log),
                "--options",
                f"-F -h 127.0.0.1 -p {self.port}",
                "--wait",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        import psycopg

        admin_url = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "CREATE ROLE freqtrade LOGIN NOSUPERUSER NOCREATEROLE "
                "NOCREATEDB NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            connection.execute("CREATE DATABASE freqtrade_ai_natural_preflight")
        return (
            "postgresql+psycopg://postgres@127.0.0.1:"
            f"{self.port}/freqtrade_ai_natural_preflight"
        )

    def __exit__(self, *_exc: object) -> None:
        if self.pg_ctl and self.data.exists():
            subprocess.run(
                [self.pg_ctl, "--pgdata", str(self.data), "--wait", "--mode=fast", "stop"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _all_nodeids() -> Iterator[str]:
    seen: set[str] = set()
    for check in CONTRACT_CHECKS:
        for nodeid in check.nodeids:
            if nodeid not in seen:
                seen.add(nodeid)
                yield nodeid


def _run_suite(database_url: str, junit: Path) -> int:
    environment = dict(os.environ)
    for key in tuple(environment):
        if (
            key == "DATABASE_URL"
            or key.startswith("POSTGRES_")
            or key.startswith("OKX_")
            or key.startswith("FREQTRADE_AI_OKX_DEMO_")
        ):
            environment.pop(key)
    environment.update(
        {
            "POSTGRES_WORKER_URL": database_url,
            "FREQTRADE_AI_CI_OFFLINE": "1",
            "FREQTRADE_AI_REAL_ORDERS": "false",
            "PYTHONPATH": ".",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml",
        str(junit),
        *_all_nodeids(),
    ]
    return subprocess.run(command, cwd=BACKEND, env=environment, check=False).returncode


def _test_outcomes(junit: Path) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    if not junit.exists():
        return outcomes
    for case in ET.parse(junit).getroot().iter("testcase"):
        name = case.attrib.get("name", "").split("[", 1)[0]
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "FAILED"
        elif case.find("skipped") is not None:
            outcome = "SKIPPED"
        else:
            outcome = "PASSED"
        previous = outcomes.get(name)
        if previous == "FAILED" or outcome == "FAILED":
            outcomes[name] = "FAILED"
        elif previous == "SKIPPED" or outcome == "SKIPPED":
            outcomes[name] = "SKIPPED"
        else:
            outcomes[name] = "PASSED"
    return outcomes


def _report(returncode: int, junit: Path, mode: str) -> dict[str, object]:
    outcomes = _test_outcomes(junit)
    categories = []
    for check in CONTRACT_CHECKS:
        tests = {
            nodeid.rsplit("::", 1)[-1]: outcomes.get(nodeid.rsplit("::", 1)[-1], "NOT_RUN")
            for nodeid in check.nodeids
        }
        status = "PASSED" if tests and set(tests.values()) == {"PASSED"} else "FAILED"
        categories.append(
            {
                "category": check.category,
                "status": status,
                "proves": check.proves,
                "tests": tests,
            }
        )
    passed = returncode == 0 and all(
        category["status"] == "PASSED" for category in categories
    )
    return {
        "schema_version": "okx-demo-natural-chain-preflight-v1",
        "status": "PASSED" if passed else "FAILED",
        "mode": mode,
        "order_submission": "DISABLED",
        "exchange_access": "NONE",
        "canonical_runtime_access": "NONE",
        "failure_class": None if passed else "CONTRACT_GATE_FAILED",
        "categories": categories,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.database_url:
            validate_external_url(
                args.database_url, isolated=args.external_isolated_cluster
            )
        elif args.external_isolated_cluster:
            raise ValueError("--external-isolated-cluster requires --database-url")
    except ValueError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="freqtrade-ai-natural-preflight-") as temporary:
        temporary_path = Path(temporary)
        junit = temporary_path / "natural-chain-preflight.xml"
        try:
            if args.database_url:
                returncode = _run_suite(args.database_url, junit)
                mode = "EXTERNAL_DISPOSABLE_CLUSTER"
            else:
                with EphemeralPostgres(temporary_path / "postgres") as database_url:
                    returncode = _run_suite(database_url, junit)
                mode = "LOCAL_EPHEMERAL_CLUSTER"
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            report = {
                "schema_version": "okx-demo-natural-chain-preflight-v1",
                "status": "BLOCKED",
                "failure_class": "PREFLIGHT_INFRASTRUCTURE_BLOCKED",
                "reason": str(error),
                "order_submission": "DISABLED",
                "exchange_access": "NONE",
                "canonical_runtime_access": "NONE",
            }
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
        report = _report(returncode, junit, mode)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        if args.keep_junit:
            args.keep_junit.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(junit, args.keep_junit)
        return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
