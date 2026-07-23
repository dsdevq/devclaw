import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { fetchControl, tokenQueryString, type ControlState } from "../api";
import {
  IconAlert,
  IconEvals,
  IconGoals,
  IconMoon,
  IconNode,
  IconOverview,
  IconProjects,
  IconSettings,
  IconSun,
} from "../icons";
import { useTheme } from "../theme";
import { StatusDot } from "../ui";

const NAV = [
  { to: "/", label: "Overview", Icon: IconOverview, end: true },
  { to: "/node", label: "Node", Icon: IconNode, end: false },
  { to: "/projects", label: "Projects", Icon: IconProjects, end: false },
  { to: "/goals", label: "Goals", Icon: IconGoals, end: false },
  { to: "/evals", label: "Evals", Icon: IconEvals, end: false },
  { to: "/problems", label: "Problems", Icon: IconAlert, end: false },
  { to: "/settings", label: "Settings", Icon: IconSettings, end: false },
];

function dispatchState(c: ControlState | null): { label: string; color: string; live: boolean } {
  if (!c) return { label: "…", color: "var(--text-muted)", live: false };
  if (c.operatorHold.on) return { label: "Paused", color: "var(--amber)", live: false };
  if (c.blocked && c.schedule.enabled) return { label: "Off-hours", color: "var(--amber)", live: false };
  if (c.blocked) return { label: "Quota held", color: "var(--amber)", live: false };
  return { label: "Running", color: "var(--green)", live: true };
}

function useControl(): ControlState | null {
  const [ctrl, setCtrl] = useState<ControlState | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => fetchControl().then((c) => alive && setCtrl(c)).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return ctrl;
}

function crumb(pathname: string): string {
  const seg = pathname.replace(/^\//, "").split("/").filter(Boolean);
  if (seg.length === 0) return "Overview";
  const head = seg[0][0].toUpperCase() + seg[0].slice(1);
  return seg[1] ? `${head} › ${decodeURIComponent(seg[1])}` : head;
}

export function AppShell() {
  const loc = useLocation();
  const { theme, toggle } = useTheme();
  const ctrl = useControl();
  const d = dispatchState(ctrl);
  const qs = tokenQueryString();

  const navLinks = NAV.map(({ to, label, Icon, end }) => (
    <NavLink
      key={to}
      to={`${to}${qs}`}
      end={end}
      className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
    >
      <Icon />
      {label}
    </NavLink>
  ));

  return (
    <div className="shell">
      <aside className="sidebar">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "0 18px",
            height: "var(--topbar-h)",
            flexShrink: 0,
          }}
        >
          <StatusDot color="var(--accent)" live />
          <span style={{ fontWeight: 650, fontSize: 14.5, letterSpacing: "-0.01em" }}>
            devclaw
          </span>
        </div>
        <nav style={{ flex: 1, paddingTop: 6, overflowY: "auto" }}>{navLinks}</nav>
        <Link
          to={`/settings${qs}`}
          className="mono"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "12px 18px",
            borderTop: "1px solid var(--border)",
            fontSize: 12,
            color: "var(--text-secondary)",
          }}
        >
          <StatusDot color={d.color} live={d.live} />
          Dispatch · {d.label}
        </Link>
      </aside>

      <div className="content">
        <nav className="mobile-nav">{navLinks}</nav>
        <header className="topbar">
          <div className="secondary" style={{ fontSize: 13.5, fontWeight: 500 }}>
            {crumb(loc.pathname)}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Link
              to={`/settings${qs}`}
              className="badge"
              style={{ textDecoration: "none" }}
            >
              <StatusDot color={d.color} live={d.live} />
              {d.label}
            </Link>
            <button
              className="btn ghost sm"
              onClick={toggle}
              aria-label="Toggle theme"
              style={{ width: 32, padding: 0 }}
            >
              {theme === "dark" ? <IconSun /> : <IconMoon />}
            </button>
          </div>
        </header>
        <div className="scroll">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
