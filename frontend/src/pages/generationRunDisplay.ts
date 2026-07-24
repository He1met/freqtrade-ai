import type { GenerationRunSummary } from "../api/types";

export type GenerationRunTone = "success" | "danger" | "warning" | "info" | "neutral";

export type GenerationRunOutcome = {
  label: string;
  tone: GenerationRunTone;
  conclusion: string;
};

function normalizedStatus(status: string): string {
  return status.trim().toLowerCase().replace(/[-\s]+/g, "_");
}

export function generationRunOutcome(run: GenerationRunSummary): GenerationRunOutcome {
  const status = normalizedStatus(run.status);

  if (status === "failed" || status === "failure") {
    return {
      label: "生成失败",
      tone: "danger",
      conclusion: run.errorMessage?.trim() || "生成任务失败，未记录错误详情。",
    };
  }

  if (status === "blocked" || status === "blocked_by_preflight") {
    return {
      label: "生成受阻",
      tone: "warning",
      conclusion: run.errorMessage?.trim() || "生成任务被阻塞，需先解除前置条件。",
    };
  }

  if (status === "running" || status === "starting") {
    return {
      label: "正在生成",
      tone: "info",
      conclusion: `已生成 ${run.generatedCount} / 请求 ${run.requestedCount}。`,
    };
  }

  if (status === "pending" || status === "queued") {
    return {
      label: "等待生成",
      tone: "info",
      conclusion: `任务已记录，等待处理 ${run.requestedCount} 个请求。`,
    };
  }

  if (status === "cancelled" || status === "stopped") {
    return {
      label: "已取消",
      tone: "neutral",
      conclusion: "任务未完成，不能作为生成成功记录。",
    };
  }

  if (status === "succeeded" || status === "success" || status === "completed") {
    if (run.generatedCount === 0) {
      return {
        label: "完成但无产出",
        tone: "warning",
        conclusion: "流程已结束，但生成数为 0，不能视为有效生成成功。",
      };
    }
    if (run.acceptedCount === 0) {
      return {
        label: "无可用策略",
        tone: "warning",
        conclusion: `已生成 ${run.generatedCount} 个，但接受数为 0。`,
      };
    }
    if (run.failedCount > 0 || run.acceptedCount < run.generatedCount) {
      return {
        label: "部分产出",
        tone: "warning",
        conclusion: `已生成 ${run.generatedCount} 个，接受 ${run.acceptedCount} 个，失败 ${run.failedCount} 个。`,
      };
    }
    return {
      label: "生成完成",
      tone: "success",
      conclusion: `已生成并接受 ${run.acceptedCount} 个策略。`,
    };
  }

  return {
    label: "状态未知",
    tone: "neutral",
    conclusion: "当前状态无法归类，请查看原始状态和批次记录。",
  };
}

export function generationRunDisplayTime(run: GenerationRunSummary): string | null {
  return run.completedAt ?? run.startedAt ?? run.createdAt ?? null;
}

export function generationRunTimeLabel(run: GenerationRunSummary): string {
  if (run.completedAt) return "完成时间";
  if (run.startedAt) return "开始时间";
  if (run.createdAt) return "创建时间";
  return "记录时间";
}
