import { useEffect } from "react";

import type { DataSource, MvpData } from "../../api/types";
import { StatusBadge } from "../../components/DisplayPrimitives";
import { displayStatus } from "../uiCopy";
import {
  deriveLabWorkflow,
  type LabPhase,
} from "./workflowState";

export function WorkflowNavigator({
  data,
  dryRunSource,
  error,
  inspectedPhase,
  isLoading,
  onInspectedPhaseChange,
}: {
  data: MvpData;
  dryRunSource: DataSource;
  error: string | null;
  inspectedPhase: LabPhase;
  isLoading: boolean;
  onInspectedPhaseChange: (stage: LabPhase) => void;
}) {
  const workflow = deriveLabWorkflow(data, { dryRunSource, error, isLoading });
  const currentStage = workflow.stages.find((stage) => stage.id === workflow.currentPhase)!;
  const inspectedStage = workflow.stages.find((stage) => stage.id === inspectedPhase) ?? currentStage;
  const isReviewing = inspectedStage.id !== currentStage.id;

  useEffect(() => {
    onInspectedPhaseChange(workflow.currentPhase);
  }, [onInspectedPhaseChange, workflow.currentPhase]);

  function inspectPhase(stageId: LabPhase) {
    onInspectedPhaseChange(stageId);
  }

  return (
    <section
      aria-label="策略实验任务流"
      className="lab-workflow"
      data-current-stage={workflow.currentPhase}
      data-testid="lab-workflow"
    >
      <ol className="lab-workflow__steps">
        {workflow.stages.map((stage, index) => (
          <li
            className={`lab-workflow__step is-${stage.progress}`}
            data-progress={stage.progress}
            key={stage.id}
          >
            <button
              aria-current={stage.id === workflow.currentPhase ? "step" : undefined}
              aria-pressed={stage.id === inspectedStage.id}
              disabled={stage.progress === "locked"}
              onClick={() => inspectPhase(stage.id)}
              type="button"
            >
              <span className="lab-workflow__index">{index + 1}</span>
              <span>
                <strong>{stage.label}</strong>
                <small>
                  {stage.progress === "completed"
                    ? "已完成，可回看"
                    : stage.progress === "locked"
                      ? "等待前置阶段"
                      : "当前阶段"}
                </small>
              </span>
              <StatusBadge
                label={stage.progress === "locked" ? "未解锁" : displayStatus(stage.state)}
                showRaw={stage.progress !== "locked"}
                status={stage.progress === "locked" ? "BLOCKED" : stage.state}
              />
            </button>
          </li>
        ))}
      </ol>

      <div className="lab-workflow__decision" role="status">
        <div>
          <span>当前阶段</span>
          <strong>{currentStage.label}</strong>
        </div>
        <div>
          <span>当前结论 / 阻断原因</span>
          <strong>{currentStage.reason}</strong>
        </div>
        <div>
          <span>唯一推荐下一步</span>
          <strong>{currentStage.nextAction}</strong>
        </div>
      </div>

      {isReviewing ? (
        <div className="lab-workflow__review" role="note">
          <span>正在回看</span>
          <strong>{inspectedStage.label}</strong>
          <span>{inspectedStage.reason}</span>
        </div>
      ) : null}

      <aside className="lab-workflow__safety">
        仅允许本地研究与受控 Dry-run；禁止 live trading、连接真实交易执行链路和提交真实订单。
      </aside>
    </section>
  );
}
