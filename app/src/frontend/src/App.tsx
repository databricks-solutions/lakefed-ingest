import { createContext, useContext, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Config } from "./api";
import Overview from "./pages/Overview";
import Runs from "./pages/Runs";
import Tables from "./pages/Tables";
import Jobs from "./pages/Jobs";
import AddTable from "./pages/AddTable";
import BulkImport from "./pages/BulkImport";

interface Settings { staleMinutes: number; config?: Config }
const SettingsContext = createContext<Settings>({ staleMinutes: 60 });
export const useSettings = () => useContext(SettingsContext);

const NAV = [
  ["/overview", "Overview"], ["/runs", "Runs"], ["/tables", "Tables"], ["/jobs", "Jobs"],
  ["/add", "Add table"], ["/bulk", "Bulk import"],
] as const;

export default function App() {
  const [staleMinutes, setStaleMinutes] = useState<number>(() => Number(localStorage.getItem("staleMinutes")) || 60);
  const config = useQuery({ queryKey: ["config"], queryFn: () => api.get<Config>("/config") });
  const qc = useQueryClient();
  return (
    <SettingsContext.Provider value={{ staleMinutes, config: config.data }}>
      <div className="layout">
        <aside className="sidebar">
          <div className="brand">lakefed_ingest</div>
          <nav>
            {NAV.map(([to, label]) => (
              <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>
            ))}
          </nav>
          <div className="sidebar-foot">
            <label>
              Stale after (minutes)
              <input type="number" min={5} step={5} value={staleMinutes}
                onChange={(e) => { const v = Math.max(5, Number(e.target.value) || 60); setStaleMinutes(v); localStorage.setItem("staleMinutes", String(v)); }} />
            </label>
            <button className="ghost" onClick={() => qc.invalidateQueries()}>Refresh data</button>
            {config.data && (
              <div className="muted small">
                Lakebase <code>{config.data.lakebase_instance}</code> / <code>{config.data.lakebase_database}</code>
                {config.data.user && <div>Signed in as {config.data.user}</div>}
              </div>
            )}
          </div>
        </aside>
        <main className="content">
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/tables" element={<Tables />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/add" element={<AddTable />} />
            <Route path="/bulk" element={<BulkImport />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </main>
      </div>
    </SettingsContext.Provider>
  );
}
