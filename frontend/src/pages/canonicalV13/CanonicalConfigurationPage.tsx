import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { fetchCanonicalConfigurations } from "../../api/canonicalV13Client";
import {
  CANONICAL_CONFIGURATION_KINDS,
  type ConfigurationProfileProjection,
  type ConfigurationVersionProjection,
} from "../../api/canonicalV13Types";
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

type PayloadEntry = { path: string; value: string };

function payloadEntries(value: unknown, prefix = ""): PayloadEntry[] {
  if (Array.isArray(value)) {
    if (!value.length) return [{ path: prefix || "配置", value: "[]" }];
    return value.flatMap((item, index) => payloadEntries(item, `${prefix}[${index}]`));
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return [{ path: prefix || "配置", value: "{}" }];
    return entries.flatMap(([key, item]) => payloadEntries(item, prefix ? `${prefix}.${key}` : key));
  }
  return [{
    path: prefix || "配置",
    value: value === null ? "null" : typeof value === "string" ? value : JSON.stringify(value),
  }];
}

function ConfigurationPayload({ payload }: { payload: Record<string, unknown> }) {
  return (
    <dl className="canonical-v13-definition-list canonical-v13-configuration-values">
      {payloadEntries(payload).map((entry) => (
        <div key={entry.path}><dt>{entry.path}</dt><dd><code>{entry.value}</code></dd></div>
      ))}
    </dl>
  );
}

