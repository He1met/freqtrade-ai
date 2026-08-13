import { useCallback, useEffect, useMemo, useState } from "react";

import {
  applyConfigurationVersionAction,
  createConfigurationDraft,
  fetchConfigurationAuditEvents,
  fetchConfigurationBundleHistory,
  fetchConfigurationCatalog,
  fetchConfigurationVersionDetail,
  fetchConfigurationVersionDiff,
  fetchConfigurationVersions,
  type ConfigurationAuditEventRead,
  type ConfigurationBundleSnapshotRead,
  type ConfigurationTypeRead,
  type ConfigurationVersionDetailRead,
  type ConfigurationVersionDiffRead,
  type ConfigurationVersionListRead,
  type ConfigurationVersionRead,
} from "../api/strategyPlatformApi";
import {
  CompactText,
  EmptyState,
  FormalLoadingState,
  PageHeader,
  StatusBadge,
} from "../components/DisplayPrimitives";
import "../styles/configuration-center.css";
import {
  canCreateDraftFromVersion,
  configurationRequestId,
  defaultValueForSchema,
  editorCapability,
  safetyCapabilities,
  versionActions,
  type JsonSchema,
} from "./configurationCenterModel";
import { displayDateTime } from "./uiCopy";

type Scope = { scope_type: string; scope_key: string };
type DraftDependency = {
  depends_on_type: string;
  depends_on_version_id: number;
  relation_key: string;
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function SchemaEditor({
  label,
  onChange,
  required = false,
  schema,
  value,
}: {
  label: string;
  onChange: (value: unknown) => void;
  required?: boolean;
  schema: JsonSchema;
  value: unknown;
}) {
  const description = schema.description ? <small>{schema.description}</small> : null;
  if (schema.const !== undefined || schema.readOnly) {
    return (
      <label className="configuration-field configuration-field-readonly">
        <span>{schema.title ?? label}{required ? " *" : ""}</span>
        <output>{JSON.stringify(value)}</output>
        {description}
      </label>
    );
  }
  if (schema.type === "object") {
    const current = asRecord(value);
    const entries = Object.entries(schema.properties ?? {}).sort(
      ([, left], [, right]) => (left.display_order ?? 0) - (right.display_order ?? 0),
    );
    return (
      <fieldset className="configuration-schema-group">
        <legend>{schema.title ?? label}{required ? " *" : ""}</legend>
        {description}
        {entries.map(([key, child]) => (
          <SchemaEditor
            key={key}
            label={key}
            onChange={(next) => onChange({ ...current, [key]: next })}
            required={schema.required?.includes(key)}
            schema={child}
            value={current[key] ?? defaultValueForSchema(child)}
          />
        ))}
      </fieldset>
    );
  }
  if (schema.type === "array") {
    const current = Array.isArray(value) ? value : [];
    const itemSchema = schema.items;
    if (!itemSchema) return null;
    return (
      <fieldset className="configuration-schema-group configuration-array-field">
        <legend>{schema.title ?? label}{required ? " *" : ""}</legend>
        {description}
        {current.map((item, index) => (
          <div className="configuration-array-item" key={`${label}-${index}`}>
            <SchemaEditor
              label={`${label} ${index + 1}`}
              onChange={(next) => onChange(current.map((entry, itemIndex) => itemIndex === index ? next : entry))}
              schema={itemSchema}
              value={item}
            />
            <button
              className="configuration-link-button"
              disabled={current.length <= (schema.minItems ?? 0)}
              onClick={() => onChange(current.filter((_, itemIndex) => itemIndex !== index))}
              type="button"
            >
              删除此项
            </button>
          </div>
        ))}
        <button
          className="secondary-button"
          disabled={current.length >= (schema.maxItems ?? Number.POSITIVE_INFINITY)}
          onClick={() => onChange([...current, defaultValueForSchema(itemSchema)])}
          type="button"
        >
          新增一项
        </button>
      </fieldset>
    );
  }
  if (schema.type === "boolean") {
    return (
      <label className="configuration-field configuration-checkbox-field">
        <input checked={value === true} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
        <span>{schema.title ?? label}{required ? " *" : ""}</span>
        {description}
      </label>
    );
  }
  if (schema.type === "null") {
    return <output className="configuration-null-field">{schema.title ?? label}：null</output>;
  }
  if (schema.enum?.length) {
    return (
      <label className="configuration-field">
        <span>{schema.title ?? label}{required ? " *" : ""}</span>
        <select onChange={(event) => onChange(JSON.parse(event.target.value))} value={JSON.stringify(value)}>
          {schema.enum.map((option) => (
            <option key={JSON.stringify(option)} value={JSON.stringify(option)}>{String(option)}</option>
          ))}
        </select>
        {description}
      </label>
    );
  }
  if (schema.type === "integer" || schema.type === "number") {
    return (
      <label className="configuration-field">
        <span>{schema.title ?? label}{schema.unit ? `（${schema.unit}）` : ""}{required ? " *" : ""}</span>
        <input
          max={schema.maximum}
          min={schema.minimum}
          onChange={(event) => onChange(schema.type === "integer" ? Number.parseInt(event.target.value, 10) : Number(event.target.value))}
          step={schema.type === "integer" ? 1 : "any"}
          type="number"
          value={typeof value === "number" ? value : schema.minimum ?? 0}
        />
        {description}
      </label>
    );
  }
  return (
    <label className="configuration-field">
      <span>{schema.title ?? label}{required ? " *" : ""}</span>
      <input
        maxLength={schema.maxLength}
        minLength={schema.minLength}
        onChange={(event) => onChange(event.target.value)}
        type="text"
        value={typeof value === "string" ? value : ""}
      />
      {description}
    </label>
  );
}

export function ConfigurationCenter() {
  const [operatorToken, setOperatorToken] = useState("");
  const [accessRevision, setAccessRevision] = useState(0);
  const [revision, setRevision] = useState(0);
  const [scopeType, setScopeType] = useState("research");
  const [scopeKey, setScopeKey] = useState("production-research");
  const [catalog, setCatalog] = useState<ConfigurationTypeRead[]>([]);
  const [selectedType, setSelectedType] = useState("");
  const [versions, setVersions] = useState<ConfigurationVersionListRead | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ConfigurationVersionDetailRead | null>(null);
  const [auditEvents, setAuditEvents] = useState<ConfigurationAuditEventRead[]>([]);
  const [bundles, setBundles] = useState<ConfigurationBundleSnapshotRead[]>([]);
  const [diff, setDiff] = useState<ConfigurationVersionDiffRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [writeBusy, setWriteBusy] = useState(false);
  const [showDraft, setShowDraft] = useState(false);
  const [draftPayload, setDraftPayload] = useState<Record<string, unknown>>({});
  const [draftDependencies, setDraftDependencies] = useState<DraftDependency[]>([]);
  const [dependencyVersions, setDependencyVersions] = useState<Record<string, ConfigurationVersionRead[]>>({});
  const [changeSummary, setChangeSummary] = useState("");
  const scope = useMemo<Scope>(() => ({ scope_type: scopeType.trim(), scope_key: scopeKey.trim() }), [scopeKey, scopeType]);
  const selectedTypeRow = catalog.find((item) => item.type_key === selectedType) ?? null;
  const capability = editorCapability(selectedTypeRow);
  const selectedVersion = versions?.items.find((item) => item.id === selectedVersionId) ?? null;
  const actions = versionActions(selectedVersion, versions?.active_version_id ?? null);
  const canCreateDraft = capability.writable && canCreateDraftFromVersion(selectedVersion);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    if (!operatorToken.trim() || accessRevision === 0) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchConfigurationCatalog(operatorToken, controller.signal)
      .then((result) => {
        setCatalog(result.items);
        setSelectedType((current) => current || result.items.find((item) => item.enabled)?.type_key || "");
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorText(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [accessRevision, operatorToken]);

  useEffect(() => {
    if (!operatorToken.trim() || !selectedType || !scope.scope_type || !scope.scope_key || accessRevision === 0) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      fetchConfigurationVersions(selectedType, scope, operatorToken, controller.signal),
      fetchConfigurationAuditEvents(selectedType, scope, operatorToken, controller.signal),
      fetchConfigurationBundleHistory(scope, operatorToken, controller.signal),
    ])
      .then(([versionResult, auditResult, bundleResult]) => {
        setVersions(versionResult);
        setAuditEvents(auditResult.items);
        setBundles(bundleResult.items);
        setSelectedVersionId((current) => (
          versionResult.items.some((item) => item.id === current)
            ? current
            : versionResult.active_version_id ?? versionResult.items[0]?.id ?? null
        ));
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorText(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [accessRevision, operatorToken, revision, scope, selectedType]);

  useEffect(() => {
    if (!operatorToken.trim() || !selectedType || selectedVersionId === null) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    fetchConfigurationVersionDetail(selectedType, selectedVersionId, operatorToken, controller.signal)
      .then(setDetail)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorText(reason));
      });
    return () => controller.abort();
  }, [operatorToken, revision, selectedType, selectedVersionId]);

  useEffect(() => {
    const activeId = versions?.active_version_id;
    if (!operatorToken.trim() || !selectedType || selectedVersionId === null || !activeId || activeId === selectedVersionId) {
      setDiff(null);
      return;
    }
    const controller = new AbortController();
    fetchConfigurationVersionDiff(selectedType, selectedVersionId, activeId, scope, operatorToken, controller.signal)
      .then(setDiff)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorText(reason));
      });
    return () => controller.abort();
  }, [operatorToken, scope, selectedType, selectedVersionId, versions?.active_version_id]);

  async function beginDraft() {
    if (!capability.schema) return;
    const immutableSource = detail && ["VALIDATED", "RETIRED"].includes(detail.version.lifecycle_status)
      ? detail
      : null;
    setDraftPayload(structuredClone(immutableSource?.version.payload_json ?? defaultValueForSchema(capability.schema)) as Record<string, unknown>);
    setDraftDependencies((immutableSource?.dependencies ?? []).map((row) => ({
      depends_on_type: row.depends_on_type,
      depends_on_version_id: row.depends_on_version_id,
      relation_key: row.relation_key,
    })));
    setChangeSummary("");
    setShowDraft(true);
    try {
      const results = await Promise.all(
        catalog
          .filter((item) => item.enabled && item.type_key !== selectedType && editorCapability(item).writable)
          .map(async (item) => ({
            typeKey: item.type_key,
            versions: (await fetchConfigurationVersions(item.type_key, scope, operatorToken)).items
              .filter((version) => version.lifecycle_status === "VALIDATED"),
          })),
      );
      setDependencyVersions(Object.fromEntries(results.map((item) => [item.typeKey, item.versions])));
    } catch (reason) {
      setError(errorText(reason));
    }
  }

  async function saveDraft() {
    if (!selectedType || !changeSummary.trim()) return;
    setWriteBusy(true);
    setError(null);
    try {
      const sourceVersionId = detail && ["VALIDATED", "RETIRED"].includes(detail.version.lifecycle_status)
        ? detail.version.id
        : undefined;
      const result = await createConfigurationDraft(
        selectedType,
        {
          ...scope,
          change_summary: changeSummary.trim(),
          source_version_id: sourceVersionId,
          payload_json: draftPayload,
          dependencies: draftDependencies.map((row) => ({
            depends_on_version_id: row.depends_on_version_id,
            relation_key: row.relation_key,
          })),
        },
        operatorToken,
        configurationRequestId("create"),
      );
      setShowDraft(false);
      setSelectedVersionId(result.version.id);
      refresh();
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setWriteBusy(false);
    }
  }

  async function runAction(action: "validate" | "activate" | "retire") {
    if (!selectedType || selectedVersionId === null) return;
    setWriteBusy(true);
    setError(null);
    try {
      await applyConfigurationVersionAction(
        action,
        selectedType,
        selectedVersionId,
        { ...scope, reason: `${action} via controlled configuration center` },
        operatorToken,
        configurationRequestId(action),
      );
      refresh();
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setWriteBusy(false);
    }
  }

  const capabilitySource = bundles[0]?.capability_snapshot ?? null;
  const safety = safetyCapabilities(selectedTypeRow, capabilitySource);

  return (
    <section className="page formal-page configuration-center-page">
      <PageHeader
        actions={(
          <div className="configuration-owner-access">
            <label><span>Owner operator token</span><input autoComplete="off" onChange={(event) => setOperatorToken(event.target.value)} placeholder="仅当前页面内存" type="password" value={operatorToken} /></label>
            <button className="formal-primary-button" disabled={!operatorToken.trim()} onClick={() => setAccessRevision((value) => value + 1)} type="button">读取配置</button>
          </div>
        )}
        description="仅管理 V1.3 配置版本、依赖与 scope activation；不提供凭据、代码执行、runtime、订单或部署控制。"
        eyebrow="Strategy Platform V1.3"
        status={<StatusBadge label={loading ? "读取中" : error ? "已阻断" : accessRevision ? "Owner-only" : "等待授权"} status={loading ? "RUNNING" : error ? "BLOCKED" : accessRevision ? "AVAILABLE" : "UNKNOWN"} />}
        title="配置中心"
      />

      <section className="configuration-scope-panel" aria-label="明确配置范围">
        <div><strong>当前 scope</strong><span>所有读取和写入都使用这组显式范围，不做全局 fallback。</span></div>
        <label><span>scope_type</span><input onChange={(event) => setScopeType(event.target.value)} value={scopeType} /></label>
        <label><span>scope_key</span><input onChange={(event) => setScopeKey(event.target.value)} value={scopeKey} /></label>
        <button className="secondary-button" disabled={!operatorToken.trim()} onClick={refresh} type="button">刷新</button>
      </section>

      {error ? <aside className="configuration-error" role="alert"><strong>操作已阻断</strong><span>{error}</span></aside> : null}
      {loading && !versions ? <FormalLoadingState label="正在读取配置目录" /> : null}
      {!accessRevision ? <EmptyState title="需要 owner 授权" description="输入本地 operator token 后读取；token 不写入 localStorage、sessionStorage 或配置 payload。" /> : null}

      {accessRevision ? (
        <div className="configuration-workspace">
          <aside className="configuration-catalog" aria-label="配置目录">
            <h2>配置目录</h2>
            {catalog.map((item) => {
              const itemCapability = editorCapability(item);
              return (
                <button className={item.type_key === selectedType ? "selected" : ""} key={item.type_key} onClick={() => { setSelectedType(item.type_key); setSelectedVersionId(null); setShowDraft(false); }} type="button">
                  <span><strong>{item.name_zh}</strong><small>{item.type_key}</small></span>
                  <StatusBadge label={itemCapability.writable ? "可写" : "只读"} status={itemCapability.writable ? "AVAILABLE" : "UNKNOWN"} />
                </button>
              );
            })}
          </aside>

          <div className="configuration-main-column">
            <section className="configuration-summary-card">
              <div>
                <span className="formal-kicker">{selectedTypeRow?.type_key ?? "未选择"}</span>
                <h2>{selectedTypeRow?.name_zh ?? "配置类型"}</h2>
                <p>{selectedTypeRow?.description_zh}</p>
              </div>
              <div className="configuration-active-summary">
                <span>Active version</span>
                <strong>{versions?.active_version_id ? `#${versions.active_version_id}` : "UNKNOWN"}</strong>
                <small>{scope.scope_type} / {scope.scope_key}</small>
              </div>
            </section>

            <section className="configuration-safety-card">
              <div><h2>不可关闭的安全边界</h2><p>只读 capability；页面没有修改开关。</p></div>
              <dl>
                {safety.map((item) => <div key={item.key}><dt>{item.key}</dt><dd>{String(item.value)}</dd></div>)}
              </dl>
            </section>

            <section className="configuration-version-section">
              <div className="formal-section-heading compact">
                <div><span className="formal-kicker">Immutable history</span><h2>版本与 lifecycle</h2></div>
                <button className="formal-primary-button" disabled={!canCreateDraft || writeBusy} onClick={() => void beginDraft()} title={capability.reason ?? undefined} type="button">{selectedVersion ? "从所选版本新建草稿" : "新建首个草稿"}</button>
              </div>
              {!capability.writable ? <p className="configuration-readonly-reason">{capability.reason}</p> : null}
              <div className="configuration-version-layout">
                <div className="configuration-version-list" role="listbox" aria-label="配置版本">
                  {versions?.items.map((item) => (
                    <button aria-selected={item.id === selectedVersionId} className={item.id === selectedVersionId ? "selected" : ""} key={item.id} onClick={() => { setSelectedVersionId(item.id); setShowDraft(false); }} role="option" type="button">
                      <span><strong>v{item.version_number}</strong><small>#{item.id}</small></span>
                      <StatusBadge label={item.id === versions.active_version_id ? `${item.lifecycle_status} · ACTIVE` : item.lifecycle_status} status={item.lifecycle_status} />
                    </button>
                  ))}
                </div>
                <div className="configuration-version-detail">
                  {detail ? (
                    <>
                      <div className="configuration-version-heading">
                        <div><h3>v{detail.version.version_number} · #{detail.version.id}</h3><p>{detail.version.change_summary ?? "无变更说明"}</p></div>
                        <StatusBadge status={detail.version.lifecycle_status} />
                      </div>
                      <dl className="configuration-metadata-grid">
                        <div><dt>schema</dt><dd>{detail.version.schema_version}</dd></div>
                        <div><dt>created by</dt><dd>{detail.version.created_by}</dd></div>
                        <div><dt>created at</dt><dd>{displayDateTime(detail.version.created_at)}</dd></div>
                        <div><dt>validated at</dt><dd>{displayDateTime(detail.version.validated_at)}</dd></div>
                        <div className="wide"><dt>digest</dt><dd><CompactText mono value={detail.version.config_digest} /></dd></div>
                      </dl>
                      <div className="configuration-action-row">
                        <button className="secondary-button" disabled={!capability.writable || !actions.canValidate || writeBusy} onClick={() => void runAction("validate")} type="button">校验草稿</button>
                        <button className="formal-primary-button" disabled={!capability.writable || !actions.canActivate || writeBusy} onClick={() => void runAction("activate")} type="button">原子切换 activation</button>
                        <button className="configuration-danger-button" disabled={!capability.writable || !actions.canRetire || writeBusy} onClick={() => void runAction("retire")} type="button">退役版本</button>
                      </div>
                      <details className="configuration-payload-preview"><summary>查看不可变 payload</summary><pre>{JSON.stringify(detail.version.payload_json, null, 2)}</pre></details>
                    </>
                  ) : <EmptyState title="没有版本详情" description="选择一个版本查看 lifecycle、digest 与依赖。" />}
                </div>
              </div>
            </section>

            {showDraft && capability.schema ? (
              <section className="configuration-draft-editor">
                <div className="formal-section-heading"><div><span className="formal-kicker">DRAFT only</span><h2>新建草稿</h2></div><button className="configuration-link-button" onClick={() => setShowDraft(false)} type="button">取消</button></div>
                <p>字段由后端 schema 渲染；未知字段、secret、可执行代码和安全边界削弱会被后端拒绝。</p>
                <label className="configuration-field"><span>变更说明 *</span><textarea maxLength={4000} onChange={(event) => setChangeSummary(event.target.value)} rows={3} value={changeSummary} /></label>
                <SchemaEditor label="payload" onChange={(value) => setDraftPayload(asRecord(value))} schema={capability.schema} value={draftPayload} />
                <div className="configuration-dependency-editor">
                  <h3>精确版本依赖</h3>
                  {draftDependencies.length ? draftDependencies.map((row, index) => {
                    const typeOptions = Object.entries(dependencyVersions).filter(([, items]) => items.length);
                    const versionOptions = dependencyVersions[row.depends_on_type] ?? [];
                    return (
                      <div className="configuration-dependency-row" key={`${index}-${row.relation_key}-${row.depends_on_version_id}`}>
                        <label><span>relation_key</span><input maxLength={120} onChange={(event) => setDraftDependencies((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, relation_key: event.target.value } : item))} value={row.relation_key} /></label>
                        <label><span>配置类型</span><select onChange={(event) => { const typeKey = event.target.value; const first = dependencyVersions[typeKey]?.[0]; if (first) setDraftDependencies((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, depends_on_type: typeKey, depends_on_version_id: first.id } : item)); }} value={row.depends_on_type}>{typeOptions.map(([typeKey]) => <option key={typeKey} value={typeKey}>{typeKey}</option>)}</select></label>
                        <label><span>精确版本</span><select onChange={(event) => setDraftDependencies((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, depends_on_version_id: Number(event.target.value) } : item))} value={row.depends_on_version_id}>{versionOptions.map((version) => <option key={version.id} value={version.id}>v{version.version_number} · #{version.id}</option>)}</select></label>
                        <button className="configuration-link-button" onClick={() => setDraftDependencies((current) => current.filter((_, itemIndex) => itemIndex !== index))} type="button">移除</button>
                      </div>
                    );
                  }) : <p>此草稿不声明依赖。</p>}
                  <button className="secondary-button" disabled={!Object.values(dependencyVersions).some((items) => items.length)} onClick={() => { const entry = Object.entries(dependencyVersions).find(([, items]) => items.length); if (!entry) return; setDraftDependencies((current) => [...current, { depends_on_type: entry[0], depends_on_version_id: entry[1][0].id, relation_key: entry[0] }]); }} type="button">新增精确依赖</button>
                  <small>只允许从当前 scope 可读的 `VALIDATED` 版本中选择；激活前后端仍会检查完整闭包、类型冲突与 scope binding。</small>
                </div>
                <button className="formal-primary-button" disabled={!changeSummary.trim() || writeBusy} onClick={() => void saveDraft()} type="button">{writeBusy ? "正在提交…" : "创建不可变草稿版本"}</button>
              </section>
            ) : null}

            <section className="configuration-evidence-grid">
              <div>
                <h2>直接依赖</h2>
                {detail?.dependencies.length ? detail.dependencies.map((row) => <div className="configuration-evidence-row" key={`${row.relation_key}-${row.depends_on_version_id}`}><span>{row.relation_key}</span><strong>{row.depends_on_type} #{row.depends_on_version_id}</strong></div>) : <p>所选版本没有直接依赖。</p>}
              </div>
              <div>
                <h2>相对 active 的结构化差异</h2>
                {diff?.items.length ? diff.items.map((item) => <div className="configuration-diff-row" key={item.path}><code>{item.path}</code><span>{JSON.stringify(item.before)} → {JSON.stringify(item.after)}</span></div>) : <p>{selectedVersionId === versions?.active_version_id ? "所选版本就是当前 active。" : "没有结构化差异或尚未选择版本。"}</p>}
              </div>
            </section>

            <section className="configuration-evidence-grid">
              <div>
                <h2>审计时间线</h2>
                {auditEvents.length ? auditEvents.map((event) => <article className="configuration-audit-row" key={event.id}><StatusBadge status={event.event_type} /><div><strong>version #{event.configuration_version_id}</strong><span>{event.actor} · {displayDateTime(event.created_at)}</span><small>{event.reason ?? event.request_id}</small></div></article>) : <p>此 scope 暂无配置写审计。</p>}
              </div>
              <div>
                <h2>不可变任务 snapshots</h2>
                {bundles.length ? bundles.map((bundle) => <article className="configuration-bundle-row" key={bundle.snapshot_id}><div><strong>snapshot #{bundle.snapshot_id}</strong><span>{bundle.workflow_kind} · {displayDateTime(bundle.created_at)}</span></div><CompactText mono value={bundle.bundle_digest} /></article>) : <p>此 scope 暂无任务锁定 snapshot；activation 不会伪造任务 snapshot。</p>}
              </div>
            </section>
          </div>
        </div>
      ) : null}
    </section>
  );
}
