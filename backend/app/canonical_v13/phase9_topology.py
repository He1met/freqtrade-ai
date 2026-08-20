"""Fail-closed service topology contract for canonical V1.3 Phase 9.

The contract is intentionally declarative.  It does not start a process, resolve a
credential reference, connect to an exchange, or grant a PostgreSQL role.  Operators
and future composition roots can validate their launch plans against this module
before any side effect is authorized.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Final, Mapping


class CanonicalPhase9TopologyBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Phase9ServiceSpec:
    service_key: str
    process_identity: str
    postgres_capability: str | None
    lifecycle: str
    launch_agent_label: str
    keep_alive: bool
    network_policy: str
    filesystem_mode: str
    credential_scope: str
    signal_writer_capability: bool
    order_writer_capability: bool
    demo_only: bool = True
    allow_real_funds: bool = False


_PHASE9_SERVICE_SPECS = {
    "control_activation": Phase9ServiceSpec(
        service_key="control_activation",
        process_identity="canonical-v13-control-activation-v1",
        postgres_capability="canonical_control_writer",
        lifecycle="ONE_SHOT_CONTROL",
        launch_agent_label="ai.freqtrade.canonical-v13.control-activation",
        keep_alive=False,
        network_policy="NONE",
        filesystem_mode="READ_ONLY",
        credential_scope="NONE",
        signal_writer_capability=False,
        order_writer_capability=False,
    ),
    "ephemeral_research": Phase9ServiceSpec(
        service_key="ephemeral_research",
        process_identity="canonical-v13-ephemeral-research-v1",
        postgres_capability=None,
        lifecycle="EPHEMERAL_RESEARCH_EXECUTOR",
        launch_agent_label="ai.freqtrade.canonical-v13.ephemeral-research",
        keep_alive=False,
        network_policy="NONE",
        filesystem_mode="EPHEMERAL",
        credential_scope="NONE",
        signal_writer_capability=False,
        order_writer_capability=False,
    ),
    "long_lived_runtime": Phase9ServiceSpec(
        service_key="long_lived_runtime",
        process_identity="canonical-v13-long-lived-runtime-v1",
        postgres_capability="canonical_runtime_reader",
        lifecycle="LONG_LIVED_TRADING_RUNTIME",
        launch_agent_label="ai.freqtrade.canonical-v13.runtime",
        keep_alive=True,
        network_policy="DEMO_EXCHANGE_ONLY",
        filesystem_mode="READ_ONLY",
        credential_scope="OKX_DEMO_READ_SIGNAL_ONLY",
        signal_writer_capability=True,
        order_writer_capability=False,
    ),
    "order_writer": Phase9ServiceSpec(
        service_key="order_writer",
        process_identity="canonical-v13-order-writer-v1",
        postgres_capability="canonical_order_writer",
        lifecycle="LONG_LIVED_ORDER_WRITER",
        launch_agent_label="ai.freqtrade.canonical-v13.order-writer",
        keep_alive=True,
        network_policy="DEMO_EXCHANGE_ONLY",
        filesystem_mode="READ_ONLY",
        credential_scope="OKX_DEMO_ORDER_ONLY",
        signal_writer_capability=False,
        order_writer_capability=True,
    ),
}
PHASE9_SERVICE_SPECS: Final[Mapping[str, Phase9ServiceSpec]] = MappingProxyType(
    _PHASE9_SERVICE_SPECS
)


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def phase9_topology_digest(
    specs: Mapping[str, Phase9ServiceSpec] = PHASE9_SERVICE_SPECS,
) -> str:
    validate_phase9_topology(specs)
    return _digest(
        {
            "contract": "canonical-v13-phase9-service-topology-v1",
            "services": [asdict(specs[key]) for key in sorted(specs)],
        }
    )


def validate_phase9_topology(
    specs: Mapping[str, Phase9ServiceSpec] = PHASE9_SERVICE_SPECS,
) -> None:
    expected = {
        "control_activation",
        "ephemeral_research",
        "long_lived_runtime",
        "order_writer",
    }
    if set(specs) != expected:
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_PHASE9_TOPOLOGY_KEYS",
            f"expected={sorted(expected)!r} observed={sorted(specs)!r}",
        )
    values = tuple(specs[key] for key in sorted(specs))
    if any(spec.service_key != key for key, spec in specs.items()):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_PHASE9_SERVICE_KEY_DRIFT", "mapping key and service key differ"
        )
    for field, observed in (
        ("process_identity", [spec.process_identity for spec in values]),
        ("launch_agent_label", [spec.launch_agent_label for spec in values]),
    ):
        if len(set(observed)) != len(observed):
            raise CanonicalPhase9TopologyBlocked(
                "BLOCKED_PHASE9_IDENTITY_REUSE", f"{field} must be unique"
            )
    database_capabilities = [
        spec.postgres_capability
        for spec in values
        if spec.postgres_capability is not None
    ]
    if len(set(database_capabilities)) != len(database_capabilities):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_PHASE9_DATABASE_IDENTITY_REUSE",
            "control, runtime, and writer database identities must differ",
        )
    if any(not spec.demo_only or spec.allow_real_funds for spec in values):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_PHASE9_REAL_FUNDS_CAPABILITY",
            "every Phase 9 service must remain Demo-only",
        )

    activation = specs["control_activation"]
    research = specs["ephemeral_research"]
    runtime = specs["long_lived_runtime"]
    writer = specs["order_writer"]
    if (
        activation.lifecycle != "ONE_SHOT_CONTROL"
        or activation.keep_alive
        or activation.network_policy != "NONE"
        or activation.credential_scope != "NONE"
        or activation.signal_writer_capability
        or activation.order_writer_capability
    ):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_CONTROL_ACTIVATION_LIFECYCLE_DRIFT",
            "activation must be one-shot, networkless, and execution-writerless",
        )
    if (
        research.lifecycle != "EPHEMERAL_RESEARCH_EXECUTOR"
        or research.keep_alive
        or research.network_policy != "NONE"
        or research.filesystem_mode != "EPHEMERAL"
        or research.credential_scope != "NONE"
        or research.postgres_capability is not None
        or research.signal_writer_capability
        or research.order_writer_capability
    ):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_RESEARCH_EXECUTOR_LIFECYCLE_DRIFT",
            "research must remain ephemeral, network-none, credentialless, and writerless",
        )
    if (
        runtime.lifecycle != "LONG_LIVED_TRADING_RUNTIME"
        or not runtime.keep_alive
        or runtime.network_policy != "DEMO_EXCHANGE_ONLY"
        or runtime.filesystem_mode != "READ_ONLY"
        or runtime.postgres_capability != "canonical_runtime_reader"
        or not runtime.signal_writer_capability
        or runtime.order_writer_capability
    ):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_LONG_LIVED_RUNTIME_CAPABILITY_DRIFT",
            "runtime must be long-lived, read-only, signal-only, and non-order-writing",
        )
    if (
        writer.lifecycle != "LONG_LIVED_ORDER_WRITER"
        or not writer.keep_alive
        or writer.network_policy != "DEMO_EXCHANGE_ONLY"
        or writer.filesystem_mode != "READ_ONLY"
        or writer.postgres_capability != "canonical_order_writer"
        or writer.signal_writer_capability
        or not writer.order_writer_capability
    ):
        raise CanonicalPhase9TopologyBlocked(
            "BLOCKED_ORDER_WRITER_CAPABILITY_DRIFT",
            "order writer must be the sole isolated Demo order capability",
        )


validate_phase9_topology()


__all__ = [
    "PHASE9_SERVICE_SPECS",
    "CanonicalPhase9TopologyBlocked",
    "Phase9ServiceSpec",
    "phase9_topology_digest",
    "validate_phase9_topology",
]
