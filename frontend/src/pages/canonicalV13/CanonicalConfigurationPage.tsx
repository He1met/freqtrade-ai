import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { fetchCanonicalConfigurations } from "../../api/canonicalV13Client";
import { CANONICAL_CONFIGURATION_KINDS } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";

export function CanonicalConfigurationPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("configuration", searchParams);
  const catalog = useCanonicalQuery(fetchCanonicalConfigurations, [], url.valid);
  const [scope, setScope] = useState(url.values.scope ?? "");
  const [workflow, setWorkflow] = useState(url.values.workflow ?? "");
  const [selectionProblem, setSelectionProblem] = useState<string | null>(null);
  useEffect(() => {
    setScope(url.values.scope ?? "");
    setWorkflow(url.values.workflow ?? "");
    setSelectionProblem(null);
  }, [searchParams]);

  function applyScope(event: FormEvent) {
    event.preventDefault();
    try {
      setSelectionProblem(null);
      setSearchParams(serializeCanonicalUrlState("configuration", {
        ...url.values,
        scope: scope || null,
        workflow: workflow || null,
      }));
    } catch (reason) {
      setSelectionProblem(reason instanceof Error ? reason.message : "INVALID_URL_STATE");
    }
  }

  const profiles = catalog.data?.items.filter((profile) =>
    (!url.values.scope || profile.scope_key === url.values.scope)
    && (!url.values.workflow || profile.workflow_key === url.values.workflow)
  ) ?? [];
  const selectedProfile = profiles.find((profile) =>
    profile.profile_id === url.values.profile
  ) ?? null;
  const selectedVersion = selectedProfile?.versions.find((version) => version.version_id === url.values.version) ?? null;
  const catalogContractKnown = catalog.data
    ? canonicalStatusesKnown(catalog.data.status, ...catalog.data.items.flatMap((profile) => profile.versions.map((version) => version.lifecycle_status)))
      && catalog.data.items.every((profile) => (CANONICAL_CONFIGURATION_KINDS as readonly string[]).includes(profile.configuration_kind))
    : true;

  return (
    <div className="canonical-v13-page">
      <PageHeader description="七类 P0 配置只有 API projection 是事实；scope 与 workflow 必须成对显式选择。" eyebrow="V1.3 canonical-only" title="Configuration Center" />
      <form className="canonical-v13-filter" onSubmit={applyScope}>
        <label>Scope<input value={scope} onChange={(event) => setScope(event.target.value)} /></label>
        <label>Workflow<input value={workflow} onChange={(event) => setWorkflow(event.target.value)} /></label>
        <button className="formal-primary-button" type="submit">写入 URL</button>
      </form>
      {selectionProblem ? <CanonicalStatePanel description="Scope/workflow 必须成对且符合 URL 合同；未提交新的读取。" kind="unknown" reasonCodes={[selectionProblem]} title="INVALID_SELECTION" /> : null}
      {!url.valid ? <CanonicalStatePanel description="scope/workflow 必须同时提供，且 URL 不能含未知或重复 key。" kind="unknown" reasonCodes={url.problems} title="INVALID_URL_STATE" /> : null}
      {catalog.loading ? <CanonicalStatePanel description="正在读取七类 canonical 配置。" kind="loading" title="加载配置" /> : null}
      {catalog.error ? <CanonicalQueryError error={catalog.error} title="配置状态未知" /> : null}
      {catalog.data && !catalogContractKnown ? <CanonicalStatePanel description="配置 projection 含未知 kind/status；版本 selection 保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Configuration 合同漂移" /> : null}
      {catalog.data && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>P0 configuration authority</h2><CanonicalStatus status={catalog.data.status} /></div>
          {catalog.data.unset_kinds.length ? (
            <CanonicalStatePanel description="以下配置 kind 尚未建立；UI 不填充默认业务值。" kind="blocked" reasonCodes={catalog.data.unset_kinds.map((kind) => `${kind}_UNSET`)} title="UNSET/BLOCKED" />
          ) : null}
          <div className="canonical-v13-card-list">
            {profiles.map((profile) => (
              <button
                aria-pressed={selectedProfile?.profile_id === profile.profile_id}
                className="canonical-v13-select-card"
                key={profile.profile_id}
                onClick={() => setSearchParams(serializeCanonicalUrlState("configuration", { ...url.values, profile: profile.profile_id, version: null }))}
                type="button"
              >
                <strong>{profile.configuration_kind}</strong><span>{profile.profile_key}</span><span>{profile.scope_key} / {profile.workflow_key}</span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {url.values.profile && !selectedProfile && catalog.data && catalogContractKnown ? <CanonicalStatePanel description="所选 profile 不存在于当前 scope/workflow projection。" kind="unknown" title="SELECTED_PROFILE_NOT_FOUND" /> : null}
      {selectedProfile && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <h2>{selectedProfile.profile_key} versions</h2>
          <div className="canonical-v13-card-list">
            {selectedProfile.versions.map((version) => (
              <button className="canonical-v13-select-card" key={version.version_id} onClick={() => setSearchParams(serializeCanonicalUrlState("configuration", { ...url.values, version: version.version_id }))} type="button">
                <strong>Version {version.version_number}</strong><CanonicalStatus status={version.lifecycle_status} /><span>{version.version_id}</span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {url.values.version && !selectedVersion && selectedProfile ? <CanonicalStatePanel description="所选 version 不属于当前 profile。" kind="unknown" title="SELECTED_VERSION_NOT_FOUND" /> : null}
      {selectedVersion && catalogContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>Immutable version</h2><CanonicalStatus status={selectedVersion.lifecycle_status} /></div>
          <dl className="canonical-v13-definition-list">
            <div><dt>Version ID</dt><dd><CopyableValue value={selectedVersion.version_id} /></dd></div>
            <div><dt>Payload digest</dt><dd><CopyableValue value={selectedVersion.payload_digest} /></dd></div>
            <div><dt>Snapshot</dt><dd>{selectedVersion.snapshot_id ? <CopyableValue value={selectedVersion.snapshot_id} /> : "UNSET"}</dd></div>
          </dl>
          <details><summary>只读 payload</summary><pre>{JSON.stringify(selectedVersion.payload_json, null, 2)}</pre></details>
        </section>
      ) : <CanonicalStatePanel description="请显式选择 profile/version；UI 不自动选择最新版本。" kind="empty" title="尚未选择配置版本" />}
    </div>
  );
}
