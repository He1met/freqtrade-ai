/**
 * Product UX contract for Issue #556.
 *
 * This module is intentionally frontend-only. It describes what an element is
 * for and how much of it belongs in the default surface; it does not derive or
 * change backend business state.
 */

export const ROUTE_IDS = [
  "global",
  "dashboard",
  "strategies",
  "strategy-detail",
  "generation-runs",
  "local-strategy-lab",
  "backtest-runs",
  "backtest-tasks",
  "hyperopt-runs",
  "ranking",
  "operator-dashboard",
  "okx-demo",
  "live-governance",
  "freq-ui",
  "not-found",
] as const;

export type RouteId = (typeof ROUTE_IDS)[number];
export type DisclosureLevel = "default" | "advanced-diagnostic";
export type ElementKind =
  | "navigation"
  | "action"
  | "form"
  | "data"
  | "status"
  | "state"
  | "disclosure"
  | "diagnostic";
export type Audience = "普通用户" | "高级用户" | "运维人员" | "开发/QA";
export type ElementDisposition = "retain" | "merge" | "rename" | "hide" | "delete";
export type DangerLevel = "none" | "low" | "medium" | "high" | "critical";
export type ActionAvailability = "available" | "conditional";

export type FreshnessPolicy = {
  source: string;
  update_rule: string;
  stale_label: string;
};

/**
 * Stable action contract. `verb` and `object` make the action label auditable
 * instead of relying on an arbitrary button caption.
 */
export type ActionDescriptor = {
  action_id: string;
  verb: string;
  object: string;
  action_label: string;
  target: string;
  boundary: string;
  prerequisite: string;
  expected_result: string;
  failure_impact: string;
  reversible: boolean;
  danger_level: DangerLevel;
  availability: ActionAvailability;
  disabled_reason: string | null;
  next_action: string;
};

/** A data element is useful only when it leads to a user decision. */
export type DecisionValue = {
  decision_question: string;
  value_label: string;
  freshness: FreshnessPolicy;
  anomaly_advice: string;
  default_visibility: DisclosureLevel;
};

export type ElementPurposeEntry = {
  element_id: string;
  route: RouteId;
  component: string;
  kind: ElementKind;
  target_user: Audience;
  purpose: string;
  why: string;
  user_decision: string;
  trigger_result: string;
  prerequisite: string;
  failure_impact: string;
  recoverability: string;
  disposition: ElementDisposition;
  disclosure: DisclosureLevel;
  accessible_name: string;
  technical_only: boolean;
  default_visible: boolean;
  primary: boolean;
  primary_context: string | null;
  action?: ActionDescriptor;
  decision_value?: DecisionValue;
};

export type RoutePurposeContract = {
  route: RouteId;
  path: string;
  label: string;
  purpose: string;
  why: string;
  default_decision: string;
  default_next_action: string;
  primary_scope: "none" | "page" | "stage";
  default_primary_action_id: string | null;
};

const freshness = (
  source: string,
  update_rule: string,
  stale_label = "无法确认新鲜度时显示为未知，并给出刷新建议。",
): FreshnessPolicy => ({ source, update_rule, stale_label });

const decision = (
  decision_question: string,
  value_label: string,
  freshness_policy: FreshnessPolicy,
  anomaly_advice: string,
  default_visibility: DisclosureLevel = "default",
): DecisionValue => ({
  decision_question,
  value_label,
  freshness: freshness_policy,
  anomaly_advice,
  default_visibility,
});

const action = (descriptor: ActionDescriptor): ActionDescriptor => descriptor;

