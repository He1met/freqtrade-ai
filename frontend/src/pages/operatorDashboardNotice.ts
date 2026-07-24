import type { DataSource } from "../api/types";

export function operatorDashboardNotice(source: DataSource): string | undefined {
  if (source === "api") {
    return "只读运行契约来自 Backend API；API 已连接不等于运行流程成功。";
  }

  if (source === "fixture") {
    return "运维状态来自显式开发 fixture，不能作为真实运行验收依据。";
  }

  return undefined;
}
