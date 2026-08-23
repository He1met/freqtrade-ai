import { useSearchParams } from "react-router-dom";

import { fetchCanonicalMarketInventory, fetchCanonicalMarketSnapshot } from "../../api/canonicalV13Client";
import type { MarketSnapshotProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalSearchSelect, type CanonicalSelectorAvailability } from "./CanonicalSearchSelect";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";
import { canonicalSelectionState, marketProfileSelectorOptions, marketSnapshotSelectorOptions, marketTargetSelectorOptions } from "./canonicalV13Selectors";

function MarketSnapshotDetail({ snapshot, target }: { snapshot: MarketSnapshotProjection; target: string | null }) {
  const members = target
    ? snapshot.members.filter((member) => member.target_key === target || member.research_target_id === target)
    : snapshot.members;
  return (
    <section className="canonical-v13-panel">
      <div className="canonical-v13-heading-row"><h2>已封存行情快照</h2><CanonicalStatus status={snapshot.status} /></div>
      {snapshot.reason_codes.length ? <CanonicalStatePanel description="Snapshot 当前不可作为 research evidence。" kind="blocked" reasonCodes={snapshot.reason_codes} title="行情证据已阻断" /> : null}
      <p>封存时间：{snapshot.created_at} · {snapshot.members.length} 个 API member</p>
      <details className="canonical-v13-advanced-evidence"><summary>高级 Snapshot 标识</summary><dl className="canonical-v13-definition-list">
        <div><dt>Snapshot ID</dt><dd><CopyableValue value={snapshot.snapshot_id} /></dd></div>
        <div><dt>Snapshot digest</dt><dd><CopyableValue value={snapshot.snapshot_digest} /></dd></div>
        <div><dt>Profile version</dt><dd><CopyableValue value={snapshot.market_profile_version_id} /></dd></div>
      </dl></details>
      {target && !members.length ? <CanonicalStatePanel description="所选 target 不在该 snapshot 的 member projection 中。" kind="unknown" reasonCodes={["SELECTED_TARGET_NOT_FOUND"]} title="所选研究目标不存在" /> : null}
      <div className="canonical-v13-card-list">
        {members.map((member) => (
          <article className="canonical-v13-data-card" key={`${member.research_target_id}:${member.market_artifact_id}`}>
            <div className="canonical-v13-heading-row"><strong>{member.target_key}</strong><CanonicalStatus status={member.receipt_status} /></div>
            <span>{member.coverage_start} → {member.coverage_end}</span>
            <details className="canonical-v13-advanced-evidence"><summary>高级 Artifact 标识</summary>
              <CopyableValue label="Target ID" value={member.research_target_id} />
              <CopyableValue label="Artifact digest" value={member.artifact_digest} />
              <CopyableValue label="Receipt digest" value={member.receipt_digest} />
            </details>
          </article>
        ))}
      </div>
    </section>
  );
}