export const actionDescriptors: Readonly<Record<string, ActionDescriptor>> = {
  "global.navigation.open-page": action({
    action_id: "global.navigation.open-page",
    verb: "打开",
    object: "页面",
    action_label: "打开页面",
    target: "指定产品页面",
    boundary: "仅改变前端路由，不写入业务状态。",
    prerequisite: "目标路由存在。",
    expected_result: "进入目标页面并显示该页面的当前结论。",
    failure_impact: "无法到达目标页面，但不会改变研究或运行状态。",
    reversible: true,
    danger_level: "none",
    availability: "available",
    disabled_reason: null,
    next_action: "检查目标路径或使用总览入口。",
  }),
  "global.mobile-nav.toggle": action({
    action_id: "global.mobile-nav.toggle",
    verb: "打开",
    object: "主导航",
    action_label: "打开主导航",
    target: "当前页面的产品导航",
    boundary: "仅展开或收起导航，不触发页面动作。",
    prerequisite: "页面 shell 已加载。",
    expected_result: "看到当前页面和可用页面入口。",
    failure_impact: "当前页面仍可使用，但移动端导航入口不可见。",
    reversible: true,
    danger_level: "none",
    availability: "available",
    disabled_reason: null,
    next_action: "使用桌面导航或直接访问已知路径。",
  }),
  "global.copy.value": action({
    action_id: "global.copy.value",
    verb: "复制",
    object: "值",
    action_label: "复制值",
    target: "当前可见的 ID、引用或诊断值",
    boundary: "只复制已经展示的值，不读取密钥或隐藏环境变量。",
    prerequisite: "值存在且 Clipboard API 可用。",
    expected_result: "值写入系统剪贴板并给出成功或失败反馈。",
    failure_impact: "复制失败不改变原始记录。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "值为空或浏览器不提供 Clipboard API 时禁用。",
    next_action: "手动选择可见值，或检查浏览器剪贴板权限。",
  }),
  "global.disclosure.open-diagnostic": action({
    action_id: "global.disclosure.open-diagnostic",
    verb: "展开",
    object: "诊断详情",
    action_label: "展开诊断详情",
    target: "当前元素的高级审计字段",
    boundary: "只读展开 database IDs、artifact 引用、原始状态或脱敏日志。",
    prerequisite: "页面已提供对应的诊断证据。",
    expected_result: "看到完整但受控的诊断上下文。",
    failure_impact: "默认决策信息仍可见，但审计上下文暂不可展开。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "没有诊断证据时不显示展开入口。",
    next_action: "先恢复数据来源，再重新加载诊断详情。",
  }),
  "strategies.open-detail": action({
    action_id: "strategies.open-detail",
    verb: "查看",
    object: "策略详情",
    action_label: "查看策略详情",
    target: "指定策略的当前版本与可追溯证据",
    boundary: "仅导航到只读策略详情，不修改策略。",
    prerequisite: "列表返回有效策略标识。",
    expected_result: "看到策略当前状态、版本和下一步。",
    failure_impact: "无法继续核对该策略，但不会改变策略记录。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "缺少策略标识时不显示详情入口。",
    next_action: "刷新策略列表或回到总览。",
  }),
  "lab.generation.submit": action({
    action_id: "lab.generation.submit",
    verb: "提交",
    object: "策略生成",
    action_label: "提交策略生成",
    target: "当前策略构想的单次本地生成请求",
    boundary: "每次只提交 1 个策略；不启动 Dry-run、Live 或真实订单。",
    prerequisite: "策略构想、operator authorization 和真实 Provider readiness 均满足当前表单要求。",
    expected_result: "创建可追踪的 generation run，并回显持久化证据或明确阻断。",
    failure_impact: "生成请求未被证明成功；页面保留 BLOCKED/FAILED 反馈。",
    reversible: false,
    danger_level: "medium",
    availability: "conditional",
    disabled_reason: "策略构想为空、授权缺失或 readiness 未确认时禁用。",
    next_action: "按紧邻的阻断原因补齐前置条件，再决定是否重试。",
  }),
  "lab.generation.cancel": action({
    action_id: "lab.generation.cancel",
    verb: "取消",
    object: "等待",
    action_label: "取消等待",
    target: "当前浏览器等待中的生成请求",
    boundary: "只取消页面等待；不声称后端持久请求已撤销。",
    prerequisite: "页面正在等待生成请求响应。",
    expected_result: "停止当前页面等待并要求刷新持久证据。",
    failure_impact: "无法确认后端请求是否仍在运行，不能把取消当作完成。",
    reversible: false,
    danger_level: "low",
    availability: "conditional",
    disabled_reason: "没有等待中的请求时不显示。",
    next_action: "刷新 API/DB 持久证据，确认是否产生 generation run。",
  }),
  "lab.generation.deepseek": action({
    action_id: "lab.generation.deepseek",
    verb: "运行",
    object: "DeepSeek 单次生成",
    action_label: "运行 DeepSeek 单次生成",
    target: "高级/受控的单次真实 Provider 请求",
    boundary: "仅在显式一次性授权下调用 Provider；不启动交易执行链路。",
    prerequisite: "operator token、Provider readiness 和一次性授权均满足。",
    expected_result: "返回可追踪的生成证据，或保持 BLOCKED/FAILED。",
    failure_impact: "Provider 请求失败或证据不完整；不得显示为核心成功。",
    reversible: false,
    danger_level: "high",
    availability: "conditional",
    disabled_reason: "未输入授权、提示词或 Provider readiness 未确认时禁用。",
    next_action: "检查脱敏的 Provider/授权状态；不要在页面或日志中粘贴密钥。",
  }),
  "lab.evidence.refresh": action({
    action_id: "lab.evidence.refresh",
    verb: "刷新",
    object: "持久证据",
    action_label: "刷新持久证据",
    target: "当前 Local Strategy Lab 使用的 API/DB 快照",
    boundary: "只重新读取已有证据，不创建运行、不导入结果。",
    prerequisite: "页面已加载或可以访问 Backend API。",
    expected_result: "更新当前页面证据并记录刷新结果。",
    failure_impact: "页面保留旧结论并标注加载失败，不猜测新状态。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "正在加载时禁用，避免并发刷新。",
    next_action: "等待当前请求结束；若失败，恢复 API 后重试。",
  }),
  "lab.workflow.inspect-phase": action({
    action_id: "lab.workflow.inspect-phase",
    verb: "查看",
    object: "研究阶段",
    action_label: "查看研究阶段",
    target: "当前任务流中的一个阶段",
    boundary: "只切换前端查看范围，不推进阶段或写入状态。",
    prerequisite: "目标阶段已出现在任务流中。",
    expected_result: "显示该阶段的当前结论、原因和证据。",
    failure_impact: "继续显示当前阶段，不影响持久记录。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "被锁定的后续阶段不可查看为可执行状态。",
    next_action: "先处理当前阶段的唯一下一步。",
  }),
  "lab.backtest.trigger": action({
    action_id: "lab.backtest.trigger",
    verb: "触发",
    object: "本地回测",
    action_label: "触发本地回测",
    target: "当前候选策略与本地 BacktestProfile",
    boundary: "仅运行本地研究回测；不连接真实交易执行、不下单。",
    prerequisite: "当前候选、strategy file、本地数据和 profile 均通过前置门禁。",
    expected_result: "创建可追踪的 backtest run/task，随后等待持久结果。",
    failure_impact: "回测未被证明创建或成功；保留阻断原因。",
    reversible: false,
    danger_level: "medium",
    availability: "conditional",
    disabled_reason: "候选或本地回测前置条件不满足时禁用。",
    next_action: "处理候选、文件、数据或 profile 的阻断原因后重试。",
  }),
  "lab.backtest.refresh-results": action({
    action_id: "lab.backtest.refresh-results",
    verb: "刷新",
    object: "回测结果",
    action_label: "刷新回测结果",
    target: "当前候选关联的持久 BacktestTask/Result",
    boundary: "只读取并对账结果，不创建新的回测。",
    prerequisite: "已选择候选或关联 task。",
    expected_result: "显示最新持久 task/result 及其状态。",
    failure_impact: "不能确认结果是否到达；不把 POST 成功或空表当作结果。",
    reversible: true,
    danger_level: "none",
    availability: "available",
    disabled_reason: null,
    next_action: "核对 task/result ID 与 artifact，再决定是否导入评分。",
  }),
  "lab.score.ingest": action({
    action_id: "lab.score.ingest",
    verb: "导入",
    object: "任务并评分",
    action_label: "导入任务并评分",
    target: "当前已对账的 BacktestTask/Result",
    boundary: "只对当前任务导入本地 artifact 并计算评分，不晋级策略或启动交易。",
    prerequisite: "task、result、artifact 与候选身份一致且通过对账。",
    expected_result: "创建可追踪的 result/score 证据或明确失败。",
    failure_impact: "评分不被视为成功；保留任务与 artifact 的阻断信息。",
    reversible: false,
    danger_level: "medium",
    availability: "conditional",
    disabled_reason: "任务、结果或 artifact 未完成对账时禁用。",
    next_action: "刷新并核对同一 task 的 result、artifact 与候选 ID。",
  }),
  "lab.dry-run.check": action({
    action_id: "lab.dry-run.check",
    verb: "检查",
    object: "Dry-run 就绪",
    action_label: "检查 Dry-run 就绪",
    target: "当前候选的受控 Dry-run readiness",
    boundary: "只读检查，不启动 Freqtrade、不连接交易所、不下单。",
    prerequisite: "当前候选存在且属于当前环境。",
    expected_result: "返回持久 readiness 结论、原因和下一步。",
    failure_impact: "无法证明可以启动；保持 BLOCKED/API_GAP。",
    reversible: true,
    danger_level: "none",
    availability: "conditional",
    disabled_reason: "缺少当前核心候选时禁用。",
    next_action: "按 readiness 阻断原因补齐前置条件。",
  }),
  "lab.dry-run.refresh": action({
    action_id: "lab.dry-run.refresh",
    verb: "刷新",
    object: "Dry-run 对账证据",
    action_label: "刷新 Dry-run 对账证据",
    target: "Dry-run manifest、status snapshot 与 control report",
    boundary: "只读刷新与对账，不改变运行状态。",
    prerequisite: "页面已有 Dry-run 证据查询上下文。",
    expected_result: "显示最新 manifest/snapshot 身份是否一致。",
    failure_impact: "不能证明当前运行，保持 fail-closed。",
    reversible: true,
    danger_level: "none",
    availability: "available",
    disabled_reason: null,
    next_action: "核对 strategy version、manifest path 和 dry_run=true。",
  }),
  "lab.dry-run.start": action({
    action_id: "lab.dry-run.start",
    verb: "启动",
    object: "受控 Dry-run",
    action_label: "启动受控 Dry-run",
    target: "当前候选的本地受控 Dry-run",
    boundary: "只允许当前本地 Dry-run；禁止 Live、真实资金、真实订单和交易所连接。",
    prerequisite: "readiness、人工批准、dry_run=true 和安全边界均由当前 API/DB 证据确认。",
    expected_result: "启动请求返回并可由持久 manifest/snapshot 对账。",
    failure_impact: "启动不被视为成功；若无法对账则回到 BLOCKED/API_GAP。",
    reversible: true,
    danger_level: "high",
    availability: "conditional",
    disabled_reason: "readiness、批准或安全证据缺失时禁用。",
    next_action: "先刷新持久证据，确认没有未对账运行，再处理唯一阻断。",
  }),
  "lab.dry-run.stop": action({
    action_id: "lab.dry-run.stop",
    verb: "停止",
    object: "受控 Dry-run",
    action_label: "停止受控 Dry-run",
    target: "当前候选的本地受控 Dry-run",
    boundary: "只停止当前本地受控 Dry-run，不触碰 Live 或真实订单。",
    prerequisite: "持久 snapshot 证明当前受控 Dry-run 正在运行。",
    expected_result: "停止请求完成并由持久 snapshot 证明停止。",
    failure_impact: "无法证明已停止；保持恢复/对账阻断，不猜测进程状态。",
    reversible: false,
    danger_level: "high",
    availability: "conditional",
    disabled_reason: "没有可对账的当前运行时禁用。",
    next_action: "刷新 manifest、snapshot 和 control report，确认最终状态。",
  }),
  "freq-ui.open": action({
    action_id: "freq-ui.open",
    verb: "打开",
    object: "只读 FreqUI",
    action_label: "打开只读 FreqUI",
    target: "Backend 管理的 Dry-run/FreqUI 入口",
    boundary: "只允许已配置且受安全边界保护的只读入口；不显示或拼接密钥。",
    prerequisite: "入口 enabled 且 href 由安全 API 返回。",
    expected_result: "在新标签页打开受控 FreqUI 入口。",
    failure_impact: "入口不可用但不会改变 Dry-run 状态。",
    reversible: true,
    danger_level: "medium",
    availability: "conditional",
    disabled_reason: "入口未配置或安全边界未通过时不提供可点击链接。",
    next_action: "先恢复只读入口配置和安全边界，再重新加载。",
  }),
  "not-found.return-dashboard": action({
    action_id: "not-found.return-dashboard",
    verb: "返回",
    object: "总览",
    action_label: "返回总览",
    target: "Freqtrade AI 总览页面",
    boundary: "仅导航，不改变任何研究、运行或交易状态。",
    prerequisite: "应用 shell 已加载。",
    expected_result: "返回总览并重新获取其真实数据。",
    failure_impact: "停留在 404 页面，不影响系统状态。",
    reversible: true,
    danger_level: "none",
    availability: "available",
    disabled_reason: null,
    next_action: "检查当前 URL 或使用主导航。",
  }),
};

