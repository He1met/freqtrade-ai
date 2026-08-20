import type { ResearchResultsProjection } from "../../api/canonicalV13Types";
import { CopyableValue } from "../../components/DisplayPrimitives";
import { CanonicalInlineReason, CanonicalStatePanel, CanonicalStatus } from "./CanonicalStatePanel";
import { canonicalMetricMatrix, canonicalScorePercent, canonicalWindowEvidenceState } from "./canonicalV13Results";

const EVIDENCE_LABEL = {
  "gate-failed": "Hard Gate 未通过",
  "gate-passed": "Hard Gate 已通过",
  "missing-result": "API 未提供窗口结果",
  "result-only": "已有结果，尚无资格 Gate 证据",
} as const;

export function CanonicalResearchResults({ projection }: { projection: ResearchResultsProjection }) {
  const scorePercent = canonicalScorePercent(projection.score?.overall_score ?? null);
  const matrix = canonicalMetricMatrix(projection.windows);
  return (
    <section className="canonical-v13-panel canonical-v13-results" aria-labelledby="canonical-results-title">
      <div className="canonical-v13-heading-row">
        <div><span className="canonical-v13-home-kicker">Exact API evidence</span><h2 id="canonical-results-title">回测与资格结果</h2></div>
        <CanonicalStatus status={projection.plan_status} />
      </div>
      <p className="canonical-v13-results-boundary">图表只展示 API 返回的持久化数值；分数、趋势与 Gate 不会被前端解释为 QUALIFIED、READY 或可运行。</p>
      {!projection.attempt ? <CanonicalStatePanel description="Exact plan 尚无 validation attempt；页面没有可展示的回测窗口。" kind="empty" title="尚无回测记录" /> : (
        <div className="canonical-v13-result-summary">
          <div><span>回测 Attempt</span><CanonicalStatus status={projection.attempt.status} /><small>第 {projection.attempt.attempt_number} 次 · {projection.attempt.completed_at ?? "尚未完成"}</small></div>
          <div>
            <span>API Overall score</span>
            <strong>{projection.score?.overall_score ?? "未提供"}</strong>
            {scorePercent === null ? <small>没有可绘制的 0–100 分数</small> : (
              <div aria-label="API Overall score" aria-valuemax={100} aria-valuemin={0} aria-valuenow={scorePercent} className="canonical-v13-score-meter" role="meter"><span style={{ width: `${scorePercent}%` }} /></div>
            )}
          </div>
          <div><span>Qualification</span>{projection.qualification ? <CanonicalStatus status={projection.qualification.status} /> : <strong>尚无决策</strong>}<small>仅投影 API decision</small></div>
          <div><span>研究目标</span><strong>{projection.target_key}</strong><small>{projection.windows.length} 个计划窗口</small></div>
        </div>
      )}
      {projection.qualification?.reason_code ? <CanonicalInlineReason code={projection.qualification.reason_code} /> : null}

      {projection.windows.length ? (
        <div className="canonical-v13-window-grid">
          {projection.windows.map((window) => {
            const evidenceState = canonicalWindowEvidenceState(window);
            return (
              <article data-evidence-state={evidenceState} key={window.validation_plan_window_id}>
                <div className="canonical-v13-heading-row"><h3>{window.window_key}</h3><strong>{window.required ? "必需窗口" : "可选窗口"}</strong></div>
                <p>{window.window_start} → {window.window_end}</p>
                <div className="canonical-v13-window-evidence"><span>{EVIDENCE_LABEL[evidenceState]}</span></div>
                {!window.result ? <p>Canonical API 未返回该窗口的 persisted result；不显示零值或 fallback。</p> : (
                  <details><summary>原始窗口指标</summary><pre>{JSON.stringify(window.result.metrics_json, null, 2)}</pre></details>
                )}
                {window.qualification_evidence ? (
                  <ul aria-label={`${window.window_key} Hard Gate 证据`} className="canonical-v13-gate-list">
                    {window.qualification_evidence.gates.map((gate) => (
                      <li data-passed={gate.passed ? "true" : "false"} key={gate.gate_key}>
                        <strong>{gate.gate_key}</strong>
                        <span>{gate.metric}: {gate.observed} {gate.operator} {gate.threshold}</span>
                        <b>{gate.passed ? "通过" : "未通过"}</b>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}

      {matrix.metricKeys.length ? (
        <div className="canonical-v13-table-wrap" role="region" aria-label="窗口指标对比" tabIndex={0}>
          <table className="canonical-v13-result-table"><caption>API 窗口指标对比（缺失值保持为空）</caption><thead><tr><th>窗口</th>{matrix.metricKeys.map((metric) => <th key={metric}>{metric}</th>)}</tr></thead>
            <tbody>{matrix.rows.map((row) => <tr key={row.windowKey}><th>{row.windowKey}</th>{matrix.metricKeys.map((metric) => <td key={metric}>{row.values[metric] ?? "未提供"}</td>)}</tr>)}</tbody>
          </table>
        </div>
      ) : null}

      <details className="canonical-v13-advanced-evidence"><summary>高级 Lineage 与 Receipt</summary>
        <dl className="canonical-v13-definition-list">
          <div><dt>Validation plan</dt><dd><CopyableValue value={projection.validation_plan_id} /></dd></div>
          <div><dt>Strategy version</dt><dd><CopyableValue value={projection.strategy_version_id} /></dd></div>
          <div><dt>Configuration bundle</dt><dd><CopyableValue value={projection.configuration_bundle_id} /></dd></div>
          <div><dt>Market snapshot</dt><dd><CopyableValue value={projection.market_snapshot_id} /></dd></div>
          <div><dt>Plan digest</dt><dd><CopyableValue value={projection.validation_plan_digest} /></dd></div>
          <div><dt>Bundle digest</dt><dd><CopyableValue value={projection.configuration_bundle_digest} /></dd></div>
          <div><dt>Market digest</dt><dd><CopyableValue value={projection.market_snapshot_digest} /></dd></div>
          <div><dt>Score receipt</dt><dd>{projection.score ? <CopyableValue value={projection.score.score_digest} /> : "未设置"}</dd></div>
          <div><dt>Qualification receipt</dt><dd>{projection.qualification ? <CopyableValue value={projection.qualification.decision_digest} /> : "未设置"}</dd></div>
        </dl>
      </details>
    </section>
  );
}
