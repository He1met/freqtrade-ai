from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.freqtrade.backtest_runner import (
    _market_data_files_digest,
    _matching_market_data_files,
)
from app.core.strategy_research_matrix import RESEARCH_TARGETS, ResearchTarget
from app.models import ResearchJob
from app.services.qualified_demo_deployment_queue import (
    OWNERSHIP_SCHEMA,
    validate_formal_research_ownership,
)
from app.services.strategy_blueprint_equivalence import (
    prove_blueprint_code_equivalence,
)
from app.services.strategy_candidate_validation_queue import (
    CANDIDATE_VALIDATION_OPERATION,
    GeneratedCandidate,
    StrategyCandidateValidationQueueBlocked,
    StrategyCandidateValidationQueueService,
)


EXPECTED_CANDIDATE_COUNT = 60
OWNERSHIP_RELATIVE_PATH = Path(
    ".freqtrade-ai/research/formal-strategy-research-ownership.json"
)
LOCK_RELATIVE_PATH = Path(".freqtrade-ai/research/bihourly-strategy-research.lock")
MARKET_DATA_MAX_CLOSE_AGE = timedelta(minutes=20)
SOURCE_RECEIPT_MAX_AGE = timedelta(hours=2)
REQUIRED_HISTORY_START = datetime(2023, 7, 1, tzinfo=timezone.utc)


class BihourlyStrategyResearchBlocked(RuntimeError):
    """The generation-only automation cannot safely persist a complete batch."""


@dataclass(frozen=True)
class BihourlyStrategyResearchResult:
    status: str
    reason_code: str
    run_id: str
    persisted_count: int
    runtime_or_writer_touched: bool = False
    backtest_started: bool = False
    deployment_started: bool = False


@dataclass(frozen=True)
class _CandidateSource:
    path: Path
    relative_path: str
    digest: str
    class_name: str
    timeframe: str
    blueprint_evidence: dict[str, Any]


