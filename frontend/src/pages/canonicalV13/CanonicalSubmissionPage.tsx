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

function createEmptyForm(): SubmissionForm {
  const versionId = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : "";
  return {
    archiveDigest: "",
    artifactContent: "",
    callerIdentity: "",
    displayName: "",
    idempotencyKey: "",
    sourceEntryKey: "",
    sourceStrategyKey: "",
    versionId,
    versionNumber: "",
  };
}

function utf8Base64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return window.btoa(binary);
}

export function CanonicalSubmissionPage() {
  const [searchParams] = useSearchParams();
  const url = parseCanonicalUrlState("submission", searchParams);
  const [form, setForm] = useState<SubmissionForm>(createEmptyForm);
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
        title="策略受控入库"
      />
      {!url.valid ? (
        <CanonicalStatePanel description="策略入库页面不接受 URL 选择参数。" kind="unknown" reasonCodes={url.problems} title="页面地址无效" />
      ) : null}
      <form className="canonical-v13-panel canonical-v13-form" onSubmit={(event) => void submit(event)}>
        <h2>受控入库 envelope</h2>
        <label>调用方 identity<input required value={form.callerIdentity} onChange={(event) => field("callerIdentity", event.target.value)} /></label>
        <label>Idempotency key<input required value={form.idempotencyKey} onChange={(event) => field("idempotencyKey", event.target.value)} /></label>
        <label>展示名称<input required value={form.displayName} onChange={(event) => field("displayName", event.target.value)} /></label>
        <label>Archive snapshot SHA-256<input minLength={64} maxLength={64} pattern="[0-9a-f]{64}" required value={form.archiveDigest} onChange={(event) => field("archiveDigest", event.target.value)} /></label>
        <label>Root-relative source entry<input placeholder="archive/strategy.py" required value={form.sourceEntryKey} onChange={(event) => field("sourceEntryKey", event.target.value)} /></label>
        <label>Source strategy key<input required value={form.sourceStrategyKey} onChange={(event) => field("sourceStrategyKey", event.target.value)} /></label>
        <label>Current version number<input inputMode="numeric" min="1" required type="number" value={form.versionNumber} onChange={(event) => field("versionNumber", event.target.value)} /></label>
        <details className="canonical-v13-form-wide canonical-v13-advanced-evidence"><summary>自动生成的高级请求标识</summary>
          <p>Current version identity 由浏览器为本次新对象请求生成；它不代表 API 已接收入库。</p>
          {form.versionId ? <CopyableValue label="Version identity" value={form.versionId} /> : "当前浏览器无法生成 UUID。"}
        </details>
        <label className="canonical-v13-form-wide">Captured UTF-8 artifact<textarea required rows={10} value={form.artifactContent} onChange={(event) => field("artifactContent", event.target.value)} /></label>
        <button className="formal-primary-button" disabled={submitting || !url.valid || !form.versionId} type="submit">{submitting ? "正在提交…" : "提交 canonical intake"}</button>
      </form>
      {!form.versionId ? <CanonicalStatePanel description="当前浏览器没有提供安全的 randomUUID；入库保持禁用，不要求用户手工补写。" kind="blocked" reasonCodes={["CANONICAL_ID_GENERATION_UNAVAILABLE"]} title="无法生成版本标识" /> : null}
      {error ? <CanonicalQueryError error={error} title="Submission 被拒绝或阻塞" /> : null}
      {receipt && !receiptContractKnown ? (
        <CanonicalStatePanel description="入库 receipt 返回未知 enum；receipt 事实与成功状态保持隐藏。" kind="unknown" reasonCodes={["UNKNOWN_CONTRACT_VALUE"]} title="入库回执合同漂移" />
      ) : null}
      {receipt && receiptContractKnown ? (
        <section className="canonical-v13-panel" aria-label="Submission receipt">
          <div className="canonical-v13-heading-row"><h2>不可变入库回执</h2><CanonicalStatus status={receipt.intake_status} /></div>
          <div className="canonical-v13-status-grid">
            <div><span>目录</span><CanonicalStatus status={receipt.catalog_status} /></div>
            <div><span>验证</span><CanonicalStatus status={receipt.validation_status} /></div>
            <div><span>资格</span><CanonicalStatus status={receipt.qualification_status} /></div>
            <div><span>执行授权</span><strong>{receipt.execution_authorized ? "是" : "否"}</strong></div>
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
