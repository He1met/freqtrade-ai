import { useSearchParams } from "react-router-dom";

import { fetchCanonicalConfigurations } from "../../api/canonicalV13Client";
import { CANONICAL_CONFIGURATION_KINDS } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalSearchSelect, type CanonicalSelectorAvailability } from "./CanonicalSearchSelect";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";
import {
  canonicalSelectionState,
  configurationContextOptions,
  configurationProfileSelectorOptions,
  configurationVersionSelectorOptions,
} from "./canonicalV13Selectors";

export function CanonicalConfigurationPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("configuration", searchParams);
  const catalog = useCanonicalQuery(fetchCanonicalConfigurations, [], url.valid);
  const catalogContractKnown = catalog.data
    ? canonicalStatusesKnown(catalog.data.status, ...catalog.data.items.flatMap((profile) => profile.versions.map((version) => version.lifecycle_status)))
      && catalog.data.items.every((profile) => (CANONICAL_CONFIGURATION_KINDS as readonly string[]).includes(profile.configuration_kind))
    : true;
  const availability: CanonicalSelectorAvailability = !url.valid || catalog.error || !catalogContractKnown ? "unavailable"
    : catalog.loading ? "loading"
      : catalog.data?.status === "AVAILABLE" ? "ready" : "empty";
  const contexts = configurationContextOptions(catalogContractKnown ? catalog.data : null);
  const contextValue = url.values.scope && url.values.workflow
    ? JSON.stringify([url.values.scope, url.values.workflow])
    : "";
  const profiles = configurationProfileSelectorOptions(catalogContractKnown ? catalog.data : null, url.values.scope ?? null, url.values.workflow ?? null);
  const selectedProfile = catalog.data?.items.find((profile) => (
    profile.profile_id === url.values.profile
    && profile.scope_key === url.values.scope
    && profile.workflow_key === url.values.workflow
  )) ?? null;
  const versions = configurationVersionSelectorOptions(selectedProfile);
  const selectedVersion = selectedProfile?.versions.find((version) => version.version_id === url.values.version) ?? null;

  function selectContext(value: string | null) {
    const selected = contexts.find((option) => option.value === value) ?? null;
    setSearchParams(serializeCanonicalUrlState("configuration", {
      profile: null,
      scope: selected?.scopeKey ?? null,
      version: null,
      workflow: selected?.workflowKey ?? null,
    }));
  }

  function selectProfile(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("configuration", {
      ...url.values,
      profile: value,
      version: null,
    }));
  }

  function selectVersion(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("configuration", { ...url.values, version: value }));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="七类 P0 配置只有 API projection 是事实；Scope 与 Workflow 以同一 API 上下文成对选择。" eyebrow="V1.3 canonical-only" title="配置中心" />
      <section className="canonical-v13-selector-panel" aria-label="配置上下文选择器">
        <CanonicalSearchSelect availability={availability} label="Scope / Workflow 上下文" onChange={selectContext} options={contexts} value={contextValue} />
        <CanonicalSearchSelect availability={availability} disabled={!url.values.scope || !url.values.workflow} label="配置 Profile" onChange={selectProfile} options={profiles} value={url.values.profile ?? ""} />
        <CanonicalSearchSelect availability={availability} disabled={!selectedProfile} label="配置版本" onChange={selectVersion} options={versions} value={url.values.version ?? ""} />
      </section>
      {!url.valid ? <CanonicalStatePanel description="Scope/Workflow 必须同时提供，且 URL 不能含未知或重复 key。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" /> : null}
      {catalog.loading ? <CanonicalStatePanel description="正在读取七类 canonical 配置及其可选上下文。" kind="loading" title="加载配置选择器" /> : null}
      {catalog.error ? <CanonicalQueryError error={catalog.error} title="配置选择器暂不可用" /> : null}
      {catalog.data && !catalogContractKnown ? <CanonicalStatePanel description="配置 projection 含未知 kind/status；所有选择器保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="配置合同漂移" /> : null}
      {availability === "empty" ? <CanonicalStatePanel description="Canonical API 未返回配置 Profile；页面不会填充 Scope、Workflow 或版本默认值。" kind="empty" title="暂无配置选项" /> : null}
      {contextValue && availability === "ready" && canonicalSelectionState(contexts, contextValue) === "stale" ? <CanonicalStatePanel description="Committed Scope/Workflow 不在最新配置 projection 中；未自动选择其他上下文。" kind="unknown" reasonCodes={["SELECTED_CONTEXT_NOT_FOUND"]} title="所选配置上下文已失效" /> : null}
      {url.values.profile && availability === "ready" && canonicalSelectionState(profiles, url.values.profile) === "stale" ? <CanonicalStatePanel description="所选 profile 不存在于当前 Scope/Workflow projection。" kind="unknown" reasonCodes={["SELECTED_PROFILE_NOT_FOUND"]} title="所选配置 Profile 不存在" /> : null}
      {url.values.version && selectedProfile && canonicalSelectionState(versions, url.values.version) === "stale" ? <CanonicalStatePanel description="所选 version 不属于当前 profile。" kind="unknown" reasonCodes={["SELECTED_VERSION_NOT_FOUND"]} title="所选配置版本不存在" /> : null}
      {catalog.data && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>P0 配置事实</h2><CanonicalStatus status={catalog.data.status} /></div>
          {catalog.data.unset_kinds.length ? (
            <CanonicalStatePanel description="以下配置 kind 尚未建立；UI 不填充默认业务值。" kind="blocked" reasonCodes={catalog.data.unset_kinds.map((kind) => `${kind}_UNSET`)} title="研究配置不完整" />
          ) : null}
        </section>
      ) : null}
      {selectedVersion && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>{selectedProfile?.profile_key} · 版本 {selectedVersion.version_number}</h2><CanonicalStatus status={selectedVersion.lifecycle_status} /></div>
          <details className="canonical-v13-advanced-evidence">
            <summary>高级标识与不可变摘要</summary>
            <dl className="canonical-v13-definition-list">
              <div><dt>Version ID</dt><dd><CopyableValue value={selectedVersion.version_id} /></dd></div>
              <div><dt>Payload digest</dt><dd><CopyableValue value={selectedVersion.payload_digest} /></dd></div>
              <div><dt>Snapshot</dt><dd>{selectedVersion.snapshot_id ? <CopyableValue value={selectedVersion.snapshot_id} /> : "未设置"}</dd></div>
            </dl>
          </details>
          <details><summary>只读 payload</summary><pre>{JSON.stringify(selectedVersion.payload_json, null, 2)}</pre></details>
        </section>
      ) : <CanonicalStatePanel description="按 API 选项显式选择上下文、Profile 和版本；UI 不自动选择最新项。" kind="empty" title="尚未选择配置版本" />}
    </div>
  );
}
