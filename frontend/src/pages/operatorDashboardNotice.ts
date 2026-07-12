import type { DataSource } from "../api/types";

export function operatorDashboardNotice(source: DataSource): string | undefined {
  if (source === "api") {
    return "Backend API 已连接；下方状态来自只读运行契约。";
  }

  if (source === "fixture") {
    return "已显式启用开发 fixture；未使用 Backend API 数据，不能作为运行验收依据。";
  }

  return undefined;
}