function activeVersion(profile: ConfigurationProfileProjection): ConfigurationVersionProjection | null {
  return profile.versions.find((version) => version.active_in_bundle) ?? null;
}

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
  const selectedLatestVersionNumber = selectedProfile?.versions.length
    ? Math.max(...selectedProfile.versions.map((version) => version.version_number))
    : null;
  const contextProfiles = catalog.data?.items.filter((profile) => (
    profile.scope_key === url.values.scope && profile.workflow_key === url.values.workflow
  )) ?? [];
  const activeProfiles = contextProfiles.flatMap((profile) => {
    const version = activeVersion(profile);
    return version ? [{ profile, version }] : [];
  });

  useEffect(() => {
    if (!url.valid || url.values.scope || url.values.workflow || availability !== "ready") return;
    const activeContextValues = contexts.filter((context) => catalog.data?.items.some((profile) => (
      profile.scope_key === context.scopeKey
      && profile.workflow_key === context.workflowKey
      && Boolean(activeVersion(profile))
    )));
    const initial = activeContextValues.length === 1 ? activeContextValues[0]
      : contexts.length === 1 ? contexts[0]
        : null;
    if (!initial) return;
    setSearchParams(serializeCanonicalUrlState("configuration", {
      profile: null,
      scope: initial.scopeKey,
      version: null,
      workflow: initial.workflowKey,
    }), { replace: true });
  }, [availability, catalog.data, contexts, setSearchParams, url.valid, url.values.scope, url.values.workflow]);

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
    const profile = catalog.data?.items.find((item) => item.profile_id === value) ?? null;
    const defaultVersion = profile ? activeVersion(profile)
      ?? [...profile.versions].filter((version) => version.lifecycle_status === "VALIDATED").sort((left, right) => right.version_number - left.version_number)[0]
      ?? null
      : null;
    setSearchParams(serializeCanonicalUrlState("configuration", {
      ...url.values,
      profile: value,
      version: defaultVersion?.version_id ?? null,
    }));
  }

  function selectVersion(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("configuration", { ...url.values, version: value }));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="默认展示已激活 Bundle 绑定的七类 P0 配置；选择 Profile 和版本后可查看完整字段。所有内容均来自 canonical API。" eyebrow="V1.3 canonical-only" title="配置中心" />
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
      {contextValue && activeProfiles.length ? (
        <section className="canonical-v13-panel" aria-label="当前生效配置">
          <div className="canonical-v13-heading-row">
            <div><h2>当前默认配置</h2><p className="canonical-v13-panel-copy">以下版本由当前已激活 Bundle 精确绑定，不是 UI 推断的“最新版本”。</p></div>
            <CanonicalStatus status="VALIDATED" />
          </div>
          <div className="canonical-v13-card-list">
            {activeProfiles.map(({ profile, version }) => (
              <article className="canonical-v13-data-card canonical-v13-configuration-card" key={version.version_id}>
                <span>{profile.configuration_kind}</span>
                <strong>{profile.profile_key}</strong>
                <span>版本 {version.version_number} · 当前生效</span>
                <button className="canonical-v13-text-button" onClick={() => {
                  setSearchParams(serializeCanonicalUrlState("configuration", {
                    profile: profile.profile_id,
                    scope: profile.scope_key,
                    version: version.version_id,
                    workflow: profile.workflow_key,
                  }));
                }} type="button">查看完整配置</button>
              </article>
            ))}
          </div>
        </section>
      ) : contextValue && availability === "ready" ? (
        <CanonicalStatePanel description="该上下文没有可验证的 active Bundle 成员；请选择 Profile 查看历史 VALIDATED 版本，页面不会把最新版本伪装成默认配置。" kind="blocked" reasonCodes={["RESEARCH_BUNDLE_UNSET"]} title="当前默认配置未建立" />
      ) : null}
      {selectedVersion && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><div><h2>{selectedProfile?.profile_key} · 版本 {selectedVersion.version_number}</h2><p className="canonical-v13-panel-copy">{selectedVersion.active_in_bundle ? "这是当前 Bundle 正在使用的配置。" : selectedVersion.version_number === selectedLatestVersionNumber ? "这是该 Profile 的最新历史版本，但当前 Bundle 未使用该版本。" : "这是历史配置，当前 Bundle 未使用该版本。"}</p></div><CanonicalStatus status={selectedVersion.lifecycle_status} /></div>
          <h3>具体配置</h3>
          <ConfigurationPayload payload={selectedVersion.payload_json} />
          <details className="canonical-v13-advanced-evidence">
            <summary>高级标识与不可变摘要</summary>
            <dl className="canonical-v13-definition-list">
              <div><dt>Version ID</dt><dd><CopyableValue value={selectedVersion.version_id} /></dd></div>
              <div><dt>Payload digest</dt><dd><CopyableValue value={selectedVersion.payload_digest} /></dd></div>
              <div><dt>Schema digest</dt><dd><CopyableValue value={selectedVersion.schema_digest} /></dd></div>
              <div><dt>Snapshot</dt><dd>{selectedVersion.snapshot_id ? <CopyableValue value={selectedVersion.snapshot_id} /> : "未设置"}</dd></div>
              <div><dt>Snapshot digest</dt><dd>{selectedVersion.snapshot_digest ? <CopyableValue value={selectedVersion.snapshot_digest} /> : "未设置"}</dd></div>
              <div><dt>Activation</dt><dd>{selectedVersion.active_activation_id ? <CopyableValue value={selectedVersion.active_activation_id} /> : "未使用"}</dd></div>
              <div><dt>Active Bundle</dt><dd>{selectedVersion.active_bundle_id ? <CopyableValue value={selectedVersion.active_bundle_id} /> : "未使用"}</dd></div>
              <div><dt>Active Bundle digest</dt><dd>{selectedVersion.active_bundle_digest ? <CopyableValue value={selectedVersion.active_bundle_digest} /> : "未使用"}</dd></div>
            </dl>
          </details>
          <details><summary>原始 JSON 与 Schema</summary><h3>Payload JSON</h3><pre>{JSON.stringify(selectedVersion.payload_json, null, 2)}</pre><h3>Schema JSON</h3><pre>{JSON.stringify(selectedVersion.schema_json, null, 2)}</pre></details>
        </section>
      ) : <CanonicalStatePanel description={activeProfiles.length ? "上方已展示当前 Bundle 的默认配置；选择任一配置卡片或 Profile，即可查看完整字段和历史版本。" : "请选择 Profile 和版本查看具体配置。"} kind="empty" title="尚未选择具体配置" />}
    </div>
  );
}
