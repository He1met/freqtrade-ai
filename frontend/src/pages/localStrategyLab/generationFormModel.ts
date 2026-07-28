import type {
  DataSource,
  OperatorDashboardSummary,
} from "../../api/types";

export type ProviderCredentialReadiness = {
  state: "ready" | "missing" | "unknown";
  label: string;
  detail: string;
};

export type OperatorCredentialReadiness = ProviderCredentialReadiness;

export type GenerationFormModel = {
  canSubmit: boolean;
  disabledReasons: string[];
  operatorTokenLabel: string;
  providerCallLabel: string;
  requestedCount: 1;
};

export function deriveProviderCredentialReadiness(
  dashboard: OperatorDashboardSummary,
  source: DataSource,
): ProviderCredentialReadiness {
  if (source !== "api") {
    return {
      state: "unknown",
      label: "未由真实 API 确认",
      detail: "页面不会读取 Keychain 或密钥值；请先恢复 Operator Status API 的凭据 presence 结果。",
    };
  }

  const credential = dashboard.operatorStatus.envPresence.find(
    (entry) => entry.name === "DEEPSEEK_API_KEY",
  );
  if (dashboard.operatorStatus.safety.reportsEnvValues !== false) {
    return {
      state: "unknown",
      label: "Provider readiness 未确认",
      detail: "Operator Status API 未显式确认 reports_env_values=false；页面拒绝使用可能泄露凭据值的状态。",
    };
  }
  if (!credential || credential.source !== "env" || credential.valueRendered !== false) {
    return {
      state: "unknown",
      label: "Provider readiness 未确认",
      detail: !credential
        ? "Operator Status API 未返回 DEEPSEEK_API_KEY presence；页面不会猜测 Keychain 状态。"
        : credential.source !== "env"
          ? "DEEPSEEK_API_KEY presence 不是来自明确的 env 状态源；fixture 或未知来源不可作为 Provider readiness。"
          : "状态源未显式确认 value_rendered=false；页面已拒绝使用该结果。",
    };
  }
  if (!credential.present) {
    return {
      state: "missing",
      label: "Provider 凭据未就绪",
      detail: "后端未检测到 DEEPSEEK_API_KEY。请先通过本机 Keychain/运行环境注入，再刷新状态。",
    };
  }
  return {
    state: "ready",
    label: "Provider 凭据已就绪",
    detail: "真实 API 仅确认凭据存在，未返回或展示任何密钥值。",
  };
}

export function deriveOperatorCredentialReadiness(
  dashboard: OperatorDashboardSummary,
  source: DataSource,
): OperatorCredentialReadiness {
  if (source !== "api") {
    return {
      state: "unknown",
      label: "Operator 授权未确认",
      detail: "必须由真实 Operator Status API 确认本地授权凭据存在。",
    };
  }
  const credential = dashboard.operatorStatus.envPresence.find(
    (entry) => entry.name === "FREQTRADE_AI_OPERATOR_TOKEN",
  );
  if (
    dashboard.operatorStatus.safety.reportsEnvValues !== false ||
    !credential ||
    credential.source !== "env" ||
    credential.valueRendered !== false
  ) {
    return {
      state: "unknown",
      label: "Operator 授权未确认",
      detail: "后端未返回安全、仅 presence 的 operator token 状态。",
    };
  }
  if (!credential.present) {
    return {
      state: "missing",
      label: "Operator 授权未配置",
      detail: "请先运行 make operator-token-init，并受控重启唯一运行环境。",
    };
  }
  return {
    state: "ready",
    label: "Operator 授权已配置",
    detail: "后端仅确认 Keychain 凭据存在；页面不会读取或展示 token。",
  };
}

export function generationFormModel({
  authorizeRealProvider,
  idea,
  isSubmitting,
  operatorTokenPresent,
  operatorCredentialReadiness,
  providerReadiness,
}: {
  authorizeRealProvider: boolean;
  idea: string;
  isSubmitting: boolean;
  operatorTokenPresent: boolean;
  operatorCredentialReadiness: OperatorCredentialReadiness;
  providerReadiness: ProviderCredentialReadiness;
}): GenerationFormModel {
  const disabledReasons: string[] = [];
  if (isSubmitting) {
    disabledReasons.push("生成请求正在提交；请等待完成，或使用“取消等待”。");
  }
  if (!idea.trim()) {
    disabledReasons.push("填写策略构想，至少说明入场、退出或风险约束。");
  }
  if (!operatorTokenPresent) {
    disabledReasons.push("输入本次请求使用的本地 operator token。");
  }
  if (operatorCredentialReadiness.state !== "ready") {
    disabledReasons.push(
      operatorCredentialReadiness.state === "missing"
        ? "后端尚未配置 operator token；先初始化 Keychain 凭据并重启。"
        : "Operator authorization 尚未由真实 API 确认。",
    );
  }
  if (authorizeRealProvider && providerReadiness.state !== "ready") {
    disabledReasons.push(
      providerReadiness.state === "missing"
        ? "真实 Provider 凭据未就绪；先从本机 Keychain/运行环境注入并刷新。"
        : "真实 Provider readiness 尚未由 API 确认；恢复状态 API 后再授权。",
    );
  }

  return {
    canSubmit: disabledReasons.length === 0,
    disabledReasons,
    operatorTokenLabel: operatorTokenPresent ? "已输入，仅保留在本次页面会话" : "未输入",
    providerCallLabel: authorizeRealProvider ? "仅授权下一次提交" : "未授权真实 Provider 调用",
    requestedCount: 1,
  };
}
