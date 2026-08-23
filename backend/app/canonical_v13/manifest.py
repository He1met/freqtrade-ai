"""Machine-readable Strategy Platform V1.3 canonical table authority.

This module is intentionally dependency-free apart from the Python standard library.
It must remain usable by schema installers, static audits, API identity guards, and
offline acceptance tooling without importing any legacy ORM metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Final, Mapping


DESIGN_AUTHORITY_KEY: Final = "canonical-v13-phase0-20260814"
CANONICAL_DATABASE_PURPOSE: Final = "FREQTRADE_AI_V13_CANONICAL"
CANONICAL_BUSINESS_SCHEMA: Final = "strategy_platform_v13"
CANONICAL_GENESIS_VERSION: Final = "20260814_01"
CANONICAL_MANIFEST_KEY: Final = "canonical-v13-table-manifest-v1"
CANONICAL_LEGACY_IMPORT_MODE: Final = "EXTERNAL_LATEST_ONLY"
CANONICAL_TRADING_CAPABILITY: Final = "TRADING_DISABLED"
CANONICAL_PRODUCTION_DEFAULT: Final = "UNSET"
CANONICAL_AUTHORITY_REVISION: Final = "20260824_optimization_observability15"
CANONICAL_PREDECESSOR_RUNTIME_IDENTITY_INDEX: Final = (
    "uq_runtime_instances_runtime_identity"
)

P0_CONFIGURATION_KINDS: Final[tuple[str, ...]] = (
    "TARGET",
    "WINDOW",
    "GENERATION",
    "DIVERSITY",
    "QUALITY_QUALIFICATION",
    "SCORING",
    "RESEARCH_AGGREGATE",
)
INDEPENDENT_MARKET_PROFILE_KIND: Final = "MARKET_DATA"

WRITER_IDENTITIES: Final[tuple[str, ...]] = (
    "canonical_schema_owner",
    "canonical_control_writer",
    "canonical_validation_writer",
    "canonical_scoring_writer",
    "canonical_qualification_writer",
    "canonical_optimization_writer",
    "canonical_approval_writer",
    "canonical_deployment_writer",
    "canonical_signal_writer",
    "canonical_risk_writer",
    "canonical_order_writer",
    "canonical_fill_writer",
    "canonical_ledger_writer",
    "canonical_reconciliation_writer",
    "canonical_projection_writer",
)

READER_IDENTITIES: Final[tuple[str, ...]] = (
    "canonical_api_reader",
    "canonical_research_reader",
    "canonical_runtime_reader",
)

_DOMAIN_AUTHORITIES = {
    "schema_audit": "SCHEMA_IDENTITY_AND_APPEND_ONLY_AUDIT",
    "intake_catalog": "CONTROLLED_SUBMISSION_AND_CATALOG",
    "control_plane": "VALIDATED_SNAPSHOT_BUNDLE_AND_ACTIVE_POINTER",
    "market": "ACCEPTED_RECEIPT_AND_FROZEN_SNAPSHOT",
    "validation": "EXACT_PLAN_WINDOW_AND_ATTEMPT_EVIDENCE",
    "scoring_qualification": "SEPARATE_SCORER_AND_QUALIFIER_AUTHORITIES",
    "optimization": "CONTROLLED_POST_BASELINE_EXPERIMENT",
    "approval_deployment": "INDEPENDENT_HUMAN_AND_RUNTIME_GATES",
    "execution": "SIGNAL_RISK_ORDER_FILL_LEDGER_LINEAGE",
}
CANONICAL_DOMAIN_AUTHORITIES: Final[Mapping[str, str]] = MappingProxyType(
    _DOMAIN_AUTHORITIES
)

_TABLES_BY_DOMAIN = {
    "schema_audit": (
        "schema_metadata",
        "audit_events",
        "idempotency_receipts",
    ),
    "intake_catalog": (
        "strategy_artifacts",
        "strategy_submissions",
        "strategy_intake_receipts",
        "strategies",
        "strategy_versions",
    ),
    "control_plane": (
        "configuration_profiles",
        "configuration_versions",
        "configuration_dependencies",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "configuration_activations",
        "research_targets",
        "research_target_allocations",
    ),
    "market": (
        "market_profiles",
        "market_profile_versions",
        "market_artifacts",
        "market_inspections",
        "market_receipts",
        "market_snapshots",
        "market_snapshot_members",
    ),
    "validation": (
        "research_gate_attempts",
        "research_gate_receipts",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
    ),
    "scoring_qualification": (
        "target_scores",
        "qualification_decisions",
        "qualification_window_evidence",
    ),
    "optimization": (
        "optimization_runs",
        "optimization_trials",
    ),
    "approval_deployment": (
        "deployment_approvals",
        "deployments",
        "runtime_image_acceptances",
        "runtime_instances",
        "runtime_receipts",
    ),
    "execution": (
        "acceptance_signal_triggers",
        "execution_canary_probe_receipts",
        "execution_canary_risk_policies",
        "execution_risk_budget_authorizations",
        "execution_risk_reservations",
        "execution_attestations",
        "order_writer_leases",
        "signals",
        "trade_intents",
        "risk_decisions",
        "orders",
        "order_dispatch_receipts",
        "order_dispatch_outcome_receipts",
        "fills",
        "ledger_entries",
        "reconciliation_runs",
        "reconciliation_items",
    ),
}
CANONICAL_TABLES_BY_DOMAIN: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    _TABLES_BY_DOMAIN
)
CANONICAL_TABLE_NAMES: Final[tuple[str, ...]] = tuple(
    table_name
    for table_names in _TABLES_BY_DOMAIN.values()
    for table_name in table_names
)
CANONICAL_BUSINESS_TABLE_NAMES: Final[tuple[str, ...]] = tuple(
    table_name
    for table_name in CANONICAL_TABLE_NAMES
    if table_name != "schema_metadata"
)

_WRITER_TABLE_ALLOWLIST = {
    "canonical_schema_owner": ("schema_metadata",),
    "canonical_control_writer": (
        "audit_events",
        "acceptance_signal_triggers",
        "idempotency_receipts",
        "runtime_image_acceptances",
        *_TABLES_BY_DOMAIN["intake_catalog"],
        *_TABLES_BY_DOMAIN["control_plane"],
        *_TABLES_BY_DOMAIN["market"],
    ),
    "canonical_validation_writer": _TABLES_BY_DOMAIN["validation"],
    "canonical_scoring_writer": ("target_scores",),
    "canonical_qualification_writer": (
        "qualification_decisions",
        "qualification_window_evidence",
    ),
    "canonical_optimization_writer": _TABLES_BY_DOMAIN["optimization"],
    "canonical_approval_writer": (
        "deployment_approvals",
        "execution_canary_probe_receipts",
        "execution_canary_risk_policies",
        "execution_risk_budget_authorizations",
    ),
    "canonical_deployment_writer": (
        "deployments",
        "runtime_instances",
        "runtime_receipts",
        "execution_attestations",
    ),
    "canonical_signal_writer": ("signals",),
    "canonical_risk_writer": (
        "trade_intents",
        "risk_decisions",
        "execution_risk_reservations",
    ),
    "canonical_order_writer": (
        "orders",
        "order_dispatch_receipts",
        "order_dispatch_outcome_receipts",
        "order_writer_leases",
    ),
    "canonical_fill_writer": ("fills",),
    "canonical_ledger_writer": ("ledger_entries",),
    "canonical_reconciliation_writer": (
        "reconciliation_runs",
        "reconciliation_items",
    ),
    # Read projections are deliberately outside the business table manifest.
    "canonical_projection_writer": (),
}
WRITER_TABLE_ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    _WRITER_TABLE_ALLOWLIST
)

_RESEARCH_READER_TABLES = (
    "schema_metadata",
    "strategy_artifacts",
    "strategy_versions",
    "configuration_snapshots",
    "configuration_snapshot_members",
    "configuration_bundles",
    "configuration_bundle_members",
    "research_targets",
    "research_target_allocations",
    "market_artifacts",
    "market_inspections",
    "market_receipts",
    "market_snapshots",
    "market_snapshot_members",
    "research_gate_attempts",
    "research_gate_receipts",
    "validation_plans",
    "validation_plan_windows",
)
_RUNTIME_READER_TABLES = (
    "schema_metadata",
    "strategy_artifacts",
    "strategies",
    "strategy_versions",
    "configuration_snapshots",
    "configuration_snapshot_members",
    "configuration_bundles",
    "configuration_bundle_members",
    "research_targets",
    "research_target_allocations",
    "market_artifacts",
    "market_inspections",
    "market_receipts",
    "market_snapshots",
    "market_snapshot_members",
    "qualification_decisions",
    "deployment_approvals",
    "deployments",
    "runtime_image_acceptances",
    "runtime_instances",
    "runtime_receipts",
)
_READER_TABLE_ALLOWLIST = {
    # Phase 4 projections are rebuildable; this identity never receives DML.
    "canonical_api_reader": CANONICAL_TABLE_NAMES,
    "canonical_research_reader": _RESEARCH_READER_TABLES,
    "canonical_runtime_reader": _RUNTIME_READER_TABLES,
}
READER_TABLE_ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    _READER_TABLE_ALLOWLIST
)

# Writers may read only the immutable/upstream rows required to validate their
# direct input lineage.  This is deliberately separate from DML ownership: a
# table still has exactly one writer, while PostgreSQL services do not need an
# over-privileged API-reader connection merely to follow a foreign key.
_WRITER_READ_ALLOWLIST = {
    "canonical_schema_owner": (),
    "canonical_control_writer": (
        "schema_metadata",
        "deployment_approvals",
        "deployments",
        "runtime_instances",
        "runtime_receipts",
        "order_writer_leases",
        "execution_canary_risk_policies",
        "execution_risk_budget_authorizations",
        "execution_risk_reservations",
        "signals",
        "trade_intents",
        "risk_decisions",
        "orders",
        "fills",
        "ledger_entries",
        "reconciliation_runs",
        "reconciliation_items",
    ),
    "canonical_validation_writer": (
        "schema_metadata",
        "strategy_artifacts",
        "strategy_versions",
        "configuration_activations",
        "configuration_profiles",
        "configuration_versions",
        "configuration_dependencies",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "research_targets",
        "research_target_allocations",
        "market_snapshots",
        "market_snapshot_members",
        "market_profiles",
        "market_profile_versions",
        "market_artifacts",
        "market_inspections",
        "market_receipts",
    ),
    "canonical_scoring_writer": (
        "schema_metadata",
        "strategy_versions",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "research_targets",
        "market_snapshots",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
        "research_gate_attempts",
        "research_gate_receipts",
    ),
    "canonical_qualification_writer": (
        "schema_metadata",
        "strategy_versions",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "research_targets",
        "market_snapshots",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
        "target_scores",
    ),
    "canonical_optimization_writer": (
        "schema_metadata",
        "strategy_versions",
        "configuration_snapshots",
        "configuration_snapshot_members",
        "configuration_bundles",
        "configuration_bundle_members",
        "research_targets",
        "market_snapshots",
        "validation_plans",
        "validation_plan_windows",
        "validation_attempts",
        "validation_window_results",
        "target_scores",
        "qualification_decisions",
        "qualification_window_evidence",
    ),
    "canonical_approval_writer": (
        "schema_metadata",
        "audit_events",
        "qualification_decisions",
        "strategy_versions",
        "strategy_artifacts",
        "research_targets",
        "execution_attestations",
        "deployments",
        "execution_risk_reservations",
        "risk_decisions",
        "orders",
        "reconciliation_runs",
        "reconciliation_items",
    ),
    "canonical_deployment_writer": (
        "schema_metadata",
        "deployment_approvals",
        "qualification_decisions",
        "configuration_bundles",
        "runtime_image_acceptances",
        "order_writer_leases",
    ),
    "canonical_signal_writer": (
        "schema_metadata",
        "acceptance_signal_triggers",
        "deployment_approvals",
        "deployments",
        "qualification_decisions",
        "research_targets",
        "runtime_image_acceptances",
        "runtime_instances",
        "runtime_receipts",
    ),
    "canonical_risk_writer": (
        "schema_metadata",
        "signals",
        "deployments",
        "execution_risk_budget_authorizations",
        "execution_canary_risk_policies",
    ),
    "canonical_order_writer": (
        "schema_metadata",
        "deployments",
        "trade_intents",
        "risk_decisions",
        "execution_risk_budget_authorizations",
        "execution_canary_risk_policies",
        "execution_risk_reservations",
        "execution_attestations",
        "execution_canary_probe_receipts",
    ),
    "canonical_fill_writer": (
        "schema_metadata",
        "orders",
        "risk_decisions",
        "trade_intents",
    ),
    "canonical_ledger_writer": ("schema_metadata", "fills"),
    "canonical_reconciliation_writer": (
        "schema_metadata",
        "orders",
        "fills",
        "ledger_entries",
    ),
    "canonical_projection_writer": ("schema_metadata",),
}
WRITER_READ_ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    _WRITER_READ_ALLOWLIST
)

# UPDATE is reserved for explicit lifecycle/pointer/status transitions. Immutable
# evidence and receipt tables receive INSERT-only DML in the generated ACL.
_MUTABLE_TABLES = frozenset(
    {
        "strategies",
        "strategy_versions",
        "configuration_profiles",
        "configuration_versions",
        "configuration_activations",
        "market_profiles",
        "market_profile_versions",
        "validation_plans",
        "validation_attempts",
        "research_gate_attempts",
        "optimization_runs",
        "deployment_approvals",
        "execution_canary_risk_policies",
        "deployments",
        "runtime_instances",
        "order_writer_leases",
        "orders",
        "reconciliation_runs",
    }
)


@dataclass(frozen=True)
class CanonicalTableManifestEntry:
    """Exact authority and ACL identity for one canonical business table."""

    domain: str
    table: str
    authority: str
    writer: str
    readers: tuple[str, ...]
    writer_privileges: tuple[str, ...]


def _table_writer(table_name: str) -> str:
    writers = tuple(
        writer
        for writer, table_names in WRITER_TABLE_ALLOWLIST.items()
        if table_name in table_names
    )
    if len(writers) != 1:
        raise RuntimeError(
            f"BLOCKED_DESIGN_DRIFT: {table_name} has {len(writers)} writers"
        )
    return writers[0]


def _table_readers(table_name: str) -> tuple[str, ...]:
    application_readers = tuple(
        reader
        for reader, table_names in READER_TABLE_ALLOWLIST.items()
        if table_name in table_names
    )
    dependency_readers = tuple(
        writer
        for writer, table_names in WRITER_READ_ALLOWLIST.items()
        if table_name in table_names
    )
    return (*application_readers, *dependency_readers)


CANONICAL_TABLE_MANIFEST: Final[tuple[CanonicalTableManifestEntry, ...]] = tuple(
    CanonicalTableManifestEntry(
        domain=domain,
        table=table_name,
        authority=CANONICAL_DOMAIN_AUTHORITIES[domain],
        writer=_table_writer(table_name),
        readers=_table_readers(table_name),
        writer_privileges=("SELECT", "INSERT", "UPDATE")
        if table_name in _MUTABLE_TABLES
        else ("SELECT", "INSERT"),
    )
    for domain, table_names in CANONICAL_TABLES_BY_DOMAIN.items()
    for table_name in table_names
)

TABLE_MANIFEST_BY_NAME: Final[Mapping[str, CanonicalTableManifestEntry]] = (
    MappingProxyType({entry.table: entry for entry in CANONICAL_TABLE_MANIFEST})
)

FORBIDDEN_CANONICAL_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "research_jobs",
        "backtest_runs",
        "backtest_tasks",
        "backtest_results",
        "strategy_scores",
        "strategy_validation_plans",
        "strategy_validation_windows",
        "market_data_quality_receipts",
        "okx_demo_attestation_secrets",
        "okx_demo_operator_consent_secrets",
    }
)
FORBIDDEN_CANONICAL_TABLE_PREFIXES: Final[tuple[str, ...]] = (
    "strategy_platform_migration_",
    "okx_demo_",
    "legacy_",
)

INITIAL_PRODUCTION_STATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "target_set": "UNSET",
        "per_target_allocation": "UNSET",
        "per_target_cap": "UNSET",
        "market_profile": "UNSET",
        "market_snapshot": "UNSET",
        "window_coverage": "UNSET",
        "research_bundle": "BLOCKED",
        "trading": "TRADING_DISABLED",
    }
)


class CanonicalManifestError(RuntimeError):
    """Raised when code and the frozen Phase 0 design no longer agree."""


def canonical_manifest_payload() -> dict[str, object]:
    """Return the exact JSON-safe manifest payload used for the genesis digest."""

    return {
        "design_authority_key": DESIGN_AUTHORITY_KEY,
        "authority_revision": CANONICAL_AUTHORITY_REVISION,
        "identity": {
            "database_purpose": CANONICAL_DATABASE_PURPOSE,
            "business_schema": CANONICAL_BUSINESS_SCHEMA,
            "genesis_version": CANONICAL_GENESIS_VERSION,
            "manifest_key": CANONICAL_MANIFEST_KEY,
            "legacy_import_mode": CANONICAL_LEGACY_IMPORT_MODE,
            "production_default_target": CANONICAL_PRODUCTION_DEFAULT,
            "production_default_count": CANONICAL_PRODUCTION_DEFAULT,
            "production_default_cap": CANONICAL_PRODUCTION_DEFAULT,
            "trading_capability": CANONICAL_TRADING_CAPABILITY,
        },
        "p0_configuration_kinds": list(P0_CONFIGURATION_KINDS),
        "independent_market_profile_kind": INDEPENDENT_MARKET_PROFILE_KIND,
        "initial_production_states": dict(INITIAL_PRODUCTION_STATES),
        "tables": [asdict(entry) for entry in CANONICAL_TABLE_MANIFEST],
    }


def canonical_manifest_json() -> str:
    """Serialize the manifest canonically for content-addressed identity."""

    return json.dumps(
        canonical_manifest_payload(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_manifest_digest() -> str:
    return sha256(canonical_manifest_json().encode("utf-8")).hexdigest()


CANONICAL_MANIFEST_DIGEST: Final = canonical_manifest_digest()


def manifest_problems() -> tuple[str, ...]:
    """Return design drift problems without mutating or consulting a database."""

    problems: list[str] = []
    table_names = tuple(entry.table for entry in CANONICAL_TABLE_MANIFEST)
    if table_names != CANONICAL_TABLE_NAMES:
        problems.append("table manifest order/content differs from frozen domain list")
    if len(table_names) != len(set(table_names)):
        problems.append("canonical table manifest contains duplicate table names")
    if tuple(CANONICAL_DOMAIN_AUTHORITIES) != tuple(CANONICAL_TABLES_BY_DOMAIN):
        problems.append("domain authority keys differ from table domain keys")
    if tuple(WRITER_TABLE_ALLOWLIST) != WRITER_IDENTITIES:
        problems.append("writer identity allowlist differs from frozen identity order")
    if tuple(READER_TABLE_ALLOWLIST) != READER_IDENTITIES:
        problems.append("reader identity allowlist differs from frozen identity order")
    if tuple(WRITER_READ_ALLOWLIST) != WRITER_IDENTITIES:
        problems.append("writer read allowlist differs from frozen identity order")
    if len(P0_CONFIGURATION_KINDS) != 7 or len(set(P0_CONFIGURATION_KINDS)) != 7:
        problems.append("P0 configuration manifest must contain exactly seven kinds")

    writer_occurrences = {
        table_name: sum(
            table_name in table_names_for_writer
            for table_names_for_writer in WRITER_TABLE_ALLOWLIST.values()
        )
        for table_name in CANONICAL_TABLE_NAMES
    }
    for table_name, count in writer_occurrences.items():
        if count != 1:
            problems.append(f"table {table_name} has {count} writer identities")

    writer_tables = {
        table_name
        for table_names_for_writer in WRITER_TABLE_ALLOWLIST.values()
        for table_name in table_names_for_writer
    }
    if writer_tables != set(CANONICAL_TABLE_NAMES):
        problems.append("writer allowlists do not cover the exact canonical table set")

    for reader, table_names_for_reader in READER_TABLE_ALLOWLIST.items():
        if len(table_names_for_reader) != len(set(table_names_for_reader)):
            problems.append(f"reader {reader} contains duplicate table grants")
        unknown = set(table_names_for_reader) - set(CANONICAL_TABLE_NAMES)
        if unknown:
            problems.append(
                f"reader {reader} references unknown tables {sorted(unknown)}"
            )

    for writer, table_names_for_writer in WRITER_READ_ALLOWLIST.items():
        if len(table_names_for_writer) != len(set(table_names_for_writer)):
            problems.append(f"writer reader {writer} contains duplicate table grants")
        unknown = set(table_names_for_writer) - set(CANONICAL_TABLE_NAMES)
        if unknown:
            problems.append(
                f"writer reader {writer} references unknown tables {sorted(unknown)}"
            )
        overlap = set(table_names_for_writer) & set(WRITER_TABLE_ALLOWLIST[writer])
        if overlap:
            problems.append(
                f"writer reader {writer} duplicates owned tables {sorted(overlap)}"
            )

    forbidden = set(CANONICAL_TABLE_NAMES) & FORBIDDEN_CANONICAL_TABLE_NAMES
    if forbidden:
        problems.append(
            f"canonical manifest contains forbidden tables {sorted(forbidden)}"
        )
    prefixed = sorted(
        table_name
        for table_name in CANONICAL_TABLE_NAMES
        if table_name.startswith(FORBIDDEN_CANONICAL_TABLE_PREFIXES)
    )
    if prefixed:
        problems.append(
            f"canonical manifest contains forbidden table prefixes {prefixed}"
        )

    expected_initial = {
        "target_set": "UNSET",
        "per_target_allocation": "UNSET",
        "per_target_cap": "UNSET",
        "market_profile": "UNSET",
        "market_snapshot": "UNSET",
        "window_coverage": "UNSET",
        "research_bundle": "BLOCKED",
        "trading": "TRADING_DISABLED",
    }
    if dict(INITIAL_PRODUCTION_STATES) != expected_initial:
        problems.append(
            "initial production state contains a hidden/default business value"
        )
    if len(CANONICAL_MANIFEST_DIGEST) != 64:
        problems.append("manifest digest is not SHA-256 shaped")
    return tuple(problems)


def assert_canonical_manifest() -> None:
    problems = manifest_problems()
    if problems:
        raise CanonicalManifestError("BLOCKED_DESIGN_DRIFT: " + "; ".join(problems))


assert_canonical_manifest()
