import { createContext, useContext, useState } from "react";
import { PageErrorBoundary } from "./errorBoundary";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Config } from "./api";
import { Menu, PageStampContext, Stamp, Toaster, type PageStampValue } from "./components";
import Overview from "./pages/Overview";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Tables from "./pages/Tables";
import AddTable from "./pages/AddTable";
import BulkImport from "./pages/BulkImport";
import Changes from "./pages/Changes";
import Schedules from "./pages/Schedules";
import Timeline from "./pages/Timeline";
import Collection from "./pages/Collection";
import IngestionJobs from "./pages/IngestionJobs";
import IngestionJob from "./pages/IngestionJob";
import NotInJob from "./pages/NotInJob";
import JobEdit from "./pages/JobEdit";
import NewJob from "./pages/NewJob";
import JobRun from "./pages/JobRun";
import Sources from "./pages/Sources";
import Database from "./pages/Database";
import ImportCsv from "./pages/ImportCsv";
import Manage from "./pages/Manage";
import { usePermissions } from "./permissions";

interface Settings { config?: Config }
const SettingsContext = createContext<Settings>({});
export const useSettings = () => useContext(SettingsContext);

// The left menu's links, in order, with no group labels: one list, so a link is added with one entry.
// Add table, Bulk import and Import CSV are Add tables inside New ingestion job and Edit; their pages stay for
// groups in no job, reached from a group's page and from Sources.
const NAV: [string, string][] = [
  ["/overview", "Overview"], ["/ingestion-jobs", "Ingestion jobs"], ["/runs", "Runs"], ["/sources", "Sources"],
  ["/timeline", "Timeline"], ["/tables", "Tables"], ["/schedules", "Schedules"], ["/changes", "Changes"],
];

// Pages under an ingestion job keep Ingestion jobs selected, as the workspace keeps a job's section selected on its pages.
// A group run (/runs/<id>) is under its job too, as its crumbs say; the Runs list itself stays under Runs.
const UNDER = { "/ingestion-jobs": ["/ingestion-jobs", "/collections", "/runs/", "/add", "/bulk", "/import"] } as Record<string, string[]>;

export default function App() {
  const config = useQuery({ queryKey: ["config"], queryFn: () => api.get<Config>("/config") });
  const qc = useQueryClient();
  const { pathname } = useLocation();
  const [stamp, setStamp] = useState<PageStampValue | null>(null);
  // Manage the app is listed only for those with Can Manage on the app; its page and its calls refuse anyone else.
  const manages = usePermissions().data?.manage === null;
  const nav: [string, string][] = manages ? [...NAV, ["/manage", "Manage the app"]] : NAV;
  return (
    <SettingsContext.Provider value={{ config: config.data }}>
      <PageStampContext.Provider value={setStamp}>
      <div className="layout">
        <aside className="sidebar">
          <div className="brand"><div>Lakefed Ingest<small>Operations</small></div></div>
          <nav>
            {nav.map(([to, label]) => (
              <NavLink key={to} to={to} end className={({ isActive }) =>
                (isActive || (UNDER[to] ?? []).some((p) => pathname.startsWith(p)) ? "active" : "")}>{label}</NavLink>
            ))}
          </nav>
          {/* When the data was read and Refresh data, at the bottom of the menu, out of the page's way. */}
          <div className="sidebar-foot">
            {stamp && <Stamp at={stamp.at} every={stamp.every} error={stamp.error} />}
            <button className="ghost" onClick={() => qc.invalidateQueries()}>Refresh data</button>
          </div>
        </aside>
        <main className="content">
          {/* As the Databricks workspace's top bar: actions and the signed-in user at the top right. */}
          <div className="topbar">
            <UserMenu config={config.data} />
          </div>
          {/* A page that throws shows what happened instead of a white window; a new page starts clean. */}
          <PageErrorBoundary key={pathname}>
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/ingestion-jobs" element={<IngestionJobs />} />
            <Route path="/ingestion-jobs/not-in-job" element={<NotInJob />} />
            <Route path="/ingestion-jobs/new" element={<NewJob />} />
            <Route path="/ingestion-jobs/:id" element={<IngestionJob />} />
            <Route path="/ingestion-jobs/:id/edit" element={<JobEdit />} />
            <Route path="/ingestion-jobs/:id/runs/:slot" element={<JobRun />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/tables" element={<Tables />} />
            <Route path="/schedules" element={<Schedules />} />
            {/* The two pages it replaces, for old links and bookmarks. */}
            <Route path="/jobs" element={<Navigate to="/schedules" replace />} />
            <Route path="/schedules-and-jobs" element={<Navigate to="/schedules" replace />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/collections/:name" element={<Collection />} />
            <Route path="/collections/:name/databases/:server/:database" element={<Database />} />
            <Route path="/sources" element={<Sources />} />
            <Route path="/add" element={<AddTable />} />
            <Route path="/bulk" element={<BulkImport />} />
            <Route path="/import" element={<ImportCsv />} />
            <Route path="/changes" element={<Changes />} />
            <Route path="/manage" element={<Manage />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
          </PageErrorBoundary>
        </main>
        <Toaster />
      </div>
      </PageStampContext.Provider>
    </SettingsContext.Provider>
  );
}


/** The signed-in user's menu, as the Databricks workspace shows it at the top right: their initials, opening to who
    they are and which Lakebase database this app reads. */
function UserMenu({ config }: { config?: Config }) {
  const user = config?.user ?? "";
  // Blank until the user is known, so the first paint never shows a placeholder as if it were someone.
  const initials = user.split("@")[0].split(/[._-]/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join("");
  return (
    <Menu label={initials} buttonClass="avatar" title={user || "Your account"}>
      <div className="menu-note"><div className="muted small">Signed in as</div><div>{user || "unknown"}</div></div>
      {config && <div className="menu-note"><div className="muted small">Lakebase</div>
        <div className="mono">{config.lakebase_instance}</div><div className="mono">{config.lakebase_database}</div></div>}
    </Menu>
  );
}