const routePurposeContracts: readonly RoutePurposeContract[] = [
  {
    route: "global",
    path: "(app shell)",
    label: "全局界面契约",
    purpose: "提供唯一导航、当前页面、状态来源和按需诊断入口。",
    why: "用户需要先知道自己在哪里、数据是否可信以及下一步在哪里。",
    default_decision: "当前页面是否可继续做产品决策。",
    default_next_action: "按当前页面的唯一结论和下一步继续。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "dashboard",
    path: "/",
    label: "总览",
    purpose: "集中显示策略研究、回测验证和评分的当前真实进展。",
    why: "普通用户需要一个可信的当前进度，而不是跨页面比较原始状态。",
    default_decision: "当前研究链是否有可继续核对的真实记录。",
    default_next_action: "进入产生当前阻断的研究页面；没有安全动作时明确暂无可执行动作。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "strategies",
    path: "/strategies",
    label: "策略",
    purpose: "先判断策略是否有当前版本，再决定是否查看详情。",
    why: "策略名称本身不能证明当前版本可用或可运行。",
    default_decision: "哪一个策略有可核对的当前版本。",
    default_next_action: "选择一条策略查看当前版本与失败原因。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "strategy-detail",
    path: "/strategies/:strategyId",
    label: "策略详情",
    purpose: "显示策略概要、当前版本、谱系和校验结论。",
    why: "继续研究前必须确认当前 artifact 和版本身份。",
    default_decision: "当前版本是否可以进入后续回测。",
    default_next_action: "按当前版本结论进入回测，或处理缺失/失败原因。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "generation-runs",
    path: "/generation-runs",
    label: "生成批次",
    purpose: "核对 Provider、请求数量、产出数量和生成失败结论。",
    why: "生成批次完成不等于存在可用策略产出。",
    default_decision: "是否存在可追踪且有有效产出的生成记录。",
    default_next_action: "打开策略或处理生成批次的阻断/失败原因。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "local-strategy-lab",
    path: "/local-strategy-lab",
    label: "Local Strategy Lab",
    purpose: "按生成、回测、评分、受控 Dry-run 四个阶段推进一个当前候选。",
    why: "阶段化任务流应让用户只面对当前一个决定和一个安全下一步。",
    default_decision: "当前阶段是否满足前置并有唯一可执行动作。",
    default_next_action: "处理当前阶段的唯一阻断或执行带有 descriptor 的主操作。",
    primary_scope: "stage",
    default_primary_action_id: "lab.generation.submit",
  },
  {
    route: "backtest-runs",
    path: "/backtest-runs",
    label: "回测批次",
    purpose: "先判断回测是否有真实、可验收的结果，再看收益和风险。",
    why: "指标和 artifact 不能替代持久 run/task/result 身份。",
    default_decision: "哪些回测结果可以进入评分或复核。",
    default_next_action: "按失败原因补齐本地研究前置，或查看技术证据。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "backtest-tasks",
    path: "/backtest-tasks",
    label: "回测任务",
    purpose: "按任务核对 profile、执行状态和真实持久化结果。",
    why: "任务排队或 HTTP 成功不等于有结果。",
    default_decision: "当前 task 是否已经有可对账的 result。",
    default_next_action: "等待、刷新或处理 task 的阻断/失败原因。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "hyperopt-runs",
    path: "/hyperopt-runs",
    label: "Hyperopt 参数优化",
    purpose: "先判断是否有可用最佳结果，再按需审计参数和 artifact。",
    why: "优化批次状态不能独立证明最佳参数可复现。",
    default_decision: "是否存在可复核的最佳结果和前后指标。",
    default_next_action: "查看最佳结果或处理失败/缺失 artifact。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "ranking",
    path: "/ranking",
    label: "策略排行榜",
    purpose: "只展示有真实 BacktestResult 和 StrategyScore 证据的排名。",
    why: "没有结果身份的分数会误导策略选择。",
    default_decision: "哪些策略分数可作为当前研究比较依据。",
    default_next_action: "查看评分拆解或回到策略详情核对来源。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "operator-dashboard",
    path: "/operator-dashboard",
    label: "运维面板",
    purpose: "只读汇总运行就绪、阻断诊断和安全边界。",
    why: "运维证据应帮助定位原因，但不能覆盖业务链路不可继续的结论。",
    default_decision: "当前环境的主要阻断和可恢复路径是什么。",
    default_next_action: "按优先级处理首个阻断；只在高级诊断中查看内部证据。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "okx-demo",
    path: "/okx-demo",
    label: "OKX Demo 执行",
    purpose: "只读核对 Demo readiness、订单、仓位、账户和对账结论。",
    why: "可见订单或健康进程不能单独证明 Demo 生命周期已完成。",
    default_decision: "当前 Demo 是否有可接受且可对账的状态。",
    default_next_action: "按唯一阻断刷新或补齐证据；不得从此页推断 Live 可用。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "live-governance",
    path: "/live-governance",
    label: "实盘候选治理",
    purpose: "只读核对候选、人工审批、部署记录、回滚和监控快照。",
    why: "治理记录只说明治理状态，不代表已经执行实盘部署。",
    default_decision: "当前候选是否仍被阻断以及应审计哪条证据。",
    default_next_action: "先处理最高风险阻断；没有安全动作时保持只读。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "freq-ui",
    path: "/freq-ui",
    label: "FreqUI 重定向",
    purpose: "把旧入口收敛到 OKX Demo 页面，避免产生第二套运行状态。",
    why: "旧入口不能成为并行的状态或执行控制面。",
    default_decision: "当前入口是否已被安全地收敛到唯一产品页面。",
    default_next_action: "使用 OKX Demo 只读页面；入口不可用时查看安全边界原因。",
    primary_scope: "none",
    default_primary_action_id: null,
  },
  {
    route: "not-found",
    path: "*",
    label: "页面未找到",
    purpose: "解释当前路径无匹配页面，并提供安全返回入口。",
    why: "错误路径不应让用户猜测系统是否发生了业务变化。",
    default_decision: "是否返回可信的总览入口。",
    default_next_action: "返回总览或使用主导航。",
    primary_scope: "page",
    default_primary_action_id: "not-found.return-dashboard",
  },
];

export { routePurposeContracts };

type EntrySeed = {
  element_id: string;
  route: RouteId;
  component: string;
  kind: ElementKind;
  purpose: string;
  why: string;
  user_decision: string;
  trigger_result: string;
  prerequisite: string;
  failure_impact: string;
  recoverability: string;
  disposition: ElementDisposition;
  disclosure: DisclosureLevel;
  accessible_name: string;
  target_user?: Audience;
  technical_only?: boolean;
  default_visible?: boolean;
  primary?: boolean;
  primary_context?: string | null;
  action?: ActionDescriptor;
  decision_value?: DecisionValue;
};

function entry(seed: EntrySeed): ElementPurposeEntry {
  return {
    target_user: "普通用户",
    technical_only: false,
    default_visible: seed.disclosure === "default",
    primary: false,
    primary_context: null,
    ...seed,
  };
}

const navigationMatrix: readonly ElementPurposeEntry[] = [
  ["dashboard", "总览"],
  ["strategies", "策略"],
  ["generation-runs", "生成批次"],
  ["local-strategy-lab", "Local Strategy Lab"],
  ["backtest-runs", "回测批次"],
  ["backtest-tasks", "回测任务"],
  ["hyperopt-runs", "Hyperopt 参数优化"],
  ["ranking", "策略排行榜"],
  ["live-governance", "实盘候选治理"],
  ["operator-dashboard", "运维面板"],
  ["okx-demo", "OKX Demo 执行"],
] .map(([route, label]) => entry({
  element_id: `global.nav.${route}`,
  route: "global",
  component: "AppLayout / NavLink",
  kind: "navigation",
  purpose: `进入${label}页面。`,
  why: "用户需要从统一导航进入一个明确的工作上下文。",
  user_decision: "是否切换到该页面继续当前任务。",
  trigger_result: `路由切换到${label}，不写入业务状态。`,
  prerequisite: "应用 shell 已加载。",
  failure_impact: "无法切换页面，但当前业务状态不变。",
  recoverability: "可使用其他导航入口或直接访问路径。",
  disposition: "retain",
  disclosure: "default",
  accessible_name: `打开${label}`,
  action: actionDescriptors["global.navigation.open-page"],
}));

