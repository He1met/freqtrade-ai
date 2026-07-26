export function normalizePresenceSource(source: string | undefined): string {
  return source?.trim() || "unknown";
}

export function normalizeValueRendered(
  camelCase: boolean | undefined,
  snakeCase: boolean | undefined,
): boolean {
  if (camelCase === true || snakeCase === true) return true;
  return camelCase === false || snakeCase === false ? false : true;
}

export function normalizeReportsEnvValues(
  camelCase: boolean | undefined,
  snakeCase: boolean | undefined,
): boolean {
  if (camelCase === true || snakeCase === true) return true;
  return camelCase === false || snakeCase === false ? false : true;
}
