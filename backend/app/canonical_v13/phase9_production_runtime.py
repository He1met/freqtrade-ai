"""Production composition for the public-only Phase 9 signal runtime.

The runtime reader and signal writer use distinct injected connection factories.
Market acquisition is public-only and injected.  No OKX account credential or
order transport is reachable from this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
from typing import Protocol

from sqlalchemy import select

from app.canonical_v13.market_acquisition import MarketAcquisitionRequest
from app.canonical_v13.models import (
    CONFIGURATION_BUNDLES_TABLE,
    DEPLOYMENT_APPROVALS_TABLE,
    DEPLOYMENTS_TABLE,
    QUALIFICATION_DECISIONS_TABLE,
    RESEARCH_TARGETS_TABLE,
    RUNTIME_INSTANCES_TABLE,
    RUNTIME_RECEIPTS_TABLE,
    STRATEGY_ARTIFACTS_TABLE,
    STRATEGY_VERSIONS_TABLE,
)
from app.canonical_v13.phase9_production_composition import (
    CanonicalPhase9CompositionBlocked,
    ConnectionFactory,
)
from app.canonical_v13.phase9_runtime_supervisor import Phase9LaunchPlan
from app.canonical_v13.phase9_runtime_worker import (
    ActiveRuntimeLineage,
    CanonicalPhase9RuntimeWorker,
    NaturalMarketEvidence,
    NaturalSignalEvaluation,
    RuntimeWorkerReceipt,
    natural_market_evidence_digest,
    verify_runtime_worker_receipt,
)
from app.canonical_v13.signal_service import record_production_demo_signal


class PublicDownloaderPort(Protocol):
    provenance_class: str
    network_access: str
    credential_access: str

    def acquire(self, request: MarketAcquisitionRequest): ...


class DatabaseRuntimeLineageReader:
    """Load exact qualified ACTIVE/HEALTHY lineage with runtime-reader identity."""

    def __init__(self, factory: ConnectionFactory, plan: Phase9LaunchPlan) -> None:
        self._factory = factory
        self._plan = plan

    def read_active_runtime_lineage(self) -> ActiveRuntimeLineage:
        if self._plan.deployment_id is None:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RUNTIME_DEPLOYMENT", "plan deployment is unset"
            )
        with self._factory() as connection:
            deployment = connection.execute(
                select(DEPLOYMENTS_TABLE).where(
                    DEPLOYMENTS_TABLE.c.id == self._plan.deployment_id
                )
            ).mappings().one_or_none()
            runtime = connection.execute(
                select(RUNTIME_INSTANCES_TABLE).where(
                    RUNTIME_INSTANCES_TABLE.c.deployment_id == self._plan.deployment_id
                )
            ).mappings().one_or_none()
            receipt = (
                connection.execute(
                    select(RUNTIME_RECEIPTS_TABLE)
                    .where(RUNTIME_RECEIPTS_TABLE.c.runtime_instance_id == runtime["id"])
                    .order_by(RUNTIME_RECEIPTS_TABLE.c.observed_at.desc())
                    .limit(1)
                ).mappings().one_or_none()
                if runtime is not None
                else None
            )
            approval = (
                connection.execute(
                    select(DEPLOYMENT_APPROVALS_TABLE).where(
                        DEPLOYMENT_APPROVALS_TABLE.c.id
                        == deployment["deployment_approval_id"]
                    )
                ).mappings().one_or_none()
                if deployment is not None
                else None
            )
            qualification = (
                connection.execute(
                    select(QUALIFICATION_DECISIONS_TABLE).where(
                        QUALIFICATION_DECISIONS_TABLE.c.id
                        == approval["qualification_decision_id"]
                    )
                ).mappings().one_or_none()
                if approval is not None
                else None
            )
            version = (
                connection.execute(
                    select(STRATEGY_VERSIONS_TABLE).where(
                        STRATEGY_VERSIONS_TABLE.c.id == deployment["strategy_version_id"]
                    )
                ).mappings().one_or_none()
                if deployment is not None
                else None
            )
            artifact = (
                connection.execute(
                    select(STRATEGY_ARTIFACTS_TABLE).where(
                        STRATEGY_ARTIFACTS_TABLE.c.id == version["artifact_id"]
                    )
                ).mappings().one_or_none()
                if version is not None
                else None
            )
            target = (
                connection.execute(
                    select(RESEARCH_TARGETS_TABLE).where(
                        RESEARCH_TARGETS_TABLE.c.id == qualification["research_target_id"]
                    )
                ).mappings().one_or_none()
                if qualification is not None
                else None
            )
            bundle = (
                connection.execute(
                    select(CONFIGURATION_BUNDLES_TABLE).where(
                        CONFIGURATION_BUNDLES_TABLE.c.id
                        == deployment["configuration_bundle_id"]
                    )
                ).mappings().one_or_none()
                if deployment is not None
                else None
            )
        source = artifact["normalized_content"] if artifact is not None else None
        source_digest = (
            sha256(str(source).encode("utf-8")).hexdigest()
            if isinstance(source, str)
            else None
        )
        exact = bool(
            deployment is not None
            and deployment["status"] == "ACTIVE"
            and deployment["demo_only"] is True
            and deployment["allow_real_funds"] is False
            and deployment["capability_digest"]
            == self._plan.deployment_capability_digest
            and runtime is not None
            and runtime["status"] == "HEALTHY"
            and runtime["runtime_identity"] == self._plan.process_identity
            and runtime["service_account"] == "canonical_runtime_reader"
            and runtime["order_writer_capability"] is False
            and receipt is not None
            and receipt["status"] == "HEALTHY"
            and receipt["evidence_class"] == "PRODUCTION_DEMO_RUNTIME"
            and receipt["launch_spec_digest"] == runtime["launch_spec_digest"]
            and receipt["capability_digest"] == deployment["capability_digest"]
            and approval is not None
            and approval["status"] == "APPROVED"
            and approval["strategy_version_id"] == deployment["strategy_version_id"]
            and qualification is not None
            and qualification["status"] == "QUALIFIED"
            and qualification["id"] == approval["qualification_decision_id"]
            and qualification["strategy_version_id"] == deployment["strategy_version_id"]
            and qualification["configuration_bundle_id"]
            == deployment["configuration_bundle_id"]
            and qualification["configuration_bundle_digest"]
            == deployment["configuration_bundle_digest"]
            and qualification["market_snapshot_id"] == deployment["market_snapshot_id"]
            and qualification["market_snapshot_digest"]
            == deployment["market_snapshot_digest"]
            and version is not None
            and artifact is not None
            and isinstance(artifact["encoding"], str)
            and artifact["encoding"].casefold() == "utf-8"
            and artifact["content_digest"] == source_digest
            and target is not None
            and target["instrument"] == "BTC-USDT-SWAP"
            and target["pair"] == "BTC/USDT:USDT"
            and target["timeframe"] == "15m"
            and target["data_kind"] == "futures"
            and bundle is not None
            and bundle["bundle_digest"] == deployment["configuration_bundle_digest"]
            and bundle["market_snapshot_id"] == deployment["market_snapshot_id"]
            and bundle["market_snapshot_digest"] == deployment["market_snapshot_digest"]
        )
        if not exact:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_RUNTIME_EXACT_LINEAGE",
                "ACTIVE runtime, QUALIFIED artifact, bundle, or target drifted",
            )
        return ActiveRuntimeLineage(
            qualification_decision_id=qualification["id"],
            qualification_decision_digest=qualification["decision_digest"],
            deployment_approval_id=approval["id"],
            deployment_approval_digest=approval["approval_digest"],
            deployment_id=deployment["id"],
            runtime_instance_id=runtime["id"],
            strategy_version_id=version["id"],
            research_target_id=target["id"],
            configuration_bundle_id=bundle["id"],
            configuration_bundle_digest=bundle["bundle_digest"],
            market_snapshot_id=deployment["market_snapshot_id"],
            market_snapshot_digest=deployment["market_snapshot_digest"],
            deployment_capability_digest=deployment["capability_digest"],
            runtime_launch_spec_digest=runtime["launch_spec_digest"],
            runtime_receipt_digest=receipt["receipt_digest"],
            runtime_receipt_observed_at=receipt["observed_at"],
            runtime_identity=runtime["runtime_identity"],
            runtime_service_account=runtime["service_account"],
            deployment_status=deployment["status"],
            runtime_status=runtime["status"],
            runtime_evidence_class=receipt["evidence_class"],
            demo_only=deployment["demo_only"],
            allow_real_funds=deployment["allow_real_funds"],
            runtime_order_writer_capability=runtime["order_writer_capability"],
            strategy_artifact_digest=artifact["content_digest"],
            strategy_artifact_source=source,
            target_instrument=target["instrument"],
            target_pair=target["pair"],
            target_timeframe=target["timeframe"],
            target_data_kind=target["data_kind"],
        )


class PublicOkxRuntimeMarketEvidence:
    def __init__(self, downloader: PublicDownloaderPort, *, candle_count: int = 1):
        if (
            downloader.credential_access != "NONE"
            or downloader.network_access != "PUBLIC_MARKET_DATA_ONLY"
            or candle_count != 1
        ):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_PUBLIC_MARKET_PORT", "public credential-free port required"
            )
        self._downloader = downloader
        self._candle_count = candle_count

    def read_market_evidence(self, *, lineage, observed_at: datetime):
        now = observed_at.astimezone(timezone.utc)
        interval = timedelta(minutes=15)
        end = datetime.fromtimestamp(
            int(now.timestamp() // interval.total_seconds())
            * int(interval.total_seconds()),
            tz=timezone.utc,
        )
        payload = self._downloader.acquire(
            MarketAcquisitionRequest(
                source_identity="okx-public-history-candles-v1",
                target_key="canonical-v13-btc-usdt-swap-15m",
                instrument=lineage.target_instrument or "",
                pair=lineage.target_pair or "",
                timeframe=lineage.target_timeframe or "",
                data_kind=lineage.target_data_kind or "",
                requested_start=end - self._candle_count * interval,
                requested_end=end,
            )
        )
        if payload.observed_closed_candles != self._candle_count:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_PUBLIC_MARKET_COUNT", "closed candle count drifted"
            )
        try:
            candles = [json.loads(line) for line in payload.content.splitlines()]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_PUBLIC_MARKET_JSON", "candle payload is invalid"
            ) from exc
        evidence = NaturalMarketEvidence(
            evidence_id=payload.locator,
            evidence_digest="0" * 64,
            instrument="BTC-USDT-SWAP",
            observed_at=now,
            payload={
                "source": "okx-public-history-candles-v1",
                "credential_access": "NONE",
                "content_digest": sha256(payload.content).hexdigest(),
                "closed_candles": candles,
                "window_end": end.isoformat(),
            },
        )
        return replace(evidence, evidence_digest=natural_market_evidence_digest(evidence))


class FrozenIntradayLeverageEvaluator:
    """Evaluate only the exact qualified 03:00 UTC long-entry artifact."""

    ARTIFACT_DIGEST = "d5682ed00a4755afabd612ef86404c0663152104525010fcc2268c08d4659ac7"

    _REQUIRED_SOURCE = (
        "class CanonicalIntradayLeverageBaseline",
        'timeframe = "15m"',
        "startup_candle_count = 1",
        "return min(14.0, max_leverage)",
        '(dataframe["date"].dt.hour == 3)',
        '(dataframe["date"].dt.minute == 0)',
        '(dataframe["volume"] > 0)',
        'dataframe.loc[daily_entry, "enter_long"] = 1',
    )

    def evaluate_natural_signal(self, *, lineage, evidence):
        source = lineage.strategy_artifact_source
        if (
            not isinstance(source, str)
            or sha256(source.encode("utf-8")).hexdigest()
            != lineage.strategy_artifact_digest
            or lineage.strategy_artifact_digest != self.ARTIFACT_DIGEST
            or any(token not in source for token in self._REQUIRED_SOURCE)
            or "populate_entry_trend" not in source
            or "enter_short" in source
        ):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_EVALUATOR_ARTIFACT",
                "qualified artifact is not the frozen long-only baseline",
            )
        candles = evidence.payload.get("closed_candles")
        if not isinstance(candles, list) or len(candles) != 1:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_EVALUATOR_WINDOW", "one exact closed candle is required"
            )
        try:
            candle = candles[0]
            volume = Decimal(str(candle["volume"]))
            opened_at = datetime.fromisoformat(
                str(candle["opened_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_EVALUATOR_CANDLE", "candle shape is invalid"
            ) from exc
        signal = bool(opened_at.hour == 3 and opened_at.minute == 0 and volume > 0)
        return NaturalSignalEvaluation(
            outcome="SIGNAL" if signal else "NO_ACTION",
            evaluated_at=evidence.observed_at,
            evaluator_identity="canonical-intraday-leverage-baseline-v1",
            evaluation_payload={
                "direction": "LONG",
                "closed_candle": True,
                "candle_opened_at": opened_at.isoformat(),
                "volume": str(volume),
                "effective_strategy_leverage": "14",
                "artifact_digest": lineage.strategy_artifact_digest,
            },
        )


class ReleaseBoundReceiptSeal:
    algorithm = "HMAC_SHA256_V1"

    def __init__(self, release_digest: str, signing_key: str) -> None:
        if len(signing_key) < 48:
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_SIGNAL_SIGNER", "dedicated signing key is invalid"
            )
        self._release_digest = release_digest
        self._key = signing_key.encode("utf-8")
        key_digest = sha256(self._key).hexdigest()
        self.key_id = f"canonical-signal-receipt-v1:{key_digest}"

    def sign_digest(self, digest: str) -> str:
        return hmac.new(
            self._key,
            f"{self._release_digest}:{digest}".encode("ascii"),
            sha256,
        ).hexdigest()

    def verify_digest(self, *, key_id, algorithm, digest, signature) -> bool:
        return (
            key_id == self.key_id
            and algorithm == self.algorithm
            and signature == self.sign_digest(digest)
        )


class DatabaseSignalReceiptWriter:
    """Verify the sealed receipt before invoking the signal-writer-only service."""

    def __init__(self, factory: ConnectionFactory, verifier: ReleaseBoundReceiptSeal):
        self._factory = factory
        self._verifier = verifier

    def persist(self, receipt: RuntimeWorkerReceipt) -> None:
        if not verify_runtime_worker_receipt(receipt, verifier=self._verifier):
            raise CanonicalPhase9CompositionBlocked(
                "BLOCKED_PHASE9_SIGNAL_RECEIPT", "runtime receipt verification failed"
            )
        candidate = receipt.signal_candidate
        if candidate is None:
            return
        with self._factory() as connection:
            record_production_demo_signal(
                connection,
                deployment_id=candidate.deployment_id,
                runtime_instance_id=candidate.runtime_instance_id,
                research_target_id=candidate.research_target_id,
                signal_json=candidate.signal_json,
                evaluated_at=candidate.evaluated_at,
            )


class PersistingRuntimeWorker:
    def __init__(self, worker, writer: DatabaseSignalReceiptWriter):
        self._worker = worker
        self._writer = writer

    def heartbeat(self, *, stage: str, plan_digest: str, observed_at: datetime):
        receipt = self._worker.heartbeat(
            stage=stage, plan_digest=plan_digest, observed_at=observed_at
        )
        self._writer.persist(receipt)
        return receipt


class ProductionRuntimeWorkerFactory:
    def __init__(
        self,
        *,
        runtime_connection_factory: ConnectionFactory,
        signal_connection_factory: ConnectionFactory,
        downloader: PublicDownloaderPort,
        signing_key: str,
    ) -> None:
        self._runtime_factory = runtime_connection_factory
        self._signal_factory = signal_connection_factory
        self._downloader = downloader
        self._signing_key = signing_key

    def build(self, plan: Phase9LaunchPlan) -> PersistingRuntimeWorker:
        seal = ReleaseBoundReceiptSeal(plan.release_digest, self._signing_key)
        worker = CanonicalPhase9RuntimeWorker(
            lineage_reader=DatabaseRuntimeLineageReader(self._runtime_factory, plan),
            market_evidence=PublicOkxRuntimeMarketEvidence(self._downloader),
            evaluator=FrozenIntradayLeverageEvaluator(),
            signer=seal,
            maximum_evidence_age=timedelta(minutes=2),
        )
        return PersistingRuntimeWorker(
            worker, DatabaseSignalReceiptWriter(self._signal_factory, seal)
        )


__all__ = [
    "DatabaseRuntimeLineageReader",
    "DatabaseSignalReceiptWriter",
    "FrozenIntradayLeverageEvaluator",
    "PersistingRuntimeWorker",
    "ProductionRuntimeWorkerFactory",
    "PublicOkxRuntimeMarketEvidence",
    "ReleaseBoundReceiptSeal",
]
