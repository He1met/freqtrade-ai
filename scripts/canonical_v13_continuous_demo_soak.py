#!/usr/bin/env python3
"""Prepare, run, drain, and inspect one 24h canonical OKX_DEMO soak."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.canonical_v13.continuous_demo_soak import ContinuousDemoSoakOperator  # noqa: E402
from app.canonical_v13.execution_common import CanonicalExecutionChainBlocked  # noqa: E402
from app.canonical_v13.phase9_production_composition import (  # noqa: E402
    CanonicalPhase9CompositionBlocked,
)
from app.canonical_v13.phase9_order_writer import CanonicalOrderRecoveryRequired  # noqa: E402
from app.canonical_v13.models import DEPLOYMENTS_TABLE  # noqa: E402
from sqlalchemy import select  # noqa: E402


CONTRACT = "canonical-v13-bounded-continuous-okx-demo-soak-v1"
SUPPORT_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "FreqtradeAiV13"
    / "continuous-demo-soak"
)
STATE_PATH = SUPPORT_ROOT / "state.json"
PLAN_PATH = SUPPORT_ROOT / "plan.json"
RECEIPTS_PATH = SUPPORT_ROOT / "receipts.jsonl"
LABEL = "com.freqtrade-ai.v13.continuous-demo-soak"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_ROOT = Path.home() / "Library" / "Logs" / "FreqtradeAiV13"
PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
SOAK_DURATION = timedelta(hours=24)
TICK_SECONDS = 15


class ContinuousDemoSoakServiceBlocked(RuntimeError):
    pass


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_SCRIPT_IMPORT")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase9():
    return _load("continuous_demo_phase9_boundary", REPO_ROOT / "scripts" / "canonical_v13_phase9_service.py")


def _api_service():
    return _load("continuous_demo_api_boundary", REPO_ROOT / "scripts" / "canonical_v13_api_service.py")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_json_safe(payload), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append(payload: dict[str, object]) -> None:
    SUPPORT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with RECEIPTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_STATE_UNSET") from exc
    if not isinstance(value, dict):
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_STATE_INVALID")
    return value


def _release() -> tuple[str, str]:
    phase9 = _phase9()
    digest = phase9._require_release_checkout()
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    sha = completed.stdout.strip()
    if completed.returncode != 0 or len(sha) != 40:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_RELEASE_SHA")
    return sha, digest


def _reader_factory(phase9):
    api = _api_service()
    url = api._database_url(api.READER_PRINCIPAL, api.READER_KEYCHAIN_SERVICE)
    return phase9._connection_factory(url)


def _operator():
    phase9 = _phase9()
    raw_holder = phase9._read_order_holder_token()
    holder_digest = sha256(
        json.dumps(
            {"holder_token": raw_holder},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return ContinuousDemoSoakOperator(
        reader_factory=_reader_factory(phase9),
        deployment_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_deployment_writer")
        ),
        approval_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_approval_writer")
        ),
        risk_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_risk_writer")
        ),
        order_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_order_writer")
        ),
        fill_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_fill_writer")
        ),
        ledger_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_ledger_writer")
        ),
        reconciliation_factory=phase9._connection_factory(
            phase9._phase9_database_url("canonical_reconciliation_writer")
        ),
        session_factory=phase9._production_okx_session_factory(),
        holder_token_digest=holder_digest,
    )


def _runtime_ready_for_openings() -> bool:
    observed = _phase9().status("long_lived_runtime")
    return bool(
        observed.get("status") == "RUNNING"
        and observed.get("loaded") is True
        and observed.get("lease_fresh") is True
        and observed.get("holder_alive") is True
        and observed.get("order_writer_enabled") is False
    )


def _active_deployment_id() -> object:
    phase9 = _phase9()
    with _reader_factory(phase9)() as connection:
        rows = connection.execute(
            select(DEPLOYMENTS_TABLE.c.id).where(
                DEPLOYMENTS_TABLE.c.status == "ACTIVE",
                DEPLOYMENTS_TABLE.c.demo_only.is_(True),
                DEPLOYMENTS_TABLE.c.allow_real_funds.is_(False),
            )
        ).scalars().all()
    if len(rows) != 1:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_ACTIVE_DEPLOYMENT")
    return rows[0]


def prepare() -> dict[str, object]:
    sha, release_digest = _release()
    now = _now()
    deployment_id = _active_deployment_id()
    existing = _read(PLAN_PATH) if PLAN_PATH.exists() else None
    if existing is not None and existing.get("status") in {"PREPARED", "RUNNING", "DRAINING"}:
        state = _read(STATE_PATH) if STATE_PATH.exists() else None
        if state is None or state.get("status") != "BLOCKED":
            if existing.get("release_sha") != sha or existing.get("deployment_id") != str(deployment_id):
                raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_EXISTING_PLAN_DRIFT")
            return {**existing, "repeat_noop": True}

        # A failed worker is not running, but launchd can retain its loaded
        # one-shot job.  Unload that exact job before replacing the failed
        # plan with a release-bound retry; the prior failure remains in the
        # append-only receipts log.
        target = f"gui/{os.getuid()}/{LABEL}"
        completed = subprocess.run(
            ["launchctl", "bootout", target], capture_output=True, check=False
        )
        if completed.returncode != 0:
            still_loaded = subprocess.run(
                ["launchctl", "print", target], capture_output=True, check=False
            ).returncode == 0
            if still_loaded:
                raise ContinuousDemoSoakServiceBlocked(
                    "BLOCKED_SOAK_FAILED_AGENT_LOADED"
                )
    plan = {
        "contract": CONTRACT,
        "status": "PREPARED",
        "release_sha": sha,
        "release_digest": release_digest,
        "deployment_id": str(deployment_id),
        "execution_target": "OKX_DEMO",
        "instrument": "BTC-USDT-SWAP",
        "signal_timeframe": "15m",
        "natural_signals_only": True,
        "single_position": True,
        "same_signal_dispatch_maximum": 1,
        "allow_real_funds": False,
        "prepared_at": now.isoformat(),
        "starts_at": None,
        "openings_end_at": None,
        "drain_until_flat": True,
    }
    _atomic(PLAN_PATH, plan)
    if existing is not None:
        _atomic(
            STATE_PATH,
            {
                **plan,
                "updated_at": now.isoformat(),
                "last_result": None,
            },
        )
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(PYTHON), str(Path(__file__).resolve()), "run"],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_ROOT / "continuous-demo-soak.stdout.log"),
        "StandardErrorPath": str(LOG_ROOT / "continuous-demo-soak.stderr.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "FREQTRADE_AI_DISABLE_ENV_FILE": "1",
        },
    }
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(plist, handle)
    os.chmod(PLIST_PATH, 0o600)
    return {**plan, "repeat_noop": False}


def confirm() -> dict[str, object]:
    plan = _read(PLAN_PATH)
    sha, release_digest = _release()
    if plan.get("release_sha") != sha or plan.get("release_digest") != release_digest:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_RELEASE_DRIFT")
    if plan.get("status") == "RUNNING":
        observed = status()
        if observed["launch_agent_loaded"] is not True:
            raise ContinuousDemoSoakServiceBlocked(
                "BLOCKED_SOAK_RUNNING_AGENT_UNLOADED"
            )
        return {**plan, "launch_agent_loaded": True, "repeat_noop": True}
    if plan.get("status") != "PREPARED":
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_PLAN_NOT_PREPARED")
    if not _runtime_ready_for_openings():
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_NATURAL_RUNTIME_NOT_READY")
    starts = _now()
    running = {
        **plan,
        "status": "RUNNING",
        "starts_at": starts.isoformat(),
        "openings_end_at": (starts + SOAK_DURATION).isoformat(),
        "last_result": None,
    }
    _atomic(PLAN_PATH, running)
    _atomic(STATE_PATH, {**running, "pid": None, "updated_at": starts.isoformat()})
    target = f"gui/{os.getuid()}/{LABEL}"
    subprocess.run(["launchctl", "bootout", target], capture_output=True, check=False)
    completed = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        failed = {
            **running,
            "status": "BLOCKED",
            "updated_at": _now().isoformat(),
            "reason_code": "BLOCKED_SOAK_LAUNCHD_START",
        }
        _atomic(STATE_PATH, failed)
        _append(failed)
        _atomic(PLAN_PATH, {**plan, "status": "PREPARED"})
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_LAUNCHD_START")
    return {**running, "launch_agent_loaded": True, "repeat_noop": False}


def tick(*, operator_factory=None) -> dict[str, object]:
    plan = _read(PLAN_PATH)
    if plan.get("status") not in {"RUNNING", "DRAINING"}:
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_PLAN_NOT_RUNNING")
    now = _now()
    end = datetime.fromisoformat(str(plan["openings_end_at"]).replace("Z", "+00:00"))
    openings_enabled = now < end
    if openings_enabled and not _runtime_ready_for_openings():
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_NATURAL_RUNTIME_NOT_READY")
    operator = (operator_factory or _operator)()
    result = operator.tick(openings_enabled=openings_enabled, evaluated_at=now)
    payload = {
        "contract": CONTRACT,
        "observed_at": now.isoformat(),
        "openings_enabled": openings_enabled,
        **_json_safe(result.__dict__),
    }
    _append(payload)
    state = {
        **plan,
        "status": (
            "BLOCKED"
            if result.status == "BLOCKED"
            else ("STOPPED" if result.status == "DRAINED" else ("RUNNING" if openings_enabled else "DRAINING"))
        ),
        "pid": os.getpid(),
        "updated_at": now.isoformat(),
        "last_result": payload,
    }
    _atomic(STATE_PATH, state)
    return state


def run() -> int:
    operator = None

    def process_operator():
        nonlocal operator
        if operator is None:
            operator = _operator()
        return operator

    while True:
        try:
            state = tick(operator_factory=process_operator)
        except (CanonicalExecutionChainBlocked, CanonicalPhase9CompositionBlocked, CanonicalOrderRecoveryRequired, ContinuousDemoSoakServiceBlocked) as exc:
            now = _now()
            blocked = {
                **(_read(PLAN_PATH) if PLAN_PATH.exists() else {}),
                "status": "BLOCKED",
                "pid": os.getpid(),
                "updated_at": now.isoformat(),
                "reason_code": getattr(exc, "code", str(exc).split(":", 1)[0]),
            }
            _atomic(STATE_PATH, blocked)
            _append(blocked)
            return 1
        except Exception as exc:
            now = _now()
            blocked = {
                **(_read(PLAN_PATH) if PLAN_PATH.exists() else {}),
                "status": "BLOCKED",
                "pid": os.getpid(),
                "updated_at": now.isoformat(),
                "reason_code": f"BLOCKED_SOAK_INTERNAL_{type(exc).__name__}",
            }
            _atomic(STATE_PATH, blocked)
            _append(blocked)
            return 1
        if state["status"] in {"STOPPED", "BLOCKED"}:
            if state["status"] == "STOPPED":
                try:
                    _phase9().stop("long_lived_runtime")
                except Exception as exc:
                    now = _now()
                    blocked = {
                        **state,
                        "status": "BLOCKED",
                        "pid": os.getpid(),
                        "updated_at": now.isoformat(),
                        "reason_code": getattr(
                            exc, "code", str(exc).split(":", 1)[0]
                        ),
                    }
                    _atomic(STATE_PATH, blocked)
                    _append(blocked)
                    return 1
            return 0 if state["status"] == "STOPPED" else 1
        time.sleep(TICK_SECONDS)


def status() -> dict[str, object]:
    plan = _read(PLAN_PATH) if PLAN_PATH.exists() else None
    state = _read(STATE_PATH) if STATE_PATH.exists() else None
    loaded = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        check=False,
    ).returncode == 0
    return {
        "contract": CONTRACT,
        "status": (state or plan or {}).get("status", "UNSET"),
        "plan": plan,
        "state": state,
        "launch_agent_loaded": loaded,
        "allow_real_funds": False,
    }


def stop() -> dict[str, object]:
    plan = _read(PLAN_PATH)
    now = _now()
    draining = {**plan, "status": "DRAINING", "openings_end_at": now.isoformat()}
    _atomic(PLAN_PATH, draining)
    return {**draining, "stop_mode": "DRAIN_UNTIL_FLAT"}


def finalize() -> dict[str, object]:
    state = _read(STATE_PATH)
    if state.get("status") != "STOPPED":
        raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_NOT_DRAINED")
    target = f"gui/{os.getuid()}/{LABEL}"
    completed = subprocess.run(
        ["launchctl", "bootout", target], capture_output=True, check=False
    )
    if completed.returncode != 0:
        still_loaded = subprocess.run(
            ["launchctl", "print", target], capture_output=True, check=False
        ).returncode == 0
        if still_loaded:
            raise ContinuousDemoSoakServiceBlocked("BLOCKED_SOAK_LAUNCHD_STOP")
    PLIST_PATH.unlink(missing_ok=True)
    plan = _read(PLAN_PATH)
    _atomic(PLAN_PATH, {**plan, "status": "STOPPED"})
    return {
        "contract": CONTRACT,
        "status": "STOPPED",
        "launch_agent_loaded": False,
        "allow_real_funds": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare", "confirm", "tick", "run", "status", "stop", "finalize"),
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            payload = prepare()
        elif args.command == "confirm":
            payload = confirm()
        elif args.command == "tick":
            payload = tick()
        elif args.command == "run":
            return run()
        elif args.command == "stop":
            payload = stop()
        elif args.command == "finalize":
            payload = finalize()
        else:
            payload = status()
    except Exception as exc:
        payload = {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "reason_code": getattr(exc, "code", str(exc).split(":", 1)[0]),
            "allow_real_funds": False,
        }
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(_json_safe(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