export function CanonicalMarketDataPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const url = parseCanonicalUrlState("market-data", searchParams);
  const inventory = useCanonicalQuery(fetchCanonicalMarketInventory, [], url.valid);
  const selectedSnapshotId = url.valid ? url.values.snapshot ?? null : null;
  const selectedSnapshot = useCanonicalQuery(
    (signal) => fetchCanonicalMarketSnapshot(selectedSnapshotId ?? "", signal),
    [selectedSnapshotId],
    Boolean(selectedSnapshotId),
  );
  const inventoryContractKnown = inventory.data
    ? canonicalStatusesKnown(inventory.data.status, ...inventory.data.profiles.map((profile) => profile.lifecycle_status))
    : true;
  const snapshotContractKnown = selectedSnapshot.data
    ? canonicalStatusesKnown(selectedSnapshot.data.status, ...selectedSnapshot.data.members.map((member) => member.receipt_status))
    : true;
  const inventoryAvailability: CanonicalSelectorAvailability = !url.valid || inventory.error || !inventoryContractKnown ? "unavailable" : inventory.loading ? "loading" : inventory.data ? "ready" : "empty";
  const targetAvailability: CanonicalSelectorAvailability = !url.valid || selectedSnapshot.error || !snapshotContractKnown ? "unavailable" : selectedSnapshot.loading ? "loading" : selectedSnapshot.data ? "ready" : "empty";
  const profileOptions = marketProfileSelectorOptions(inventoryContractKnown ? inventory.data : null);
  const snapshotOptions = marketSnapshotSelectorOptions(inventoryContractKnown ? inventory.data : null, url.values.profile ?? null);
  const targetOptions = marketTargetSelectorOptions(snapshotContractKnown ? selectedSnapshot.data : null);
  const legacyTarget = selectedSnapshot.data?.members.find((member) => member.target_key === url.values.target) ?? null;
  const targetSelectorValue = url.values.target && canonicalSelectionState(targetOptions, url.values.target) === "selected"
    ? url.values.target
    : legacyTarget?.research_target_id ?? url.values.target ?? "";
  const profileStale = Boolean(url.values.profile && inventoryAvailability === "ready" && canonicalSelectionState(profileOptions, url.values.profile) === "stale");
  const snapshotStale = Boolean(selectedSnapshotId && inventoryAvailability === "ready" && canonicalSelectionState(snapshotOptions, selectedSnapshotId) === "stale");
  const targetStale = Boolean(url.values.target && selectedSnapshot.data && targetAvailability === "ready" && !legacyTarget && canonicalSelectionState(targetOptions, url.values.target) === "stale");

  function selectProfile(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("market-data", { profile: value, snapshot: null, target: null }));
  }

  function selectSnapshot(value: string | null) {
    const selected = inventory.data?.snapshots.find((snapshot) => snapshot.snapshot_id === value) ?? null;
    setSearchParams(serializeCanonicalUrlState("market-data", {
      profile: selected?.market_profile_version_id ?? url.values.profile ?? null,
      snapshot: value,
      target: null,
    }));
  }

  function selectTarget(value: string | null) {
    setSearchParams(serializeCanonicalUrlState("market-data", { ...url.values, target: value }));
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader description="选择器分别来自 canonical 已验证 Profile 版本、已封存 Profile 快照和 member projection；路径只是稳定 locator。" eyebrow="V1.3 canonical-only" title="行情证据" />
      <section className="canonical-v13-selector-panel" aria-label="行情证据选择器">
        <CanonicalSearchSelect availability={inventoryAvailability} label="已验证 Profile 版本" onChange={selectProfile} options={profileOptions} value={url.values.profile ?? ""} />
        <CanonicalSearchSelect availability={inventoryAvailability} label="已封存 Profile 快照" onChange={selectSnapshot} options={snapshotOptions} value={selectedSnapshotId ?? ""} />
        <CanonicalSearchSelect availability={targetAvailability} disabled={!selectedSnapshotId} label="研究目标" onChange={selectTarget} options={targetOptions} value={targetSelectorValue} />
      </section>
      {!url.valid ? <CanonicalStatePanel description="Market URL state 含未知、重复或非法选择；selector 请求未发送。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" /> : null}
      {inventory.loading ? <CanonicalStatePanel description="正在读取 market profile 与 snapshot 选项。" kind="loading" title="加载行情选择器" /> : null}
      {inventory.error ? <CanonicalQueryError error={inventory.error} title="行情选择器暂不可用" /> : null}
      {inventory.data && !inventoryContractKnown ? <CanonicalStatePanel description="Market inventory 返回未知 enum；所有 selection 保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="行情清单合同漂移" /> : null}
      {selectedSnapshot.loading ? <CanonicalStatePanel description="正在读取所选 snapshot 的 target members。" kind="loading" title="加载研究目标选项" /> : null}
      {selectedSnapshot.error ? <CanonicalQueryError error={selectedSnapshot.error} title="所选行情快照无法读取" /> : null}
      {selectedSnapshot.data && !snapshotContractKnown ? <CanonicalStatePanel description="Snapshot projection 返回未知 enum；target selection 保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="行情快照合同漂移" /> : null}
      {profileStale ? <CanonicalStatePanel description="Committed market profile version 不在最新 API inventory 中。" kind="unknown" reasonCodes={["SELECTED_MARKET_PROFILE_NOT_FOUND"]} title="所选 Market Profile 已失效" /> : null}
      {snapshotStale ? <CanonicalStatePanel description="Committed snapshot 不属于当前 profile 的最新 API inventory。" kind="unknown" reasonCodes={["SELECTED_SNAPSHOT_NOT_FOUND"]} title="所选 Market Snapshot 已失效" /> : null}
      {targetStale ? <CanonicalStatePanel description="Committed target 不属于当前 snapshot member projection。" kind="unknown" reasonCodes={["SELECTED_TARGET_NOT_FOUND"]} title="所选研究目标已失效" /> : null}
      {inventoryContractKnown && inventory.data?.status === "MARKET_SNAPSHOT_UNSET" ? <CanonicalStatePanel description="没有新的 canonical market snapshot；历史 v47 receipt 不会作为 fallback。" kind="blocked" reasonCodes={["MARKET_SNAPSHOT_UNSET"]} title="行情证据未设置" /> : null}
      {inventory.data && inventoryContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>行情证据总览</h2><CanonicalStatus status={inventory.data.status} /></div>
          <div className="canonical-v13-metrics">
            <div><span>行情 Profiles</span><strong>{inventory.data.profile_count}</strong></div>
            <div><span>已验证 Profile 版本</span><strong>{inventory.data.validated_profile_count}</strong></div>
            <div><span>行情 Artifacts</span><strong>{inventory.data.artifact_count}</strong></div>
            <div><span>已接受行情 Receipts</span><strong>{inventory.data.accepted_receipt_count}</strong></div>
          </div>
          {url.values.profile && !snapshotOptions.length ? <CanonicalStatePanel description="所选 profile 没有 snapshot；未改写全局 inventory 事实。" kind="empty" title="当前 profile 无 snapshot" /> : null}
        </section>
      ) : null}
      {selectedSnapshot.data && snapshotContractKnown && !snapshotStale && !targetStale
        ? <MarketSnapshotDetail snapshot={selectedSnapshot.data} target={url.values.target ?? null} />
        : <CanonicalStatePanel description="从 API 选项显式选择 snapshot；UI 不自动选择最新项。" kind="empty" title="尚未选择 market snapshot" />}
    </div>
  );
}