const commonMatrix: readonly ElementPurposeEntry[] = [
  ...navigationMatrix,
  entry({
    element_id: "global.mobile-nav.toggle",
    route: "global",
    component: "AppLayout / mobile-nav summary",
    kind: "navigation",
    purpose: "在窄屏显示或收起主导航。",
    why: "移动端需要知道当前位置和可用页面入口。",
    user_decision: "是否打开导航选择下一页。",
    trigger_result: "显示当前页面与所有导航入口。",
    prerequisite: "应用 shell 已加载。",
    failure_impact: "当前页面可继续使用，但导航入口不可见。",
    recoverability: "使用桌面导航或已知路径。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "打开主导航",
    action: actionDescriptors["global.mobile-nav.toggle"],
  }),
  entry({
    element_id: "global.page-header",
    route: "global",
    component: "PageHeader",
    kind: "state",
    purpose: "显示页面标题、用途说明和当前数据来源状态。",
    why: "首屏必须先回答在什么页面、为什么需要以及数据是否可用。",
    user_decision: "是否理解当前页面的工作边界。",
    trigger_result: "首屏形成页面上下文，不触发业务动作。",
    prerequisite: "路由匹配。",
    failure_impact: "用户难以判断页面目的，但后端状态不变。",
    recoverability: "回到导航或刷新页面。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "当前页面标题与用途",
    decision_value: decision(
      "我现在应该在这里做什么？",
      "页面用途、当前状态、下一步",
      freshness("当前页面渲染", "每次路由进入或状态刷新时更新"),
      "标题或用途缺失时，保留页面错误状态，不展示无上下文指标。",
    ),
  }),
  entry({
    element_id: "global.status-badge",
    route: "global",
    component: "StatusBadge",
    kind: "status",
    purpose: "把状态枚举翻译为普通用户可理解的结论，并按需保留 raw status。",
    why: "原始枚举不能独立回答是否可继续。",
    user_decision: "当前状态是否需要注意。",
    trigger_result: "显示一致的中文标签、颜色、图标和可访问名称。",
    prerequisite: "有状态值或明确的未知状态。",
    failure_impact: "用户可能误读状态；不得猜测为 READY。",
    recoverability: "显示未知并给出刷新或查看原因的下一步。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "状态：用户语言结论",
    decision_value: decision(
      "这个状态是否阻碍我继续？",
      "用户语言的状态结论",
      freshness("当前 API/DB 响应", "随来源响应更新"),
      "未知、过期或来源冲突时优先显示阻断/需注意，不显示为正常。",
    ),
  }),
  entry({
    element_id: "global.source-notice",
    route: "global",
    component: "FallbackNotice",
    kind: "state",
    purpose: "说明数据来自真实 API、fixture、fallback 或失败来源。",
    why: "数据来源直接决定是否可以作为当前结论的证据。",
    user_decision: "当前内容能否用于产品验收或仅供排查。",
    trigger_result: "在指标旁显示来源可信度和下一步。",
    prerequisite: "页面请求已经得到来源分类。",
    failure_impact: "fixture 或 fallback 可能被误认为真实成功。",
    recoverability: "恢复真实 API 后刷新；fixture 永远不提升为核心成功。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "数据来源状态",
    decision_value: decision(
      "这些数据可以作为当前环境证据吗？",
      "真实数据 / 不可验收 / 数据不可用",
      freshness("来源分类与当前响应", "每次请求完成时更新"),
      "来源失败、fixture 或 fallback 必须显式标记并阻止完成推断。",
    ),
  }),
  entry({
    element_id: "global.empty-error-loading-state",
    route: "global",
    component: "EmptyState / ErrorNotice",
    kind: "state",
    purpose: "区分加载中、真实为空、请求失败和不可用状态。",
    why: "初始空数组不能被当成真实的零记录。",
    user_decision: "等待、恢复数据，还是确认确实没有记录。",
    trigger_result: "显示状态原因和不误导的下一步。",
    prerequisite: "页面状态模型已完成分类。",
    failure_impact: "空快照可能被误读为成功或没有工作。",
    recoverability: "等待当前请求、恢复 API 后刷新，或开始合法的本地研究流程。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "当前数据状态与处理建议",
    decision_value: decision(
      "我现在应等待、恢复还是开始工作？",
      "加载中 / 暂无真实记录 / 暂不可用",
      freshness("当前请求生命周期", "加载结束或失败时更新"),
      "状态未确定时不显示 0 值指标，也不显示完成结论。",
    ),
  }),
  entry({
    element_id: "global.copyable-value",
    route: "global",
    component: "CopyableValue",
    kind: "action",
    target_user: "高级用户",
    purpose: "复制已经显示的 ID、artifact 引用或诊断值以便复核。",
    why: "复制是审计辅助，不应把技术字段提升为普通用户的决策内容。",
    user_decision: "是否需要把当前引用带到诊断或 Issue 中。",
    trigger_result: "复制可见值并给出成功/失败反馈。",
    prerequisite: "值存在且不是敏感值。",
    failure_impact: "复制失败不改变业务记录。",
    recoverability: "手动选择可见值或检查剪贴板权限。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "复制当前可见值",
    technical_only: true,
    action: actionDescriptors["global.copy.value"],
  }),
  entry({
    element_id: "global.diagnostic-disclosure",
    route: "global",
    component: "details / ExpandableText",
    kind: "disclosure",
    target_user: "高级用户",
    purpose: "按需打开完整原因、raw status、IDs、paths、artifact 和脱敏日志。",
    why: "普通模式需要聚焦决策，高级诊断仍必须可完整审计。",
    user_decision: "是否需要进入技术排查。",
    trigger_result: "展开受控的诊断字段，不改变当前产品结论。",
    prerequisite: "存在对应诊断证据。",
    failure_impact: "默认决策仍可见，但排查上下文暂缺。",
    recoverability: "恢复来源后重新展开。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开诊断详情",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
];

const routeMatrix: readonly ElementPurposeEntry[] = [
  entry({
    element_id: "dashboard.summary",
    route: "dashboard",
    component: "Dashboard / summary metrics",
    kind: "data",
    purpose: "汇总策略、生成、回测、Hyperopt 和评分的真实记录数量。",
    why: "总览需要快速回答研究链是否有可继续核对的记录。",
    user_decision: "是否进入某个研究页面处理下一步。",
    trigger_result: "显示当前 API 已加载的核心记录摘要。",
    prerequisite: "真实 API 返回且已区分 loading/failed/empty。",
    failure_impact: "不展示空快照或 fixture 指标。",
    recoverability: "恢复 API 后刷新。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "研究链真实记录摘要",
    decision_value: decision(
      "当前研究链是否有真实记录？",
      "策略、生成、回测、优化、评分数量",
      freshness("Dashboard API 聚合响应", "页面刷新时更新"),
      "任一来源失败时显示不可用，不把缺失计数当作零。",
    ),
  }),
  entry({
    element_id: "dashboard.research-flow",
    route: "dashboard",
    component: "Dashboard / research flow panel",
    kind: "data",
    purpose: "解释生成、回测和优化的研究流处于什么阶段。",
    why: "单纯数字不能说明用户下一步应做什么。",
    user_decision: "是否需要进入 Local Strategy Lab 或回测页面。",
    trigger_result: "显示研究阶段摘要和成功回测数量。",
    prerequisite: "研究数据已加载。",
    failure_impact: "不显示未被证据支持的进度。",
    recoverability: "查看对应页面的来源和阻断原因。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "研究与验证流程进展",
    decision_value: decision(
      "研究流当前应进入哪个阶段？",
      "阶段数量与可复核回测数量",
      freshness("当前 Dashboard 数据", "页面刷新时更新"),
      "阶段来源不完整时显示需注意，并将用户带到详细页面。",
    ),
  }),
  entry({
    element_id: "dashboard.ranking-summary",
    route: "dashboard",
    component: "Dashboard / ranking summary",
    kind: "data",
    purpose: "给出最高分策略的用户语言摘要，但不替代排行榜证据。",
    why: "总览可以提示方向，但策略选择需要回到可验收评分。",
    user_decision: "是否进入排行榜复核领先结果。",
    trigger_result: "显示领先策略或明确暂无评分策略。",
    prerequisite: "ranking 数据存在且来源可解释。",
    failure_impact: "不猜测领先结果，不把 fixture 分数当作真实排名。",
    recoverability: "打开排行榜并核对 Score/BacktestResult。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "领先结果摘要",
    decision_value: decision(
      "是否有值得复核的领先结果？",
      "最高分策略与分数",
      freshness("ranking API 聚合响应", "页面刷新时更新"),
      "没有核心评分时显示暂无结果，而不是显示零或历史领先。",
    ),
  }),
  entry({
    element_id: "strategies.list",
    route: "strategies",
    component: "Strategies / table",
    kind: "data",
    purpose: "列出策略名称、状态、当前版本和 timeframe。",
    why: "用户需要先筛选有当前版本的候选。",
    user_decision: "哪条策略值得进入详情复核。",
    trigger_result: "显示策略列表或明确空/失败状态。",
    prerequisite: "策略 API 返回可解释来源。",
    failure_impact: "不将缺少当前版本的策略显示为可用。",
    recoverability: "展开来源诊断或刷新列表。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "策略列表",
    decision_value: decision(
      "哪个策略有可核对的当前版本？",
      "策略状态、当前版本、时间周期",
      freshness("strategies API 响应", "页面刷新时更新"),
      "状态未知或当前版本缺失时显式显示需注意。",
    ),
  }),
  entry({
    element_id: "strategies.detail-link",
    route: "strategies",
    component: "Strategies / row detail Link",
    kind: "action",
    purpose: "打开一条策略的只读详情。",
    why: "列表摘要不足以证明版本、文件和谱系。",
    user_decision: "是否进入该策略的审计上下文。",
    trigger_result: "进入带 strategyId 的详情页面。",
    prerequisite: "该行有稳定 strategy ID。",
    failure_impact: "无法查看详情但不改变策略。",
    recoverability: "刷新列表或选择其他记录。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "查看策略详情",
    action: actionDescriptors["strategies.open-detail"],
  }),
  entry({
    element_id: "strategy-detail.overview",
    route: "strategy-detail",
    component: "StrategyDetail / overview cards",
    kind: "data",
    purpose: "显示策略状态、当前版本和文件可用性。",
    why: "后续回测必须绑定当前可运行版本。",
    user_decision: "当前策略是否可以进入本地回测。",
    trigger_result: "显示一条明确的可继续/阻断结论。",
    prerequisite: "策略详情与当前版本 API 证据一致。",
    failure_impact: "不把历史版本或缺失文件显示为当前可用。",
    recoverability: "查看版本/文件原因并刷新。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "策略与当前版本概要",
    decision_value: decision(
      "当前版本能否进入回测？",
      "策略状态、版本状态、文件存在性",
      freshness("strategy detail API", "页面刷新时更新"),
      "文件、身份或来源不一致时显示阻断。",
    ),
  }),
  entry({
    element_id: "strategy-detail.audit-disclosures",
    route: "strategy-detail",
    component: "StrategyDetail / source, lineage and diff details",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "按需审计来源、版本谱系、Diff、校验错误和失败原因。",
    why: "技术证据需要可追溯，但不应挤占默认决策空间。",
    user_decision: "是否能定位当前版本不可用的确切原因。",
    trigger_result: "展开完整诊断字段。",
    prerequisite: "详情响应带有来源和证据引用。",
    failure_impact: "默认结论保持 fail-closed。",
    recoverability: "恢复 API 后重新读取证据。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开策略来源、谱系和 Diff",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "generation-runs.table",
    route: "generation-runs",
    component: "GenerationRuns / table",
    kind: "data",
    purpose: "显示 Provider、Model、请求/产出数量、状态和错误摘要。",
    why: "生成记录必须能解释是否真的产生了策略。",
    user_decision: "哪次生成可以继续核对，哪次需要处理失败。",
    trigger_result: "显示生成批次的用户状态和下一步。",
    prerequisite: "generation run API 返回持久记录。",
    failure_impact: "completed 但没有有效产出时不显示为完成研究链。",
    recoverability: "展开错误详情或进入策略列表复核产出。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "生成批次列表",
    decision_value: decision(
      "这次生成是否有可用产出？",
      "Provider、数量、状态、失败结论",
      freshness("generation run API", "页面刷新时更新"),
      "Provider provenance、产出数量或持久 ID 缺失时标记 API 缺口。",
    ),
  }),
  entry({
    element_id: "generation-runs.technical-details",
    route: "generation-runs",
    component: "GenerationRuns / expandable details",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "查看 run ID、错误和关联版本等诊断证据。",
    why: "内部证据用于排查而非默认决策。",
    user_decision: "是否需要把某次生成交给开发/QA 复核。",
    trigger_result: "展开完整但脱敏的技术引用。",
    prerequisite: "诊断字段存在。",
    failure_impact: "不影响默认生成结论。",
    recoverability: "刷新数据来源。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开生成批次诊断",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "local-lab.workflow",
    route: "local-strategy-lab",
    component: "WorkflowNavigator",
    kind: "navigation",
    purpose: "显示当前阶段、已完成阶段、锁定阶段和唯一下一步。",
    why: "一个当前阶段比同时暴露所有原始状态更容易决策。",
    user_decision: "是否回看已完成阶段或处理当前阶段。",
    trigger_result: "切换阶段查看范围，不推进业务状态。",
    prerequisite: "核心证据已分类为 generation/backtest/score/dry-run。",
    failure_impact: "阶段状态不确定时保持当前阶段阻断。",
    recoverability: "刷新真实证据后重新计算阶段。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "查看策略实验任务流",
    decision_value: decision(
      "当前任务应处理哪个阶段？",
      "阶段、结论、唯一推荐下一步",
      freshness("Local Strategy Lab API/DB 快照", "快照刷新时更新"),
      "任何来源不完整时不解锁后续阶段。",
    ),
    action: actionDescriptors["lab.workflow.inspect-phase"],
  }),
  entry({
    element_id: "local-lab.generation-form",
    route: "local-strategy-lab",
    component: "GenerationStage / form",
    kind: "form",
    purpose: "收集策略构想和本地授权前置，固定每次只提交一个策略。",
    why: "表单应让用户知道为什么能提交或为什么被阻止。",
    user_decision: "是否满足提交一次本地策略生成请求的前置。",
    trigger_result: "表单显示可提交或紧邻的阻断原因。",
    prerequisite: "用户输入策略构想并按需提供授权。",
    failure_impact: "不会提交不满足前置的 API 请求。",
    recoverability: "补齐字段、恢复 readiness 后重试。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "策略生成表单",
    decision_value: decision(
      "现在是否可以安全提交一次生成？",
      "构想、授权、Provider readiness",
      freshness("当前表单和 readiness 快照", "输入或刷新时更新"),
      "缺少前置时显示原因，不把按钮静默禁用。",
    ),
  }),
  entry({
    element_id: "local-lab.generation.submit",
    route: "local-strategy-lab",
    component: "GenerationStage / primary submit",
    kind: "action",
    purpose: "提交一次有界的策略生成请求。",
    why: "生成是当前 generation 阶段唯一主操作。",
    user_decision: "是否提交当前构想。",
    trigger_result: "产生 generation run 的成功、阻断或失败反馈。",
    prerequisite: "见 ActionDescriptor 的 readiness 与授权前置。",
    failure_impact: "没有完整持久证据时不显示成功。",
    recoverability: "按阻断原因补齐前置后再决定是否重试。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "提交策略生成",
    default_visible: true,
    primary: true,
    primary_context: "generation",
    action: actionDescriptors["lab.generation.submit"],
  }),
  entry({
    element_id: "local-lab.generation.cancel",
    route: "local-strategy-lab",
    component: "GenerationStage / cancel waiting",
    kind: "action",
    purpose: "停止浏览器等待并转向持久证据核对。",
    why: "超时或取消不能被误报成成功。",
    user_decision: "是否停止等待当前页面响应。",
    trigger_result: "页面显示 BLOCKED 并提示刷新持久记录。",
    prerequisite: "生成请求正在等待。",
    failure_impact: "不保证后端请求已撤销。",
    recoverability: "刷新 generation runs 和 API/DB 证据。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "取消等待",
    action: actionDescriptors["lab.generation.cancel"],
  }),
  entry({
    element_id: "local-lab.evidence-browser",
    route: "local-strategy-lab",
    component: "EvidenceBrowser",
    kind: "data",
    purpose: "默认比较核心决策字段，并将 historical/fixture/不完整来源放入诊断范围。",
    why: "核心证据与诊断证据必须可区分且不能互相提升。",
    user_decision: "当前记录是否可以更新候选工作台。",
    trigger_result: "切换范围、类型和记录选择，不改变业务状态。",
    prerequisite: "证据已按 current/diagnostic 分区。",
    failure_impact: "不把诊断记录当作当前核心候选。",
    recoverability: "刷新并重新选择同一身份的核心记录。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "持久证据浏览器",
    decision_value: decision(
      "这条证据能否更新当前候选？",
      "来源范围、状态、核心指标",
      freshness("API/DB 证据快照", "刷新证据时更新"),
      "historical、fixture 或 identity 不匹配时移入诊断并保持不可验收。",
    ),
  }),
  entry({
    element_id: "local-lab.backtest.trigger",
    route: "local-strategy-lab",
    component: "CandidateWorkbench / backtest primary",
    kind: "action",
    purpose: "在 backtest 阶段触发当前候选的本地回测。",
    why: "回测是该阶段唯一创建研究任务的主操作。",
    user_decision: "是否按当前 profile 创建一次本地回测。",
    trigger_result: "创建可追踪 run/task 或明确阻断。",
    prerequisite: "候选、文件、数据和 profile 通过门禁。",
    failure_impact: "不把请求成功或空结果当作回测完成。",
    recoverability: "按阻断原因修复本地研究前置。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "触发此候选的本地回测",
    default_visible: false,
    primary: true,
    primary_context: "backtest",
    action: actionDescriptors["lab.backtest.trigger"],
  }),
  entry({
    element_id: "local-lab.backtest.refresh-results",
    route: "local-strategy-lab",
    component: "CandidateWorkbench / result refresh",
    kind: "action",
    purpose: "重新读取当前候选的持久回测任务与结果。",
    why: "刷新是核对动作，不应与创建回测竞争主操作。",
    user_decision: "是否已有可对账 result。",
    trigger_result: "更新 task/result 选择和证据提示。",
    prerequisite: "当前候选或 task 已选择。",
    failure_impact: "不能证明结果到达，但不会创建新回测。",
    recoverability: "等待后端任务或处理缺失 artifact。",
    disposition: "rename",
    disclosure: "default",
    accessible_name: "刷新回测结果",
    default_visible: false,
    primary: false,
    primary_context: "backtest",
    action: actionDescriptors["lab.backtest.refresh-results"],
  }),
  entry({
    element_id: "local-lab.score.ingest",
    route: "local-strategy-lab",
    component: "CandidateWorkbench / score primary",
    kind: "action",
    purpose: "导入已对账的回测任务并计算评分。",
    why: "评分是 score 阶段唯一改变研究结果的主操作。",
    user_decision: "是否把当前 task/result 作为评分输入。",
    trigger_result: "产生 result/score 证据或明确失败。",
    prerequisite: "task/result/artifact 与候选身份一致。",
    failure_impact: "不显示未对账或没有持久 ID 的评分成功。",
    recoverability: "刷新并修复证据链后重试。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "导入此任务并评分",
    default_visible: false,
    primary: true,
    primary_context: "score",
    action: actionDescriptors["lab.score.ingest"],
  }),
  entry({
    element_id: "local-lab.dry-run.decision",
    route: "local-strategy-lab",
    component: "DryRunDecisionPanel",
    kind: "data",
    purpose: "汇总当前候选、readiness、持久运行和安全结论。",
    why: "运行前必须只有一个可信的 Dry-run 决定。",
    user_decision: "当前能否继续受控 Dry-run。",
    trigger_result: "显示当前结论、唯一阻断和推荐下一步。",
    prerequisite: "候选与 readiness/snapshot 证据已读取。",
    failure_impact: "任何身份或安全证据不一致都保持不可继续。",
    recoverability: "按阻断原因刷新和恢复证据。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "受控 Dry-run 统一决策区",
    default_visible: false,
    decision_value: decision(
      "当前候选是否能安全继续 Dry-run？",
      "候选、readiness、持久运行、安全边界",
      freshness("当前 Dry-run API/DB 快照", "检查或刷新时更新"),
      "缺少 manifest、snapshot、dry_run=true 或安全证据时保持 BLOCKED/API_GAP。",
    ),
  }),
  entry({
    element_id: "local-lab.dry-run.actions",
    route: "local-strategy-lab",
    component: "DryRunDecisionPanel / primary action",
    kind: "action",
    purpose: "按当前状态只显示检查、刷新、启动或停止中的一个主操作。",
    why: "受控 Dry-run 必须把边界、可逆性和证据要求放在动作旁边。",
    user_decision: "是否执行当前唯一安全动作。",
    trigger_result: "返回可对账的 readiness 或运行状态，失败则保持阻断。",
    prerequisite: "由当前 model.action 和紧邻 disabled reason 决定。",
    failure_impact: "不能凭浏览器反馈宣称已运行或已停止。",
    recoverability: "刷新持久 manifest/snapshot/control report。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "执行当前 Dry-run 主操作（检查、刷新、启动或停止）",
    default_visible: false,
    primary: true,
    primary_context: "dry-run",
    action: actionDescriptors["lab.dry-run.check"],
  }),
  entry({
    element_id: "local-lab.deepseek.single-run",
    route: "local-strategy-lab",
    component: "PersistentEvidence / advanced DeepSeek section",
    kind: "action",
    target_user: "高级用户",
    purpose: "在显式一次性授权下执行受控的单次 Provider 生成。",
    why: "真实 Provider 调用不应与普通研究动作并列或默认发生。",
    user_decision: "是否明确授权这一次高级 Provider 调用。",
    trigger_result: "产生可追踪的 generation evidence 或阻断。",
    prerequisite: "token、prompt、Provider readiness 和 checkbox 均满足。",
    failure_impact: "失败或证据不完整时不得提升为核心成功。",
    recoverability: "只检查脱敏授权/Provider 状态，不暴露密钥。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "运行 DeepSeek 单次生成",
    technical_only: true,
    action: actionDescriptors["lab.generation.deepseek"],
  }),
  entry({
    element_id: "local-lab.audit-evidence",
    route: "local-strategy-lab",
    component: "PersistentEvidence / technical matrix and diagnostics",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "完整审计 database IDs、artifact、source、environment 和原始状态。",
    why: "普通模式隐藏技术噪声，但 QA/运维仍需要完整可追踪性。",
    user_decision: "是否能定位一条证据为什么可验收或不可验收。",
    trigger_result: "展开核心/非核心诊断记录和证据链。",
    prerequisite: "API 返回脱敏诊断字段。",
    failure_impact: "默认结论仍以 fail-closed 方式显示。",
    recoverability: "恢复 API 后刷新证据。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开 Local Strategy Lab 高级诊断",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "backtest-runs.summary",
    route: "backtest-runs",
    component: "BacktestRuns / matrix summary and table",
    kind: "data",
    purpose: "按状态、时间范围、指标和结果身份展示回测批次。",
    why: "先判断是否有真实结果，再讨论收益或风险。",
    user_decision: "哪条回测可以进入结果复核。",
    trigger_result: "显示可验收、阻断、失败或缺结果的批次。",
    prerequisite: "run/task/result 关系可被 API 证据解释。",
    failure_impact: "没有 result 身份时不显示为可验收。",
    recoverability: "展开原因与 artifact 诊断。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "回测批次与结果摘要",
    decision_value: decision(
      "这条回测是否有可验收结果？",
      "状态、收益/风险指标、result 身份",
      freshness("backtest runs API", "页面刷新时更新"),
      "run/task/result 或 artifact 不完整时显示阻断/缺口。",
    ),
  }),
  entry({
    element_id: "backtest-runs.technical-details",
    route: "backtest-runs",
    component: "BacktestViewParts / technical disclosure",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "审计 profile、ID、manifest、路径、stdout/stderr 和失败原因。",
    why: "原始回测证据属于高级诊断。",
    user_decision: "是否能定位回测或 artifact 的具体失败点。",
    trigger_result: "展开完整脱敏技术证据。",
    prerequisite: "run 有对应技术字段。",
    failure_impact: "不改变默认结果结论。",
    recoverability: "修复本地前置后刷新。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开回测技术详情",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "backtest-tasks.task-list",
    route: "backtest-tasks",
    component: "BacktestTasks / task table",
    kind: "data",
    purpose: "显示任务 profile、执行状态、关联 run 和持久结果。",
    why: "task 的存在与 result 的存在必须分开判断。",
    user_decision: "当前 task 是等待、阻断还是已有 result。",
    trigger_result: "显示单一任务结论和下一步。",
    prerequisite: "task API 可用。",
    failure_impact: "不把 queued/running 或空结果当完成。",
    recoverability: "等待或刷新 API/DB 结果。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "回测任务列表",
    decision_value: decision(
      "任务现在是否已经有持久结果？",
      "task 状态、run 关联、result 状态",
      freshness("backtest tasks API", "页面刷新时更新"),
      "缺 result、来源失败或状态过期时显示需注意。",
    ),
  }),
  entry({
    element_id: "hyperopt-runs.summary",
    route: "hyperopt-runs",
    component: "HyperoptRuns / overview and table",
    kind: "data",
    purpose: "显示优化批次、最佳结果、警告和前后指标。",
    why: "最佳参数只有在可追踪 artifact 和结果存在时才有决策价值。",
    user_decision: "是否有可复核的最佳结果。",
    trigger_result: "显示最佳批次或明确没有可用结果。",
    prerequisite: "Hyperopt API 返回可解释结果。",
    failure_impact: "不把失败/缺 artifact 的批次当最佳结果。",
    recoverability: "查看警告和技术详情。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "Hyperopt 结果摘要",
    decision_value: decision(
      "是否存在可复核的最佳优化结果？",
      "最佳结果、指标变化、警告",
      freshness("hyperopt API", "页面刷新时更新"),
      "最佳结果缺少持久 ID 或 artifact 时显示阻断。",
    ),
  }),
  entry({
    element_id: "hyperopt-runs.technical-details",
    route: "hyperopt-runs",
    component: "HyperoptRuns / technical disclosure",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "按需查看 spaces、参数 JSON、配置和 artifact。",
    why: "完整参数集是复现证据，不是普通用户首屏决策。",
    user_decision: "是否可以复现或排查优化结果。",
    trigger_result: "展开完整优化技术详情。",
    prerequisite: "批次带有诊断字段。",
    failure_impact: "不改变最佳结果是否可用的默认结论。",
    recoverability: "修复数据或 artifact 后刷新。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开 Hyperopt 技术详情",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "ranking.table",
    route: "ranking",
    component: "Ranking / score table",
    kind: "data",
    purpose: "比较有真实结果身份的策略评分和结论。",
    why: "排名必须建立在当前可验收结果上。",
    user_decision: "哪个策略值得继续研究或复核。",
    trigger_result: "显示评分、资格和淘汰原因。",
    prerequisite: "BacktestResult 和 StrategyScore 证据一致。",
    failure_impact: "不显示 fixture、历史或缺 ID 记录为当前排名。",
    recoverability: "回到策略/回测页面补齐证据。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "策略评分排行榜",
    decision_value: decision(
      "哪些策略分数可用于当前比较？",
      "总分、评分拆解、资格与原因",
      freshness("ranking API aggregate", "页面刷新时更新"),
      "结果身份或来源不可接受时移入诊断，不进入普通排名。",
    ),
  }),
  entry({
    element_id: "ranking.audit-details",
    route: "ranking",
    component: "Ranking / audit disclosure",
    kind: "diagnostic",
    target_user: "高级用户",
    purpose: "审计 score ID、result ID、文件路径、database IDs 和 artifact refs。",
    why: "评分来源需要可追溯但不应占用普通模式。",
    user_decision: "是否能证明该分数来自正确的回测结果。",
    trigger_result: "展开完整来源追踪。",
    prerequisite: "排名条目带来源追踪。",
    failure_impact: "默认评分仍按 fail-closed 规则显示。",
    recoverability: "刷新排名或回到结果页面。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开评分 ID、路径和来源",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "operator-dashboard.conclusion",
    route: "operator-dashboard",
    component: "OperatorDashboard / conclusion",
    kind: "status",
    target_user: "运维人员",
    purpose: "给出运行与业务阻断排序后的唯一运维结论。",
    why: "进程健康不能覆盖业务链路不可继续。",
    user_decision: "现在最重要的阻断是什么。",
    trigger_result: "显示结论、首个问题和下一步。",
    prerequisite: "Operator status API 返回来源和状态优先级。",
    failure_impact: "来源冲突时必须保持需注意/失败。",
    recoverability: "按首个问题查看诊断，不执行交易动作。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "运维当前结论",
    decision_value: decision(
      "当前最重要的阻断是什么？",
      "业务优先级的系统结论、原因、下一步",
      freshness("Operator status API", "状态报告生成时更新"),
      "API 不可用或来源冲突时映射为 NEEDS_ATTENTION，不猜 READY。",
    ),
  }),
  entry({
    element_id: "operator-dashboard.diagnostics",
    route: "operator-dashboard",
    component: "OperatorDashboard / diagnostics",
    kind: "diagnostic",
    target_user: "运维人员",
    purpose: "按优先级查看 readiness、artifact、ENV presence、治理事件和安全边界。",
    why: "高级诊断要完整，但不能把内部字段塞进普通首屏。",
    user_decision: "应该修复哪一项以及需要什么证据。",
    trigger_result: "展开只读诊断表、证据路径和脱敏原因。",
    prerequisite: "Operator report 提供脱敏诊断项。",
    failure_impact: "不修改 runtime/writer/订单状态。",
    recoverability: "修复外部前置后重新读取报告。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开运维诊断和安全边界",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "okx-demo.readiness",
    route: "okx-demo",
    component: "OkxDemo / readiness and acceptance",
    kind: "status",
    target_user: "运维人员",
    purpose: "只读显示 Demo readiness、生命周期证据和可接受性结论。",
    why: "HTTP 成功、进程健康或单个订单都不能替代完整生命周期证据。",
    user_decision: "当前 Demo 是否可接受，或必须保持阻断。",
    trigger_result: "显示唯一 Demo 结论及阻断原因。",
    prerequisite: "OKX Demo observability/reconciliation API 返回可解释来源。",
    failure_impact: "不得把 fixture、历史屏幕或空表提升为可接受。",
    recoverability: "刷新只读证据并按阻断原因处理。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "OKX Demo 运行准备度与可接受性",
    decision_value: decision(
      "OKX Demo 生命周期是否有完整可接受证据？",
      "readiness、生命周期、对账结论",
      freshness("OKX Demo observability API", "刷新页面时更新"),
      "来源冲突、缺 ordId、对账失败或状态过期时保持 NOT_ACCEPTABLE/BLOCKED。",
    ),
  }),
  entry({
    element_id: "okx-demo.order-workspace",
    route: "okx-demo",
    component: "OkxDemo / order table and fills",
    kind: "data",
    target_user: "运维人员",
    purpose: "只读核对订单、成交、intent 和 authoritative status。",
    why: "订单展示必须服务于对账，不提供隐藏的执行控制。",
    user_decision: "订单是否已被完整、可信地对账。",
    trigger_result: "显示可核对订单和成交关系。",
    prerequisite: "订单与 intent 数据来源可验证。",
    failure_impact: "缺关键 ID 时显示不完整，不宣称完成。",
    recoverability: "查看诊断字段或刷新 observability。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "OKX Demo 订单和成交",
    decision_value: decision(
      "订单生命周期是否已对账？",
      "订单状态、成交、TradeIntent 关联",
      freshness("OKX Demo observability API", "刷新页面时更新"),
      "缺失或冲突 ID 时显示 incomplete 并指向高级诊断。",
    ),
  }),
  entry({
    element_id: "okx-demo.audit-details",
    route: "okx-demo",
    component: "OkxDemo / positions, account and reconciliation details",
    kind: "diagnostic",
    target_user: "运维人员",
    purpose: "查看账户、仓位、对账快照和原始检查字段。",
    why: "这些字段用于安全审计，不等同于 Live 或真实资金可用。",
    user_decision: "是否需要进一步排查 Demo 证据缺口。",
    trigger_result: "展开只读审计上下文。",
    prerequisite: "observability API 返回脱敏诊断值。",
    failure_impact: "不触发订单、writer 或交易配置变化。",
    recoverability: "刷新或由授权运维处理外部阻断。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开 OKX Demo 高级审计",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "live-governance.overview",
    route: "live-governance",
    component: "LiveGovernance / overview cards",
    kind: "status",
    target_user: "运维人员",
    purpose: "汇总候选、审批、部署治理记录和监控快照数量与状态。",
    why: "治理数量不能替代当前是否允许执行的结论。",
    user_decision: "应该先审计哪个治理对象。",
    trigger_result: "显示只读治理摘要。",
    prerequisite: "治理 API 返回真实来源。",
    failure_impact: "不把治理记录当作已执行部署。",
    recoverability: "展开对象的风险、审批和回滚证据。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "实盘候选治理摘要",
    decision_value: decision(
      "当前治理对象是否有高优先级阻断？",
      "候选、审批、部署、监控状态",
      freshness("Live Governance API", "页面刷新时更新"),
      "状态冲突或 stale 时优先显示需注意，不提供执行暗示。",
    ),
  }),
  entry({
    element_id: "live-governance.audit-details",
    route: "live-governance",
    component: "LiveGovernance / candidate, approval, rollback and monitoring details",
    kind: "diagnostic",
    target_user: "运维人员",
    purpose: "按需审计风险检查、人工决策、回滚方案、监控和告警引用。",
    why: "治理证据必须可追溯，但默认页面只保留当前风险结论。",
    user_decision: "是否需要人工复核或恢复治理记录。",
    trigger_result: "展开完整治理证据，不执行部署。",
    prerequisite: "治理记录存在且字段已脱敏。",
    failure_impact: "不会启动 Live、下单或修改治理状态。",
    recoverability: "由授权流程处理，前端只读刷新。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "展开治理、回滚和监控详情",
    technical_only: true,
    action: actionDescriptors["global.disclosure.open-diagnostic"],
  }),
  entry({
    element_id: "freq-ui.redirect",
    route: "freq-ui",
    component: "App / Navigate redirect",
    kind: "state",
    purpose: "将旧 FreqUI 路由收敛到唯一 OKX Demo 页面。",
    why: "避免旧入口形成第二套运行状态或控制面。",
    user_decision: "是否继续使用唯一只读 Demo 页面。",
    trigger_result: "自动导航到 /okx-demo。",
    prerequisite: "应用路由已加载。",
    failure_impact: "不能进入旧入口，但不会改变运行状态。",
    recoverability: "直接使用 /okx-demo。",
    disposition: "merge",
    disclosure: "default",
    accessible_name: "转到 OKX Demo 只读页面",
  }),
  entry({
    element_id: "freq-ui.open-link",
    route: "freq-ui",
    component: "FreqUILink / external link",
    kind: "action",
    target_user: "运维人员",
    purpose: "在安全边界通过时打开 Backend 管理的只读 FreqUI。",
    why: "入口只服务于受控 Dry-run 观察，不提供执行控制。",
    user_decision: "是否打开已配置的只读入口。",
    trigger_result: "在新标签页打开安全 API 提供的链接。",
    prerequisite: "link.enabled、href 和安全边界均满足。",
    failure_impact: "入口不可用但不改变运行状态。",
    recoverability: "恢复配置/边界后刷新。",
    disposition: "retain",
    disclosure: "advanced-diagnostic",
    accessible_name: "打开只读 FreqUI",
    technical_only: true,
    action: actionDescriptors["freq-ui.open"],
  }),
  entry({
    element_id: "not-found.return-dashboard",
    route: "not-found",
    component: "NotFound / Link",
    kind: "action",
    purpose: "从错误路径返回可信的总览入口。",
    why: "用户需要一个明确且无副作用的恢复路径。",
    user_decision: "是否回到总览继续任务。",
    trigger_result: "导航到 / 并重新加载总览数据。",
    prerequisite: "应用 shell 已加载。",
    failure_impact: "停留在 404，不改变业务状态。",
    recoverability: "使用主导航。",
    disposition: "retain",
    disclosure: "default",
    accessible_name: "返回总览",
    primary: true,
    primary_context: "page",
    action: actionDescriptors["not-found.return-dashboard"],
  }),
];

export const elementPurposeMatrix: readonly ElementPurposeEntry[] = [
  ...commonMatrix,
  ...routeMatrix,
];

const DANGER_LEVELS: readonly DangerLevel[] = ["none", "low", "medium", "high", "critical"];
const DISCLOSURE_LEVELS: readonly DisclosureLevel[] = ["default", "advanced-diagnostic"];

function nonEmpty(value: string | null | undefined): boolean {
  return Boolean(value?.trim());
}

export function validateActionDescriptor(descriptor: ActionDescriptor): string[] {
  const errors: string[] = [];
  if (!nonEmpty(descriptor.action_id)) errors.push("action_id 不能为空");
  if (!nonEmpty(descriptor.verb)) errors.push("verb 不能为空");
  if (!nonEmpty(descriptor.object)) errors.push("object 不能为空");
  if (!nonEmpty(descriptor.action_label)) errors.push("action_label 不能为空");
  if (nonEmpty(descriptor.verb) && !descriptor.action_label.includes(descriptor.verb)) {
    errors.push("action_label 必须包含 verb");
  }
  if (nonEmpty(descriptor.object) && !descriptor.action_label.includes(descriptor.object)) {
    errors.push("action_label 必须包含 object");
  }
  if (!nonEmpty(descriptor.target)) errors.push("target 不能为空");
  if (!nonEmpty(descriptor.boundary)) errors.push("boundary 不能为空");
  if (!nonEmpty(descriptor.prerequisite)) errors.push("prerequisite 不能为空");
  if (!nonEmpty(descriptor.expected_result)) errors.push("expected_result 不能为空");
  if (!nonEmpty(descriptor.failure_impact)) errors.push("failure_impact 不能为空");
  if (!nonEmpty(descriptor.next_action)) errors.push("next_action 不能为空");
  if (!DANGER_LEVELS.includes(descriptor.danger_level)) errors.push("danger_level 无效");
  if (!descriptor.reversible && descriptor.danger_level === "none") {
    errors.push("不可逆动作不能标记为 none danger_level");
  }
  if (descriptor.availability === "conditional" && !nonEmpty(descriptor.disabled_reason)) {
    errors.push("conditional 动作必须提供 disabled_reason");
  }
  return errors;
}

export function validateDecisionValue(value: DecisionValue): string[] {
  const errors: string[] = [];
  if (!nonEmpty(value.decision_question)) errors.push("decision_question 不能为空");
  if (!nonEmpty(value.value_label)) errors.push("value_label 不能为空");
  if (!nonEmpty(value.freshness.source)) errors.push("freshness.source 不能为空");
  if (!nonEmpty(value.freshness.update_rule)) errors.push("freshness.update_rule 不能为空");
  if (!nonEmpty(value.freshness.stale_label)) errors.push("freshness.stale_label 不能为空");
  if (!nonEmpty(value.anomaly_advice)) errors.push("anomaly_advice 不能为空");
  if (!DISCLOSURE_LEVELS.includes(value.default_visibility)) errors.push("default_visibility 无效");
  return errors;
}

export type StaticAuditOptions = {
  routes?: readonly RoutePurposeContract[];
  actions?: Readonly<Record<string, ActionDescriptor>>;
};

/**
 * Runtime validation used by the static Node test. It intentionally accepts a
 * matrix argument so a future page cannot bypass the gate by mutating the
 * catalog silently.
 */
export function validateElementPurposeMatrix(
  matrix: readonly ElementPurposeEntry[],
  options: StaticAuditOptions = {},
): string[] {
  const errors: string[] = [];
  const routes = options.routes ?? routePurposeContracts;
  const actions = options.actions ?? actionDescriptors;
  const routeSet = new Set<RouteId>(ROUTE_IDS);
  const seenElementIds = new Set<string>();

  for (const route of routes) {
    const entries = matrix.filter((item) => item.route === route.route);
    if (entries.length === 0) {
      errors.push(`${route.route}: 缺少元素目的矩阵条目`);
      continue;
    }
    if (!entries.some((item) => item.disclosure === "default")) {
      errors.push(`${route.route}: 缺少普通模式元素`);
    }
    const defaultPrimary = entries.filter((item) => item.primary && item.default_visible);
    if (defaultPrimary.length > 1) {
      errors.push(`${route.route}: 默认模式超过一个主操作`);
    }
    if (route.default_primary_action_id && !entries.some(
      (item) => item.primary && item.default_visible && item.action?.action_id === route.default_primary_action_id,
    )) {
      errors.push(`${route.route}: default_primary_action_id 未映射到默认主操作`);
    }
  }

  for (const item of matrix) {
    if (seenElementIds.has(item.element_id)) errors.push(`${item.element_id}: stable ID 重复`);
    seenElementIds.add(item.element_id);
    if (!routeSet.has(item.route)) errors.push(`${item.element_id}: route 不在 ROUTE_IDS`);
    if (!nonEmpty(item.accessible_name)) errors.push(`${item.element_id}: accessible_name 不能为空`);
    if (!nonEmpty(item.purpose) || !nonEmpty(item.why) || !nonEmpty(item.user_decision)) {
      errors.push(`${item.element_id}: purpose/why/user_decision 不能为空`);
    }
    if (!nonEmpty(item.trigger_result) || !nonEmpty(item.failure_impact) || !nonEmpty(item.recoverability)) {
      errors.push(`${item.element_id}: trigger_result/failure_impact/recoverability 不能为空`);
    }
    if (!DISCLOSURE_LEVELS.includes(item.disclosure)) errors.push(`${item.element_id}: disclosure 无效`);
    if (item.technical_only && item.disclosure !== "advanced-diagnostic") {
      errors.push(`${item.element_id}: technical_only 元素不得进入普通模式`);
    }
    if ((item.kind === "action" || item.kind === "navigation") && !item.action) {
      errors.push(`${item.element_id}: 可操作元素缺少 ActionDescriptor`);
    }
    if (item.action) {
      const descriptor = actions[item.action.action_id];
      if (!descriptor) errors.push(`${item.element_id}: 未注册的 ${item.action.action_id}`);
      for (const error of validateActionDescriptor(item.action)) {
        errors.push(`${item.element_id}: ${error}`);
      }
    }
    if (item.kind === "data" || item.kind === "status") {
      if (!item.decision_value) {
        errors.push(`${item.element_id}: 数据/状态元素缺少 DecisionValue`);
      } else {
        for (const error of validateDecisionValue(item.decision_value)) {
          errors.push(`${item.element_id}: ${error}`);
        }
        if (item.decision_value.default_visibility !== item.disclosure) {
          errors.push(`${item.element_id}: DecisionValue visibility 与 disclosure 不一致`);
        }
      }
    }
    if (item.primary && !item.action) errors.push(`${item.element_id}: 主操作缺少 ActionDescriptor`);
    if (item.primary && !nonEmpty(item.primary_context)) errors.push(`${item.element_id}: 主操作缺少 primary_context`);
    if (item.action && !item.accessible_name.includes(item.action.verb)) {
      errors.push(`${item.element_id}: accessible_name 必须包含动作 verb`);
    }
  }

  return errors;
}

export function primaryActionIdsForRoute(route: RouteId): string[] {
  return elementPurposeMatrix
    .filter((item) => item.route === route && item.primary && item.default_visible)
    .map((item) => item.action?.action_id)
    .filter((actionId): actionId is string => Boolean(actionId));
}

/**
 * Source-level gate for primary controls. The UI may still choose a dynamic
 * action (the Dry-run panel does this), so a template expression is accepted
 * only when its prefix resolves to registered descriptors.
 */
export function auditPrimaryActionSource(
  source: string,
  sourceName: string,
  actions: Readonly<Record<string, ActionDescriptor>> = actionDescriptors,
): string[] {
  const errors: string[] = [];
  const primaryClassPattern = /primary-(?:button|link)/g;
  const primaryClassCount = source.match(primaryClassPattern)?.length ?? 0;
  const tagPattern = /<(?:button|a|Link|span)\b[^>]*>/g;
  let auditedTagCount = 0;

  for (const match of source.matchAll(tagPattern)) {
    const tag = match[0];
    if (!/className\s*=\s*["'][^"']*primary-(?:button|link)[^"']*["']/.test(tag)) continue;
    auditedTagCount += 1;
    const line = source.slice(0, match.index ?? 0).split("\n").length;
    const dataAction = tag.match(/data-action-id\s*=\s*"([^"]+)"/);
    const templateAction = tag.match(/data-action-id\s*=\s*\{`([^`]+)`\}/);
    if (!dataAction && !templateAction) {
      errors.push(`${sourceName}:${line}: 主控件缺少 data-action-id`);
      continue;
    }
    if (dataAction && !actions[dataAction[1]]) {
      errors.push(`${sourceName}:${line}: 未注册的主操作 ${dataAction[1]}`);
    }
    if (templateAction) {
      const prefix = templateAction[1].split("${", 1)[0];
      const resolved = Object.keys(actions).some((actionId) => actionId.startsWith(prefix));
      if (!resolved) {
        errors.push(`${sourceName}:${line}: 动态主操作前缀 ${prefix} 未注册`);
      }
    }
  }

  if (auditedTagCount !== primaryClassCount) {
    errors.push(`${sourceName}: 有 primary class 未落在可审计的 button/link 元素上`);
  }
  return errors;
}
