import type { FormEventHandler } from "react";

import { StatusBadge } from "../../components/DisplayPrimitives";
import "../../styles/local-strategy-lab-generation-stage.css";
import type { SubmissionState } from "./EvidencePanels";
import {
  generationFormModel,
  type OperatorCredentialReadiness,
  type ProviderCredentialReadiness,
} from "./generationFormModel";
import { SubmissionStatusPanel } from "./SubmissionStatusPanel";

export function GenerationStage({
  authorizeRealProvider,
  idea,
  isSubmitting,
  onAuthorizeRealProviderChange,
  onCancel,
  onIdeaChange,
  onOperatorTokenChange,
  onSubmit,
  operatorTokenPresent,
  operatorCredentialReadiness,
  providerReadiness,
  submission,
}: {
  authorizeRealProvider: boolean;
  idea: string;
  isSubmitting: boolean;
  onAuthorizeRealProviderChange: (value: boolean) => void;
  onCancel: () => void;
  onIdeaChange: (value: string) => void;
  onOperatorTokenChange: (value: string) => void;
  onSubmit: FormEventHandler<HTMLFormElement>;
  operatorTokenPresent: boolean;
  operatorCredentialReadiness: OperatorCredentialReadiness;
  providerReadiness: ProviderCredentialReadiness;
  submission: SubmissionState;
}) {
  const model = generationFormModel({
    authorizeRealProvider,
    idea,
    isSubmitting,
    operatorTokenPresent,
    operatorCredentialReadiness,
    providerReadiness,
  });

  return (
    <div className="generation-stage" data-stage="generation">
      <form
        className="generation-stage__form"
        data-testid="generation-stage-form"
        onSubmit={onSubmit}
      >
        <div className="generation-stage__heading">
          <div>
            <span>固定提交范围</span>
            <strong>每次生成 1 个策略</strong>
          </div>
          <p>
            <code>requested_count=1</code> 是当前安全边界，不是可配置项；页面不再展示无效输入控件。
          </p>
        </div>

        <div className="generation-stage__grid">
          <label className="generation-stage__idea" htmlFor="strategy-idea">
            <span>策略构想（Strategy idea）</span>
            <small>使用自由文本描述约束；不要粘贴 API key、token 或其他凭据。</small>
            <textarea
              id="strategy-idea"
              maxLength={4000}
              minLength={1}
              onChange={(event) => onIdeaChange(event.currentTarget.value)}
              placeholder="示例：入场用 RSI 超卖；退出用均线回归；单笔风险 ≤ 1%；15m 周期；仅本地 Dry-run。"
              required
              rows={3}
              value={idea}
            />
            <ul className="generation-stage__hints" aria-label="策略构想提示">
              <li>入场条件</li>
              <li>退出条件</li>
              <li>风险限制</li>
              <li>时间周期</li>
            </ul>
          </label>

          <div className="generation-stage__prerequisites" aria-label="生成前置条件">
            <label className="generation-stage__token" htmlFor="operator-token">
              <span>本地操作授权（operator token）</span>
              <small>仅保留在当前页面内存；密码框不回显，不写入浏览器存储或日志。</small>
              <input
                aria-describedby="operator-token-state"
                autoComplete="off"
                id="operator-token"
                onChange={(event) => onOperatorTokenChange(event.currentTarget.value)}
                required
                type="password"
              />
              <div
                className="generation-stage__operator-state"
                id="operator-token-state"
                title={operatorCredentialReadiness.detail}
              >
                <strong>{model.operatorTokenLabel}</strong>
                <StatusBadge
                  label={operatorCredentialReadiness.label}
                  showRaw
                  status={
                    operatorCredentialReadiness.state === "ready"
                      ? "READY"
                      : operatorCredentialReadiness.state === "missing"
                        ? "BLOCKED"
                        : "UNKNOWN"
                  }
                />
              </div>
            </label>

            <div className="generation-stage__readiness">
              <div>
                <span>DeepSeek Keychain / Provider readiness</span>
                <small>{providerReadiness.detail}</small>
              </div>
              <StatusBadge
                label={providerReadiness.label}
                showRaw
                status={
                  providerReadiness.state === "ready"
                    ? "READY"
                    : providerReadiness.state === "missing"
                      ? "BLOCKED"
                      : "UNKNOWN"
                }
              />
            </div>

            <label className="generation-stage__provider-call" htmlFor="provider-authorization">
              <input
                checked={authorizeRealProvider}
                disabled={isSubmitting}
                id="provider-authorization"
                onChange={(event) => onAuthorizeRealProviderChange(event.currentTarget.checked)}
                type="checkbox"
              />
              <span>
                本次提交调用真实 Provider
                <small>一次性授权，提交发出后立即复位；不授权刷新、Dry-run、实盘或下单。</small>
              </span>
              <strong>{model.providerCallLabel}</strong>
            </label>
          </div>
        </div>

        <div className="generation-stage__submit">
          <button
            aria-busy={isSubmitting}
            className="primary-button"
            data-action-id="lab.generation.submit"
            disabled={!model.canSubmit}
            type="submit"
          >
            {isSubmitting ? "提交中" : "提交生成"}
          </button>
          {isSubmitting ? (
            <button
              className="secondary-button"
              data-action-id="lab.generation.cancel"
              onClick={onCancel}
              type="button"
            >
              取消等待
            </button>
          ) : null}
          <div aria-live="polite" className="generation-stage__submit-reasons">
            <strong>{model.canSubmit ? "可以提交" : "暂不能提交"}</strong>
            {model.canSubmit ? (
              <span>将提交 1 个策略；只有真实 Provider 调用才使用上方一次性授权。</span>
            ) : (
              <ul>
                {model.disabledReasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            )}
          </div>
        </div>

        <p className="generation-stage__timeout">
          超时或取消不会显示为成功；请用 API/DB 持久证据确认是否已经生成记录。
        </p>
      </form>

      <SubmissionStatusPanel submission={submission} />
    </div>
  );
}
