import { useEffect, useId, useMemo, useState } from "react";

import { canonicalStatusGuidance } from "./canonicalV13Model";
import { canonicalSelectionState, filterCanonicalSelectorOptions, type CanonicalSelectorOption } from "./canonicalV13Selectors";

export type CanonicalSelectorAvailability = "empty" | "loading" | "ready" | "unavailable";

export function CanonicalSearchSelect({
  availability,
  disabled = false,
  label,
  onChange,
  options,
  placeholder = "请选择",
  value,
}: {
  availability: CanonicalSelectorAvailability;
  disabled?: boolean;
  label: string;
  onChange: (value: string | null) => void;
  options: readonly CanonicalSelectorOption[];
  placeholder?: string;
  value: string;
}) {
  const id = useId();
  const [query, setQuery] = useState("");
  const state = canonicalSelectionState(options, value);
  const selected = options.find((option) => option.value === value) ?? null;
  const filtered = useMemo(() => {
    const matches = filterCanonicalSelectorOptions(options, query);
    if (selected && !matches.some((option) => option.value === selected.value)) return [selected, ...matches];
    return matches;
  }, [options, query, selected]);
  const unavailable = disabled || availability !== "ready" || options.length === 0;
  useEffect(() => { setQuery(""); }, [label, options]);

  const placeholderText = availability === "loading" ? "正在加载 API 选项…"
    : availability === "unavailable" ? "API 选项暂不可用"
      : availability === "empty" || options.length === 0 ? "API 未返回可选对象"
        : state === "stale" ? "当前 URL selection 已失效"
          : placeholder;

  return (
    <div className="canonical-v13-search-select" data-selection-state={state}>
      <label htmlFor={`${id}-search`}>搜索{label}</label>
      <input
        disabled={unavailable}
        id={`${id}-search`}
        onChange={(event) => setQuery(event.target.value)}
        placeholder={`按${label}名称或状态搜索`}
        type="search"
        value={query}
      />
      <label htmlFor={`${id}-select`}>{label}</label>
      <select
        aria-invalid={state === "stale" ? "true" : undefined}
        disabled={unavailable}
        id={`${id}-select`}
        onChange={(event) => onChange(event.target.value || null)}
        value={state === "selected" ? value : ""}
      >
        <option value="">{placeholderText}</option>
        {filtered.map((option) => {
          const status = option.status ? canonicalStatusGuidance(option.status).label : null;
          return <option key={option.value} value={option.value}>{option.label}{status ? ` · ${status}` : ""} · {option.description}</option>;
        })}
      </select>
      <span aria-live="polite" className="canonical-v13-selector-count">{availability === "ready" ? `${filtered.length} 个匹配 API 选项` : placeholderText}</span>
      {selected ? (
        <div className="canonical-v13-selector-context">
          <span>{selected.description}</span>
          {selected.status ? <span>{canonicalStatusGuidance(selected.status).label}</span> : null}
          <details><summary>高级标识</summary><code>{selected.value}</code></details>
        </div>
      ) : null}
      {state === "stale" ? (
        <div className="canonical-v13-selector-stale" role="status">
          <span>当前 URL 中的对象不在最新 API 选项内；页面不会自动改选第一项。</span>
          <details><summary>查看失效标识</summary><code>{value}</code></details>
        </div>
      ) : null}
    </div>
  );
}
