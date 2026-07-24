import type {
  OperatorDiagnosticCheck,
  OperatorEnvPresence,
  OperatorStatusReportSummary,
  RuntimeReadOnlyContractSummary,
  RuntimeStatusSummary,
} from "../api/types";

const HEALTHY_STATUSES = new Set(["READY", "OK", "PASS", "PASSED", "ACCEPTED", "SUCCESS", "PRESENT"]);
const PROBLEM_STATUS_PRIORITY: Record<string, number> = {
  FAILED: 70,
  FAILURE: 70,
  REJECTED: 70,
  BLOCKED: 60,
  UNAVAILABLE: 50,
  STALE: 40,
  WARNING: 30,
  MISSING: 20,
  UNKNOWN: 10,
};

function normalizedStatus(status: string | null | undefined): string {
  return status?.trim().toUpperCase() || "UNKNOWN";
}

export function operatorStatusPriority(status: string | null | undefined): number {
  const normalized = normalizedStatus(status);
  if (HEALTHY_STATUSES.has(normalized)) {
    return 0;
  }
  return PROBLEM_STATUS_PRIORITY[normalized] ?? 10;
}

export function isOperatorProblemStatus(status: string | null | undefined): boolean {
  return operatorStatusPriority(status) > 0;
}

export function sortOperatorDiagnostics(checks: OperatorDiagnosticCheck[]): OperatorDiagnosticCheck[] {
  return [...checks].sort((left, right) => {
    const severityDifference =
      operatorStatusPriority(right.status) - operatorStatusPriority(left.status);
    if (severityDifference !== 0) {
      return severityDifference;
    }
    if (left.required !== right.required) {
      return left.required ? -1 : 1;
    }
    return left.name.localeCompare(right.name, "zh-CN");
  });
}

export function operatorDiagnosticCounts(checks: OperatorDiagnosticCheck[]) {
  const counts = {
    failed: 0,
    blocked: 0,
    unavailable: 0,
    stale: 0,
    warning: 0,
    otherProblem: 0,
    totalProblems: 0,
  };

  for (const check of checks) {
    const status = normalizedStatus(check.status);
    if (!isOperatorProblemStatus(status)) {
      continue;
    }
    counts.totalProblems += 1;
    if (["FAILED", "FAILURE", "REJECTED"].includes(status)) {
      counts.failed += 1;
    } else if (status === "BLOCKED") {
      counts.blocked += 1;
    } else if (status === "UNAVAILABLE") {
      counts.unavailable += 1;
    } else if (status === "STALE") {
      counts.stale += 1;
    } else if (status === "WARNING") {
      counts.warning += 1;
    } else {
      counts.otherProblem += 1;
    }
  }

  return counts;
}

export function operatorDiagnosticReason(check: OperatorDiagnosticCheck): string | null {
  return (
    check.blockedReason?.trim() ||
    check.unavailableReason?.trim() ||
    check.warnings.find((warning) => warning.trim())?.trim() ||
    (isOperatorProblemStatus(check.status) ? check.summary.trim() || null : null)
  );
}

export function runtimeStatusReason(status: RuntimeStatusSummary): string | null {
  return (
    status.blockedReason?.trim() ||
    status.unavailableReason?.trim() ||
    status.staleReason?.trim() ||
    status.warnings.find((warning) => warning.trim())?.trim() ||
    null
  );
}

export type OperatorSystemConclusion = {
  status: string;
  label: string;
  reason: string | null;
};

export function operatorSystemConclusion(
  runtimeContract: RuntimeReadOnlyContractSummary,
  operatorStatus: OperatorStatusReportSummary,
): OperatorSystemConclusion {
  const candidates = [
    {
      status: runtimeContract.status,
      reason:
        runtimeContract.blockedReasons.find((reason) => reason.trim()) ??
        runtimeContract.unavailableReasons.find((reason) => reason.trim()) ??
        runtimeStatusReason(runtimeContract.systemStatus),
    },
    {
      status: runtimeContract.systemStatus.status,
      reason: runtimeStatusReason(runtimeContract.systemStatus),
    },
    {
      status: operatorStatus.status,
      reason:
        operatorStatus.blockedReasons.find((reason) => reason.trim()) ??
        operatorStatus.unavailableReasons.find((reason) => reason.trim()) ??
        operatorStatus.warnings.find((warning) => warning.trim()) ??
        null,
    },
  ];
  const worst = candidates.reduce((current, candidate) =>
    operatorStatusPriority(candidate.status) > operatorStatusPriority(current.status)
      ? candidate
      : current,
  );

  return {
    status: worst.status,
    label: isOperatorProblemStatus(worst.status) ? "系统当前不可验收" : "系统当前可用",
    reason: worst.reason,
  };
}

export function safetyBoundaryViolations(
  runtimeContract: RuntimeReadOnlyContractSummary,
  operatorStatus: OperatorStatusReportSummary,
): string[] {
  const violations: string[] = [];
  if (!runtimeContract.safety.readOnly || !operatorStatus.safety.readOnly) {
    violations.push("Dashboard 未确认只读");
  }
  if (operatorStatus.safety.reportsEnvValues) {
    violations.push("报告声称会展示 ENV 值");
  }
  if (runtimeContract.safety.allowLiveTrading || operatorStatus.safety.allowLiveTrading) {
    violations.push("允许 Live trading");
  }
  if (runtimeContract.safety.allowRealOrders || operatorStatus.safety.allowRealOrders) {
    violations.push("允许真实订单");
  }
  if (
    runtimeContract.safety.allowExchangeConnection ||
    operatorStatus.safety.allowExchangeConnection
  ) {
    violations.push("允许交易所连接");
  }
  if (runtimeContract.safety.allowDeployControl || operatorStatus.safety.allowDeployControl) {
    violations.push("允许部署控制");
  }
  if (runtimeContract.safety.canStartStopBot || operatorStatus.safety.canStartStopBot) {
    violations.push("允许启动或停止机器人");
  }
  return violations;
}

export function environmentContractViolations(envPresence: OperatorEnvPresence[]): string[] {
  return envPresence
    .filter((item) => item.valueRendered)
    .map((item) => `${item.name}：报告声称值已渲染`);
}
