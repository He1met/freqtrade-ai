export function requiredDryRunTargetField(
  value: unknown,
  field: "pair" | "timeframe" | "exchange",
): string {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (!normalized) {
    throw new Error(`Dry-run 请求缺少显式 ${field}；未发送 API 请求。`);
  }
  return normalized;
}
