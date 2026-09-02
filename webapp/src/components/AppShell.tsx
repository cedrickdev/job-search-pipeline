import { NavLink } from "react-router-dom";
import { useState, type ReactNode } from "react";
import { useTheme } from "../theme/useTheme";
import { CopilotPanel } from "./CopilotPanel";
import "./AppShell.css";

const LINKS = [
  { to: "/", label: "Overview", end: true },
  { to: "/jobs", label: "Jobs", end: false },
  { to: "/analytics", label: "Analytics", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { theme, toggle } = useTheme();
  const [copilotOpen, setCopilotOpen] = useState(false);
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">⌘ Command Center</div>
        {LINKS.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            end={l.end}
            className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}
          >
            {l.label}
          </NavLink>
        ))}
      </aside>
      <div className="main">
        <div className="topbar">
          <button className="theme-btn" onClick={() => setCopilotOpen((o) => !o)}>
            ✦ Copilot
          </button>
          <button className="theme-btn" aria-label="Toggle theme" onClick={toggle}>
            {theme === "dark" ? "☾ Dark" : "☀ Light"}
          </button>
        </div>
        {children}
      </div>
      {copilotOpen && (
        <aside className="copilot-dock">
          <div className="copilot-dock-head">
            <span>Copilot</span>
            <button className="btn-ghost" onClick={() => setCopilotOpen(false)}>✕</button>
          </div>
          <CopilotPanel scope="global" />
        </aside>
      )}
    </div>
  );
}
