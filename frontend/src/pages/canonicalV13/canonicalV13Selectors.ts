import type {
  ConfigurationCatalogProjection,
  ConfigurationProfileProjection,
  MarketInventoryProjection,
  MarketSnapshotProjection,
  ResearchPlanCatalogProjection,
  StrategyCatalogProjection,
  StrategyProjection,
} from "../../api/canonicalV13Types";
import { canonicalStatusGuidance } from "./canonicalV13Model.ts";

export type CanonicalSelectorOption = {
  description: string;
  label: string;
  status: string | null;
  value: string;
};

export type CanonicalContextOption = CanonicalSelectorOption & {
  scopeKey: string;
  workflowKey: string;
};

function byLabel(left: CanonicalSelectorOption, right: CanonicalSelectorOption): number {
  return left.label.localeCompare(right.label, "zh-CN") || left.value.localeCompare(right.value);
}

export function canonicalSelectionState(
  options: readonly CanonicalSelectorOption[],
  value: string,
): "selected" | "stale" | "unselected" {
  if (!value) return "unselected";
  return options.some((option) => option.value === value) ? "selected" : "stale";
}

export function filterCanonicalSelectorOptions(
  options: readonly CanonicalSelectorOption[],
  query: string,
): CanonicalSelectorOption[] {
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  if (!normalized) return [...options];
  return options.filter((option) => {
    const status = option.status ? canonicalStatusGuidance(option.status).label : "";
    return `${option.label} ${option.description} ${status}`.toLocaleLowerCase("zh-CN").includes(normalized);
  });
}

export function configurationContextOptions(
  catalog: ConfigurationCatalogProjection | null,
): CanonicalContextOption[] {
  if (!catalog || catalog.status !== "AVAILABLE") return [];
  const grouped = new Map<string, { profiles: number; scopeKey: string; versions: number; workflowKey: string }>();
  for (const profile of catalog.items) {
    const value = JSON.stringify([profile.scope_key, profile.workflow_key]);
    const current = grouped.get(value) ?? { profiles: 0, scopeKey: profile.scope_key, versions: 0, workflowKey: profile.workflow_key };
    current.profiles += 1;
    current.versions += profile.versions.length;
    grouped.set(value, current);
  }
  return [...grouped.entries()].map(([value, item]) => ({
    description: `${item.profiles} 个配置 Profile · ${item.versions} 个版本`,
    label: `${item.scopeKey} / ${item.workflowKey}`,
    scopeKey: item.scopeKey,
    status: null,
    value,
    workflowKey: item.workflowKey,
  })).sort(byLabel);
}

export function configurationProfileSelectorOptions(
  catalog: ConfigurationCatalogProjection | null,
  scopeKey: string | null,
  workflowKey: string | null,
): CanonicalSelectorOption[] {
  if (!catalog || catalog.status !== "AVAILABLE" || !scopeKey || !workflowKey) return [];
  return catalog.items.filter((profile) => (
    profile.scope_key === scopeKey && profile.workflow_key === workflowKey
  )).map((profile) => ({
    description: `${profile.configuration_kind} · ${profile.versions.length} 个版本`,
    label: profile.profile_key,
    status: null,
    value: profile.profile_id,
  })).sort(byLabel);
}

export function configurationVersionSelectorOptions(
  profile: ConfigurationProfileProjection | null,
): CanonicalSelectorOption[] {
  if (!profile) return [];
  return profile.versions.map((version) => ({
    description: version.validated_at ? `验证时间 ${version.validated_at}` : `创建时间 ${version.created_at}`,
    label: `版本 ${version.version_number}`,
    status: version.lifecycle_status,
    value: version.version_id,
  })).sort((left, right) => left.label.localeCompare(right.label, "zh-CN", { numeric: true }));
}