class BihourlyStrategyResearchService:
    """Persist one two-hour 3x2x10 batch; never validate or deploy it."""

    def __init__(self, db: Session, *, canonical_root: Path, datadir: Path) -> None:
        self.db = db
        self.canonical_root = canonical_root.resolve()
        self.datadir = datadir.resolve()

    def run_generation_only(
        self,
        *,
        run_id: str,
        repository_commit: str,
        owner_task_id: str,
        refresh: Callable[[], None],
        now: datetime | None = None,
    ) -> BihourlyStrategyResearchResult:
        current = _as_utc(now or datetime.now(timezone.utc))
        with self._exclusive_lock() as locked:
            if not locked:
                return self._no_op(run_id, "RESEARCH_GENERATION_LOCK_HELD")
            existing = self._run_jobs(run_id)
            if existing:
                if len(existing) == EXPECTED_CANDIDATE_COUNT:
                    return self._no_op(run_id, "RESEARCH_BATCH_ALREADY_PERSISTED")
                raise BihourlyStrategyResearchBlocked(
                    "RESEARCH_BATCH_PARTIAL_OR_CONFLICTING"
                )
            previous = self._read_ownership()
            if self._owned_by_other(previous, owner_task_id=owner_task_id, now=current):
                return self._no_op(run_id, "RESEARCH_OWNED_BY_ANOTHER_EXECUTOR")

            refresh()
            refreshed_at = (
                current
                if now is not None
                else _as_utc(datetime.now(timezone.utc))
            )
            market = self._validate_market_data(now=refreshed_at)
            ownership = self._acquire_ownership(
                owner_task_id=owner_task_id,
                now=refreshed_at,
            )
            candidates = tuple(self._candidates(market))
            if len(candidates) != EXPECTED_CANDIDATE_COUNT:
                raise BihourlyStrategyResearchBlocked(
                    "RESEARCH_CANDIDATE_CARDINALITY_INVALID"
                )
            try:
                queued = StrategyCandidateValidationQueueService(
                    self.db,
                    canonical_root=self.canonical_root,
                ).enqueue_generated(
                    run_id=run_id,
                    repository_commit=repository_commit,
                    candidates=candidates,
                    ownership_evidence=ownership,
                    now=refreshed_at,
                    ownership_guard=lambda: self._ownership_matches(ownership),
                )
            except StrategyCandidateValidationQueueBlocked as exc:
                if str(exc) == "RESEARCH_OWNERSHIP_LOST_BEFORE_QUEUE_COMMIT":
                    return self._no_op(run_id, str(exc))
                self._expire_own_ownership(ownership, now=refreshed_at)
                raise
            except BaseException:
                self._expire_own_ownership(ownership, now=refreshed_at)
                raise
            if len(queued) != EXPECTED_CANDIDATE_COUNT:
                raise BihourlyStrategyResearchBlocked(
                    "RESEARCH_QUEUE_COMMIT_CARDINALITY_INVALID"
                )
            return BihourlyStrategyResearchResult(
                status="GENERATED",
                reason_code="CANDIDATES_PERSISTED_AWAITING_SERIAL_VALIDATION",
                run_id=run_id,
                persisted_count=len(queued),
            )

    def _run_jobs(self, run_id: str) -> tuple[ResearchJob, ...]:
        return tuple(
            self.db.scalars(
                select(ResearchJob)
                .where(
                    ResearchJob.operation == CANDIDATE_VALIDATION_OPERATION,
                    ResearchJob.request_payload["research_run_id"].as_string()
                    == run_id,
                )
                .order_by(ResearchJob.id)
            ).all()
        )

    def _validate_market_data(self, *, now: datetime) -> dict[str, dict[str, Any]]:
        evidence: dict[str, dict[str, Any]] = {}
        for target in RESEARCH_TARGETS:
            path = target.market_path(self.datadir)
            sidecar_path = path.with_suffix(path.suffix + ".source.json")
            if not path.is_file() or not sidecar_path.is_file():
                raise BihourlyStrategyResearchBlocked("MARKET_DATA_SET_INCOMPLETE")
            sidecar = _read_object(sidecar_path)
            downloaded_at = _parse_time(sidecar.get("downloaded_at"))
            digest = _sha256(path)
            if (
                sidecar.get("credentials_used") is not False
                or sidecar.get("account_endpoint_used") is not False
                or sidecar.get("orders_submitted") is not False
                or sidecar.get("timeframe") != target.timeframe
                or sidecar.get("data_file_sha256") != digest
                or downloaded_at is None
                or downloaded_at > now + timedelta(minutes=1)
                or now - downloaded_at > SOURCE_RECEIPT_MAX_AGE
            ):
                raise BihourlyStrategyResearchBlocked(
                    "MARKET_DATA_SOURCE_RECEIPT_INVALID_OR_STALE"
                )
            first_open_at, last_open_at = _data_bounds(path)
            last_close_at = last_open_at + _timeframe_duration(target.timeframe)
            if (
                first_open_at > REQUIRED_HISTORY_START
                or last_open_at > now
                or last_close_at > now + timedelta(minutes=1)
                or now - last_close_at > MARKET_DATA_MAX_CLOSE_AGE
            ):
                raise BihourlyStrategyResearchBlocked("MARKET_DATA_NOT_FRESH")
            lineage = _matching_market_data_files(
                self.datadir,
                target.pair,
                target.timeframe,
            )
            lineage_digest = _market_data_files_digest(lineage)
            if not lineage or not re.fullmatch(r"[0-9a-f]{64}", lineage_digest or ""):
                raise BihourlyStrategyResearchBlocked(
                    "MARKET_DATA_LINEAGE_DIGEST_INVALID"
                )
            evidence[target.key] = {
                "pair": target.pair,
                "timeframe": target.timeframe,
                "path": str(path),
                "file_sha256": digest,
                "validation_lineage_digest": lineage_digest,
                "last_open_at": last_open_at.isoformat(),
                "last_close_at": last_close_at.isoformat(),
                "first_open_at": first_open_at.isoformat(),
                "source_receipt_path": str(sidecar_path),
                "source_receipt_sha256": _sha256(sidecar_path),
                "sidecar": sidecar,
            }
        if len(evidence) != 6:
            raise BihourlyStrategyResearchBlocked("MARKET_DATA_SET_INCOMPLETE")
        for pair in {target.pair for target in RESEARCH_TARGETS}:
            five = evidence[f"{pair}|5m"]
            fifteen = evidence[f"{pair}|15m"]
            if (
                five["sidecar"].get("source_type") != "OKX_PUBLIC_REST"
                or fifteen["sidecar"].get("source_type")
                != "DERIVED_FROM_OKX_PUBLIC_REST"
                or fifteen["sidecar"].get("parent_five_minute_sha256")
                != five["file_sha256"]
            ):
                raise BihourlyStrategyResearchBlocked(
                    "MARKET_DATA_TIMEFRAME_BINDING_INVALID"
                )
        for item in evidence.values():
            item.pop("sidecar", None)
        return evidence

    def _candidates(
        self,
        market: dict[str, dict[str, Any]],
    ) -> Iterator[GeneratedCandidate]:
        for source in _discover_sources(self.canonical_root):
            for target in RESEARCH_TARGETS:
                if target.timeframe != source.timeframe:
                    continue
                pair_slug = re.sub(r"[^A-Za-z0-9]+", "_", target.pair).strip("_")
                candidate_key = f"{source.class_name}__{pair_slug}_{target.timeframe}"
                yield GeneratedCandidate(
                    candidate_key=candidate_key,
                    source_path=source.relative_path,
                    source_code_digest=source.digest,
                    pair=target.pair,
                    timeframe=target.timeframe,
                    blueprint_evidence=source.blueprint_evidence,
                    market_data_evidence=market[target.key],
                    validation_request=_validation_request(
                        source=source,
                        target=target,
                        market_digest=market[target.key]["validation_lineage_digest"],
                    ),
                )

    def _read_ownership(self) -> dict[str, Any] | None:
        path = self.canonical_root / OWNERSHIP_RELATIVE_PATH
        return _read_object(path) if path.is_file() else None

    def _owned_by_other(
        self,
        evidence: dict[str, Any] | None,
        *,
        owner_task_id: str,
        now: datetime,
    ) -> bool:
        try:
            validate_formal_research_ownership(
                evidence,
                canonical_root=self.canonical_root,
                now=now,
            )
        except RuntimeError:
            return False
        return evidence.get("owner_task_id") != owner_task_id

    def _acquire_ownership(
        self,
        *,
        owner_task_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        if not owner_task_id.strip():
            raise BihourlyStrategyResearchBlocked("RESEARCH_OWNER_ID_INVALID")
        payload = {
            "schema_version": OWNERSHIP_SCHEMA,
            "scope": "FORMAL_STRATEGY_RESEARCH",
            "canonical_root": str(self.canonical_root),
            "owner_task_id": owner_task_id,
            "lease_nonce": uuid4().hex,
            "confirmed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat(),
        }
        _write_object_atomic(self.canonical_root / OWNERSHIP_RELATIVE_PATH, payload)
        validate_formal_research_ownership(
            payload,
            canonical_root=self.canonical_root,
            now=now,
        )
        return payload

    def _ownership_matches(self, expected: dict[str, Any]) -> bool:
        actual = self._read_ownership()
        return (
            isinstance(actual, dict)
            and actual.get("lease_nonce") == expected.get("lease_nonce")
            and actual.get("owner_task_id") == expected.get("owner_task_id")
            and actual.get("expires_at") == expected.get("expires_at")
        )

    def _expire_own_ownership(
        self,
        expected: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        if not self._ownership_matches(expected):
            return
        _write_object_atomic(
            self.canonical_root / OWNERSHIP_RELATIVE_PATH,
            {**expected, "expires_at": now.isoformat(), "status": "FAILED"},
        )

    def _exclusive_lock(self) -> Iterator[bool]:
        class _LockContext:
            def __init__(inner, path: Path) -> None:
                inner.path = path
                inner.handle = None
                inner.locked = False

            def __enter__(inner) -> bool:
                inner.path.parent.mkdir(parents=True, exist_ok=True)
                inner.handle = inner.path.open("a+")
                try:
                    fcntl.flock(inner.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    inner.locked = True
                except BlockingIOError:
                    inner.locked = False
                return inner.locked

            def __exit__(inner, *_args: object) -> None:
                if inner.handle is not None:
                    if inner.locked:
                        fcntl.flock(inner.handle.fileno(), fcntl.LOCK_UN)
                    inner.handle.close()

        return _LockContext(self.canonical_root / LOCK_RELATIVE_PATH)

    @staticmethod
    def _no_op(run_id: str, reason: str) -> BihourlyStrategyResearchResult:
        return BihourlyStrategyResearchResult(
            status="NO_OP",
            reason_code=reason,
            run_id=run_id,
            persisted_count=0,
        )


def _discover_sources(root: Path) -> tuple[_CandidateSource, ...]:
    candidate_root = root / "research/strategy_candidates"
    paths = sorted(candidate_root.glob("*/[0-9][0-9]_*.py"))
    if len(paths) != 20:
        raise BihourlyStrategyResearchBlocked("CANDIDATE_SOURCE_SET_INVALID")
    sources: list[_CandidateSource] = []
    for path in paths:
        source = path.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        blueprint_path = path.with_suffix(".blueprint.json")
        if not blueprint_path.is_file():
            raise BihourlyStrategyResearchBlocked("CANDIDATE_BLUEPRINT_MISSING")
        blueprint = _read_object(blueprint_path)
        class_name = blueprint.get("class_name")
        timeframe = blueprint.get("timeframe")
        if not isinstance(class_name, str) or timeframe != path.parent.name:
            raise BihourlyStrategyResearchBlocked("CANDIDATE_BLUEPRINT_INVALID")
        equivalence = prove_blueprint_code_equivalence(
            blueprint_payload=blueprint,
            source_bytes=source,
            expected_source_digest=digest,
            expected_class_name=class_name,
            expected_timeframe=timeframe,
        )
        sources.append(
            _CandidateSource(
                path=path,
                relative_path=str(path.relative_to(root)),
                digest=digest,
                class_name=class_name,
                timeframe=timeframe,
                blueprint_evidence={
                    "contract_version": "formal-candidate-blueprint-evidence-v1",
                    "blueprint": equivalence.blueprint.model_dump(mode="json"),
                    "blueprint_digest": equivalence.blueprint_digest,
                    "renderer_version": equivalence.renderer_version,
                    "rendered_code_digest": equivalence.rendered_code_digest,
                    "source_code_digest": digest,
                    "exact_render_match": True,
                },
            )
        )
    return tuple(sources)


def _validation_request(
    *,
    source: _CandidateSource,
    target: ResearchTarget,
    market_digest: str,
) -> dict[str, Any]:
    overrides = {
        "SOL/USDT:USDT": {
            "primary_bear": "20230801-20231001",
            "wf_range": "20240301-20240501",
        }
    }.get(target.pair, {})
    primary = overrides.get("primary_bear", "20230701-20231001")
    windows = (
        ("WALK_FORWARD", "bull", "20231001-20240301"),
        ("WALK_FORWARD", "range", overrides.get("wf_range", "20240301-20240629")),
        ("OOS", None, "20250101-20251001"),
        ("WALK_FORWARD", "bear", "20251001-20260201"),
    )
    return {
        "prompt_summary": f"Validate persisted formal candidate {source.class_name}",
        "allow_real_call": False,
        "persisted_blueprint": source.blueprint_evidence["blueprint"],
        "formal_provenance": {
            "contract_version": "formal-candidate-validation-provenance-v1",
            "execution_target_id": "OKX_DEMO",
            "allow_real_funds": False,
            "real_orders": False,
            "provider_call_attempted": False,
            "credential_values_recorded": False,
        },
        "backtest_profile": {
            "schema_version": "2",
            "profile_name": f"formal-{source.class_name}-{target.pair}-{target.timeframe}",
            "pair": target.pair,
            "timeframe": target.timeframe,
            "timerange": primary,
            "strategy": {
                "name": source.class_name,
                "path": str(source.path.parent),
            },
            "stake": {
                "currency": "USDT",
                "amount": 100.0,
                "tradable_balance_ratio": 0.99,
                "max_open_trades": 1,
            },
            "data_source": {
                "kind": "local",
                "exchange": "okx",
                "datadir": "user_data/data/okx",
                "data_format": "feather",
                "trading_mode": "futures",
                "margin_mode": "isolated",
            },
            "safety": {
                "allow_download": False,
                "allow_exchange_connection": False,
                "allow_dry_run": False,
                "allow_live_trading": False,
                "allow_hyperopt": False,
            },
            "tags": ["formal-research", "serial-validation", "okx-demo-only"],
        },
        "validation_windows": [
            {
                "window_kind": kind,
                "timerange": timerange,
                "expected_market_data_digest": market_digest,
                **({"market_state": state} if state is not None else {}),
            }
            for kind, state, timerange in windows
        ],
    }


def _data_bounds(path: Path) -> tuple[datetime, datetime]:
    import pandas as pd

    frame = pd.read_feather(path, columns=["date"])
    if frame.empty:
        raise BihourlyStrategyResearchBlocked("MARKET_DATA_SET_INCOMPLETE")
    dates = pd.to_datetime(frame["date"], utc=True)
    expected_seconds = 900 if "-15m-" in path.name else 300
    diffs = dates.diff().dropna().dt.total_seconds()
    if (
        dates.duplicated().any()
        or not dates.is_monotonic_increasing
        or (diffs != expected_seconds).any()
    ):
        raise BihourlyStrategyResearchBlocked("MARKET_DATA_INTERVAL_INCOMPLETE")
    return (
        _as_utc(pd.Timestamp(dates.iloc[0]).to_pydatetime()),
        _as_utc(pd.Timestamp(dates.iloc[-1]).to_pydatetime()),
    )


def _timeframe_duration(timeframe: str) -> timedelta:
    durations = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}
    try:
        return durations[timeframe]
    except KeyError as exc:
        raise BihourlyStrategyResearchBlocked(
            "MARKET_DATA_TIMEFRAME_UNSUPPORTED"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BihourlyStrategyResearchBlocked("RESEARCH_EVIDENCE_INVALID") from exc
    if not isinstance(value, dict):
        raise BihourlyStrategyResearchBlocked("RESEARCH_EVIDENCE_INVALID")
    return value


def _write_object_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
