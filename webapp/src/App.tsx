import { Routes, Route } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { OverviewPage } from "./routes/OverviewPage";
import { JobsPage } from "./routes/JobsPage";
import { AnalyticsPage } from "./routes/AnalyticsPage";
import { SettingsPage } from "./routes/SettingsPage";

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </AppShell>
  );
}
