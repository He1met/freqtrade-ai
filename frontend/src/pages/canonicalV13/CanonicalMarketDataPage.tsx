import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { fetchCanonicalMarketInventory, fetchCanonicalMarketSnapshot } from "../../api/canonicalV13Client";
import type { MarketSnapshotProjection } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus, useCanonicalQuery } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState, serializeCanonicalUrlState } from "./canonicalV13Model";

function MarketSnapshotDetail({ snapshotId, target }: { snapshotId: string; target: string | null }) {
  const query = useCanonicalQuery((signal) => fetchCanonicalMarketSnapshot(snapshotId, signal), [snapshotId]);
  if (query.loading) return <CanonicalStatePanel description="正在读取冻结的 artifact/receipt/coverage members。" kind="loading" title="加载 market snapshot" />;
  if (query.error) return <CanonicalQueryError error={query.error} title="Market snapshot 不存在或不可读取" />;
  const snapshot = query.data as MarketSnapshotProjection;
  if (!canonicalStatusesKnown(snapshot.status, ...snapshot.members.map((member) => member.receipt_status))) {
    return <CanonicalStatePanel description="Snapshot projection 返回未知 enum；member 列表与 accepted 状态保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Market snapshot 合同漂移" />;
  }
  const members = target
    ? snapshot.members.filter((member) => member.target_key === target || member.research_target_id === target)
    : snapshot.members;
  return (
    <section className="canonical-v13-panel">
      <div className="canonical-v13-heading-row"><h2>Sealed market snapshot</h2><CanonicalStatus status={snapshot.status} /></div>
      {snapshot.reason_codes.length ? <CanonicalStatePanel description="Snapshot 当前不可作为 research evidence。" kind="blocked" reasonCodes={snapshot.reason_codes} title="Market evidence BLOCKED" /> : null}
      <dl className="canonical-v13-definition-list">
        <div><dt>Snapshot ID</dt><dd><CopyableValue value={snapshot.snapshot_id} /></dd></div>
        <div><dt>Snapshot digest</dt><dd><CopyableValue value={snapshot.snapshot_digest} /></dd></div>
        <div><dt>Profile version</dt><dd><CopyableValue value={snapshot.market_profile_version_id} /></dd></div>
      </dl>
      {target && !members.length ? <CanonicalStatePanel description="所选 target 不在该 snapshot 的 member projection 中。" kind="unknown" title="SELECTED_TARGET_NOT_FOUND" /> : null}
      <div className="canonical-v13-card-list">
        {members.map((member) => (
          <article className="canonical-v13-data-card" key={`${member.research_target_id}:${member.market_artifact_id}`}>
            <div className="canonical-v13-heading-row"><strong>{member.target_key}</strong><CanonicalStatus status={member.receipt_status} /></div>
            <span>{member.coverage_start} → {member.coverage_end}</span>
            <CopyableValue label="Artifact digest" value={member.artifact_digest} />
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
  const [profile, setProfile] = useState(url.values.profile ?? "");
  const [target, setTarget] = useState(url.values.target ?? "");
  const [selectionProblem, setSelectionProblem] = useState<string | null>(null);
  useEffect(() => {
    setProfile(url.values.profile ?? "");
    setTarget(url.values.target ?? "");
    setSelectionProblem(null);
  }, [searchParams]);

  function applyFilter(event: FormEvent) {
    event.preventDefault();
    try {
      setSelectionProblem(null);
      setSearchParams(serializeCanonicalUrlState("market-data", { ...url.values, profile: profile || null, target: target || null }));
    } catch (reason) {
      setSelectionProblem(reason instanceof Error ? reason.message : "INVALID_URL_STATE");
    }
  }

  const snapshots = inventory.data?.snapshots.filter((snapshot) =>
    !url.values.profile || snapshot.market_profile_version_id === url.values.profile
  ) ?? [];
  const inventoryContractKnown = inventory.data ? canonicalStatusesKnown(inventory.data.status) : true;

  return (
    <div className="canonical-v13-page">
      <PageHeader description="路径只是 locator；页面只显示 canonical accepted receipt 与 frozen snapshot projection。" eyebrow="V1.3 canonical-only" title="Market Data" />
      <form className="canonical-v13-filter" onSubmit={applyFilter}>
        <label>Market profile version<input value={profile} onChange={(event) => setProfile(event.target.value)} /></label>
        <label>Target key / ID<input value={target} onChange={(event) => setTarget(event.target.value)} /></label>
        <button className="formal-primary-button" type="submit">写入 URL</button>
      </form>
      {selectionProblem ? <CanonicalStatePanel description="Profile 必须是 UUID，target 必须符合 URL 合同；未提交新的读取。" kind="unknown" reasonCodes={[selectionProblem]} title="INVALID_SELECTION" /> : null}
      {!url.valid ? <CanonicalStatePanel description="Market URL state 含未知、重复或非法选择。" kind="unknown" reasonCodes={url.problems} title="INVALID_URL_STATE" /> : null}
      {inventory.loading ? <CanonicalStatePanel description="正在读取 market inventory。" kind="loading" title="加载行情证据" /> : null}
      {inventory.error ? <CanonicalQueryError error={inventory.error} title="Market inventory 状态未知" /> : null}
      {inventory.data && !inventoryContractKnown ? <CanonicalStatePanel description="Market inventory 返回未知 enum；snapshot selection 保持禁用。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Market inventory 合同漂移" /> : null}
      {inventoryContractKnown && inventory.data?.status === "MARKET_SNAPSHOT_UNSET" ? (
        <CanonicalStatePanel description="没有新的 canonical market snapshot；历史 v47 receipt 不会作为 fallback。" kind="blocked" reasonCodes={["MARKET_SNAPSHOT_UNSET"]} title="Market evidence 未设置" />
      ) : null}
      {inventory.data && inventoryContractKnown ? (
        <section className="canonical-v13-panel">
          <div className="canonical-v13-heading-row"><h2>Inventory</h2><CanonicalStatus status={inventory.data.status} /></div>
          <div className="canonical-v13-metrics">
            <div><span>Profiles</span><strong>{inventory.data.profile_count}</strong></div>
            <div><span>Validated profiles</span><strong>{inventory.data.validated_profile_count}</strong></div>
            <div><span>Artifacts</span><strong>{inventory.data.artifact_count}</strong></div>
            <div><span>Accepted receipts</span><strong>{inventory.data.accepted_receipt_count}</strong></div>
          </div>
          <div className="canonical-v13-card-list">
            {snapshots.map((snapshot) => (
              <button className="canonical-v13-select-card" key={snapshot.snapshot_id} onClick={() => setSearchParams(serializeCanonicalUrlState("market-data", { ...url.values, snapshot: snapshot.snapshot_id }))} type="button">
                <strong>{snapshot.member_count} members</strong><span>{snapshot.snapshot_id}</span><span>{snapshot.created_at}</span>
              </button>
            ))}
          </div>
          {url.values.profile && !snapshots.length ? <CanonicalStatePanel description="所选 profile 没有 snapshot；未改写全局 inventory 事实。" kind="empty" title="当前 profile 无 snapshot" /> : null}
        </section>
      ) : null}
      {url.values.snapshot && url.valid && inventoryContractKnown ? <MarketSnapshotDetail snapshotId={url.values.snapshot} target={url.values.target ?? null} /> : <CanonicalStatePanel description="请显式选择 snapshot；UI 不自动选择最新项。" kind="empty" title="尚未选择 market snapshot" />}
    </div>
  );
}
