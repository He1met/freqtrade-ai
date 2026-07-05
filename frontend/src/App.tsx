import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./layout/AppLayout";
import { BacktestRuns } from "./pages/BacktestRuns";
import { BacktestTasks } from "./pages/BacktestTasks";
import { Dashboard } from "./pages/Dashboard";
import { FreqUILink } from "./pages/FreqUILink";
import { GenerationRuns } from "./pages/GenerationRuns";
import { HyperoptRuns } from "./pages/HyperoptRuns";
import { LiveGovernance } from "./pages/LiveGovernance";
import { OperatorDashboard } from "./pages/OperatorDashboard";
import { Ranking } from "./pages/Ranking";
import { Strategies } from "./pages/Strategies";
import { StrategyDetail } from "./pages/StrategyDetail";

export function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="strategies" element={<Strategies />} />
        <Route path="strategies/:strategyId" element={<StrategyDetail />} />
        <Route path="generation-runs" element={<GenerationRuns />} />
        <Route path="backtest-runs" element={<BacktestRuns />} />
        <Route path="backtest-tasks" element={<BacktestTasks />} />
        <Route path="hyperopt-runs" element={<HyperoptRuns />} />
        <Route path="live-governance" element={<LiveGovernance />} />
        <Route path="operator-dashboard" element={<OperatorDashboard />} />
        <Route path="ranking" element={<Ranking />} />
        <Route path="freq-ui" element={<FreqUILink />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
