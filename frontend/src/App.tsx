import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./layout/AppLayout";
import { BacktestRuns } from "./pages/BacktestRuns";
import { BacktestTasks } from "./pages/BacktestTasks";
import { ConfigurationCenter } from "./pages/ConfigurationCenter";
import { Dashboard } from "./pages/Dashboard";
import { GenerationRuns } from "./pages/GenerationRuns";
import { HyperoptRuns } from "./pages/HyperoptRuns";
import { LiveGovernance } from "./pages/LiveGovernance";
import { LocalStrategyLab } from "./pages/LocalStrategyLab";
import { NotFound } from "./pages/NotFound";
import { OperatorDashboard } from "./pages/OperatorDashboard";
import { OkxDemo } from "./pages/OkxDemo";
import { Ranking } from "./pages/Ranking";
import { ResearchQueue } from "./pages/ResearchQueue";
import { Strategies } from "./pages/Strategies";
import { StrategyDetail } from "./pages/StrategyDetail";
import { CanonicalConfigurationPage } from "./pages/canonicalV13/CanonicalConfigurationPage";
import { CanonicalMarketDataPage } from "./pages/canonicalV13/CanonicalMarketDataPage";
import { CanonicalOptimizationPage } from "./pages/canonicalV13/CanonicalOptimizationPage";
import { CanonicalResearchPage } from "./pages/canonicalV13/CanonicalResearchPage";
import { CanonicalStrategiesPage } from "./pages/canonicalV13/CanonicalStrategiesPage";
import { CanonicalSubmissionPage } from "./pages/canonicalV13/CanonicalSubmissionPage";

export function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="v13/submission" element={<CanonicalSubmissionPage />} />
        <Route path="v13/strategies" element={<CanonicalStrategiesPage />} />
        <Route path="v13/configuration" element={<CanonicalConfigurationPage />} />
        <Route path="v13/market-data" element={<CanonicalMarketDataPage />} />
        <Route path="v13/research" element={<CanonicalResearchPage />} />
        <Route path="v13/optimization" element={<CanonicalOptimizationPage />} />
        <Route path="strategies" element={<Strategies />} />
        <Route path="strategies/:strategyId" element={<StrategyDetail />} />
        <Route path="configuration" element={<ConfigurationCenter />} />
        <Route path="research-queue" element={<ResearchQueue />} />
        <Route path="generation-runs" element={<GenerationRuns />} />
        <Route path="local-strategy-lab" element={<LocalStrategyLab />} />
        <Route path="backtest-runs" element={<BacktestRuns />} />
        <Route path="backtest-tasks" element={<BacktestTasks />} />
        <Route path="hyperopt-runs" element={<HyperoptRuns />} />
        <Route path="live-governance" element={<LiveGovernance />} />
        <Route path="operator-dashboard" element={<OperatorDashboard />} />
        <Route path="okx-demo" element={<OkxDemo />} />
        <Route path="ranking" element={<Ranking />} />
        <Route path="freq-ui" element={<Navigate replace to="/okx-demo?from=freq-ui" />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