export function strategySelectorOptions(
  catalog: StrategyCatalogProjection | null,
): CanonicalSelectorOption[] {
  if (!catalog || catalog.status !== "AVAILABLE") return [];
  return catalog.items.map((strategy) => ({
    description: `版本 ${strategy.version_number} · ${canonicalStatusGuidance(strategy.qualification_status).label}`,
    label: strategy.display_name,
    status: strategy.validation_status,
    value: strategy.strategy_id,
  })).sort(byLabel);
}

function plansForStrategy(
  catalog: ResearchPlanCatalogProjection | null,
  strategy: StrategyProjection | null,
) {
  if (!catalog || catalog.status !== "AVAILABLE" || !strategy) return [];
  return catalog.items.filter((plan) => plan.strategy_version_id === strategy.current_version_id);
}

export function researchTargetSelectorOptions(
  catalog: ResearchPlanCatalogProjection | null,
  strategy: StrategyProjection | null,
): CanonicalSelectorOption[] {
  const grouped = new Map<string, { count: number; keys: Set<string> }>();
  for (const plan of plansForStrategy(catalog, strategy)) {
    const current = grouped.get(plan.research_target_id) ?? { count: 0, keys: new Set<string>() };
    current.count += 1;
    current.keys.add(plan.target_key);
    grouped.set(plan.research_target_id, current);
  }
  const options: CanonicalSelectorOption[] = [];
  for (const [value, item] of grouped) {
    if (item.keys.size !== 1) continue;
    options.push({
      description: `${item.count} 个 exact validation plan`,
      label: [...item.keys][0],
      status: null,
      value,
    });
  }
  return options.sort(byLabel);
}

export function researchPlanSelectorOptions(
  catalog: ResearchPlanCatalogProjection | null,
  strategy: StrategyProjection | null,
  targetId: string | null,
): CanonicalSelectorOption[] {
  return plansForStrategy(catalog, strategy).filter((plan) => (
    !targetId || plan.research_target_id === targetId
  )).map((plan) => ({
    description: `目标 ${plan.target_key}`,
    label: `${plan.target_key} 的研究计划`,
    status: plan.plan_status,
    value: plan.validation_plan_id,
  })).sort(byLabel);
}

export function marketProfileSelectorOptions(
  inventory: MarketInventoryProjection | null,
): CanonicalSelectorOption[] {
  if (!inventory) return [];
  return inventory.profiles.map((profile) => ({
    description: `Scope ${profile.scope_key}`,
    label: `${profile.profile_key} · 版本 ${profile.version_number}`,
    status: profile.lifecycle_status,
    value: profile.version_id,
  })).sort(byLabel);
}

export function marketSnapshotSelectorOptions(
  inventory: MarketInventoryProjection | null,
  profileVersionId: string | null,
): CanonicalSelectorOption[] {
  if (!inventory) return [];
  return inventory.snapshots.filter((snapshot) => (
    !profileVersionId || snapshot.market_profile_version_id === profileVersionId
  )).map((snapshot) => ({
    description: `${snapshot.member_count} 个成员`,
    label: `封存于 ${snapshot.created_at}`,
    status: null,
    value: snapshot.snapshot_id,
  })).sort(byLabel);
}

export function marketTargetSelectorOptions(
  snapshot: MarketSnapshotProjection | null,
): CanonicalSelectorOption[] {
  if (!snapshot) return [];
  const options: CanonicalSelectorOption[] = [];
  const byTarget = new Map<string, typeof snapshot.members>();
  for (const member of snapshot.members) {
    byTarget.set(member.research_target_id, [...(byTarget.get(member.research_target_id) ?? []), member]);
  }
  for (const [value, members] of byTarget) {
    const keys = new Set(members.map((member) => member.target_key));
    if (keys.size !== 1 || members.length !== 1) continue;
    const member = members[0];
    options.push({
      description: `${member.coverage_start} → ${member.coverage_end}`,
      label: member.target_key,
      status: member.receipt_status,
      value,
    });
  }
  return options.sort(byLabel);
}
