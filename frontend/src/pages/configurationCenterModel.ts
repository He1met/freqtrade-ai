import type { ConfigurationTypeRead, ConfigurationVersionRead } from "../api/strategyPlatformApi";

export type JsonSchema = {
  type: "object" | "array" | "string" | "integer" | "number" | "boolean" | "null";
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean;
  items?: JsonSchema;
  enum?: unknown[];
  const?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  default?: unknown;
  readOnly?: boolean;
  unit?: string;
  display_order?: number;
};

export type EditorCapability = {
  reason: string | null;
  schema: JsonSchema | null;
  writable: boolean;
};

const forbiddenFields = new Set([
  "api_key",
  "api_secret",
  "password",
  "secret",
  "secret_value",
  "passphrase",
  "private_key",
  "python_code",
  "callable_source",
  "executable_code",
]);

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function safeSchema(value: unknown): JsonSchema | null {
  const schema = record(value);
  if (!schema || !["object", "array", "string", "integer", "number", "boolean", "null"].includes(String(schema.type))) {
    return null;
  }
  if (schema.type === "object") {
    const properties = record(schema.properties);
    if (schema.additionalProperties !== false || !properties) return null;
    for (const [key, child] of Object.entries(properties)) {
      if (forbiddenFields.has(key.toLowerCase()) || !safeSchema(child)) return null;
    }
  }
  if (schema.type === "array" && !safeSchema(schema.items)) return null;
  return schema as JsonSchema;
}

export function editorCapability(type: ConfigurationTypeRead | null): EditorCapability {
  if (!type) return { writable: false, schema: null, reason: "尚未选择配置类型" };
  const capability = record(type.editor_capability);
  if (type.handler_key !== "generic-json-v1" || capability?.write_enabled !== true || capability.read_only === true) {
    return { writable: false, schema: null, reason: "该配置类型没有安装可写 handler，仅可查看历史" };
  }
  const schema = safeSchema(capability.json_schema);
  if (!schema || schema.type !== "object") {
    return { writable: false, schema: null, reason: "该配置类型缺少严格、无敏感字段的对象 schema" };
  }
  return { writable: true, schema, reason: null };
}

export function defaultValueForSchema(schema: JsonSchema): unknown {
  if (schema.const !== undefined) return structuredClone(schema.const);
  if (schema.default !== undefined) return structuredClone(schema.default);
  if (schema.enum?.length) return structuredClone(schema.enum[0]);
  if (schema.type === "object") {
    return Object.fromEntries(
      Object.entries(schema.properties ?? {}).map(([key, child]) => [key, defaultValueForSchema(child)]),
    );
  }
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number") return schema.minimum ?? 0;
  if (schema.type === "null") return null;
  return "";
}

export function versionActions(
  version: ConfigurationVersionRead | null,
  activeVersionId: number | null,
): { canActivate: boolean; canRetire: boolean; canValidate: boolean } {
  if (!version) return { canActivate: false, canRetire: false, canValidate: false };
  const active = version.id === activeVersionId;
  return {
    canValidate: version.lifecycle_status === "DRAFT",
    canActivate: version.lifecycle_status === "VALIDATED" && !active,
    canRetire: version.lifecycle_status === "VALIDATED" && !active,
  };
}

export function canCreateDraftFromVersion(
  version: ConfigurationVersionRead | null,
): boolean {
  return version === null || ["VALIDATED", "RETIRED"].includes(version.lifecycle_status);
}

export function configurationRequestId(action: string): string {
  const entropy = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `configuration-${action}-${entropy}`.slice(0, 128);
}

export function safetyCapabilities(
  type: ConfigurationTypeRead | null,
  bundleCapability: Record<string, unknown> | null,
): Array<{ key: string; value: boolean | "UNKNOWN" }> {
  const catalogCapability = record(type?.editor_capability)?.safety_capability;
  const source = record(bundleCapability) ?? record(catalogCapability);
  return [
    { key: "demo_only", value: typeof source?.demo_only === "boolean" ? source.demo_only : "UNKNOWN" },
    { key: "allow_real_funds", value: typeof source?.allow_real_funds === "boolean" ? source.allow_real_funds : "UNKNOWN" },
    { key: "single_writer_required", value: typeof source?.single_writer_required === "boolean" ? source.single_writer_required : "UNKNOWN" },
  ];
}
