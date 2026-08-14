import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { submitCanonicalStrategy } from "../../api/canonicalV13Client";
import type { SubmissionReceipt } from "../../api/canonicalV13Types";
import { CopyableValue, PageHeader } from "../../components/DisplayPrimitives";
import { CanonicalQueryError, CanonicalStatePanel, CanonicalStatus } from "./CanonicalStatePanel";
import { canonicalStatusesKnown, parseCanonicalUrlState } from "./canonicalV13Model";

type SubmissionForm = {
  archiveDigest: string;
  artifactContent: string;
  callerIdentity: string;
  displayName: string;
  idempotencyKey: string;
  sourceEntryKey: string;
  sourceStrategyKey: string;
  versionId: string;
  versionNumber: string;
};

const EMPTY_FORM: SubmissionForm = {
  archiveDigest: "",
  artifactContent: "",
  callerIdentity: "",
  displayName: "",
  idempotencyKey: "",
  sourceEntryKey: "",
  sourceStrategyKey: "",
  versionId: "",
  versionNumber: "",
};

function utf8Base64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return window.btoa(binary);
}

export function CanonicalSubmissionPage() {
  const [searchParams] = useSearchParams();
  const url = parseCanonicalUrlState("submission", searchParams);
  const [form, setForm] = useState<SubmissionForm>(EMPTY_FORM);
  const [receipt, setReceipt] = useState<SubmissionReceipt | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const receiptContractKnown = receipt
    ? canonicalStatusesKnown(receipt.intake_status, receipt.catalog_status, receipt.validation_status, receipt.qualification_status)
    : true;

  function field<K extends keyof SubmissionForm>(key: K, value: SubmissionForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const versionNumber = Number(form.versionNumber);
    if (!Number.isInteger(versionNumber) || versionNumber <= 0) {
      setError(new Error("INVALID_VERSION_NUMBER: version number 必须是正整数"));
      return;
    }
    setSubmitting(true);
    setError(null);
    setReceipt(null);
    try {
      const result = await submitCanonicalStrategy({
        archive_snapshot_digest: form.archiveDigest,
        caller_identity: form.callerIdentity,
        current_version_id: form.versionId,
        display_name: form.displayName,
        idempotency_key: form.idempotencyKey,
        source_entry_key: form.sourceEntryKey,
        source_strategy_key: form.sourceStrategyKey,
        versions: [{
          artifact_base64: utf8Base64(form.artifactContent),
          source_strategy_key: form.sourceStrategyKey,
          version_id: form.versionId,
          version_number: versionNumber,
        }],
      });
      setReceipt(result);
    } catch (reason) {
      setError(reason);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="canonical-v13-page">
      <PageHeader
        description="只提交显式 captured source envelope；INTAKE_ACCEPTED 不代表已验证、已合格或获准执行。"
        eyebrow="V1.3 canonical-only"
        title="Strategy Submission"
      />
      {!url.valid ? (
        <CanonicalStatePanel description="Submission 页面不接受 URL 选择参数。" kind="unknown" reasonCodes={url.problems} title="INVALID_URL_STATE" />
      ) : null}
      <form className="canonical-v13-panel canonical-v13-form" onSubmit={(event) => void submit(event)}>
        <h2>受控入库 envelope</h2>
        <label>调用方 identity<input required value={form.callerIdentity} onChange={(event) => field("callerIdentity", event.target.value)} /></label>
        <label>Idempotency key<input required value={form.idempotencyKey} onChange={(event) => field("idempotencyKey", event.target.value)} /></label>
        <label>展示名称<input required value={form.displayName} onChange={(event) => field("displayName", event.target.value)} /></label>
        <label>Archive snapshot SHA-256<input minLength={64} maxLength={64} pattern="[0-9a-f]{64}" required value={form.archiveDigest} onChange={(event) => field("archiveDigest", event.target.value)} /></label>
        <label>Root-relative source entry<input placeholder="archive/strategy.py" required value={form.sourceEntryKey} onChange={(event) => field("sourceEntryKey", event.target.value)} /></label>
        <label>Source strategy key<input required value={form.sourceStrategyKey} onChange={(event) => field("sourceStrategyKey", event.target.value)} /></label>
        <label>Current version identity<input required value={form.versionId} onChange={(event) => field("versionId", event.target.value)} /></label>
        <label>Current version number<input inputMode="numeric" min="1" required type="number" value={form.versionNumber} onChange={(event) => field("versionNumber", event.target.value)} /></label>
        <label className="canonical-v13-form-wide">Captured UTF-8 artifact<textarea required rows={10} value={form.artifactContent} onChange={(event) => field("artifactContent", event.target.value)} /></label>
        <button className="formal-primary-button" disabled={submitting || !url.valid} type="submit">{submitting ? "正在提交…" : "提交 canonical intake"}</button>
      </form>
      {error ? <CanonicalQueryError error={error} title="Submission 被拒绝或阻塞" /> : null}
      {receipt && !receiptContractKnown ? (
        <CanonicalStatePanel description="Submission receipt 返回未知 enum；receipt 事实与成功状态保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="Submission receipt 合同漂移" />
      ) : null}
      {receipt && receiptContractKnown ? (
        <section className="canonical-v13-panel" aria-label="Submission receipt">
          <div className="canonical-v13-heading-row"><h2>Immutable intake receipt</h2><CanonicalStatus status={receipt.intake_status} /></div>
          <div className="canonical-v13-status-grid">
            <div><span>Catalog</span><CanonicalStatus status={receipt.catalog_status} /></div>
            <div><span>Validation</span><CanonicalStatus status={receipt.validation_status} /></div>
            <div><span>Qualification</span><CanonicalStatus status={receipt.qualification_status} /></div>
            <div><span>Execution authorized</span><strong>{receipt.execution_authorized ? "是" : "否"}</strong></div>
          </div>
          <dl className="canonical-v13-definition-list">
            <div><dt>Strategy ID</dt><dd><CopyableValue value={receipt.strategy_id} /></dd></div>
            <div><dt>Receipt digest</dt><dd><CopyableValue value={receipt.receipt_digest} /></dd></div>
            <div><dt>Idempotent replay</dt><dd>{receipt.idempotent_replay ? "是" : "否"}</dd></div>
          </dl>
        </section>
      ) : null}
    </div>
  );
}
