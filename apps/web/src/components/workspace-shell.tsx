import type { ReactNode } from "react";

export type WorkspaceSection =
  | "overview"
  | "markets"
  | "watchlists"
  | "ai-analyzer"
  | "signals"
  | "paper-trading"
  | "alerts";

const navigation: {
  label: string;
  icon: string;
  href: string;
  id: WorkspaceSection;
}[] = [
  { id: "overview", label: "Overview", icon: "◫", href: "/" },
  { id: "markets", label: "Markets", icon: "◈", href: "/markets" },
  { id: "watchlists", label: "Watchlists", icon: "☆", href: "/watchlists" },
  { id: "ai-analyzer", label: "AI Analyzer", icon: "✳", href: "/ai-analyzer" },
  { id: "signals", label: "Signals", icon: "⌁", href: "/signals" },
  { id: "paper-trading", label: "Paper Trading", icon: "▤", href: "/paper-trading" },
  { id: "alerts", label: "Alerts", icon: "♧", href: "/alerts" },
];

interface WorkspaceShellProps {
  activeSection: WorkspaceSection;
  children: ReactNode;
  connectionStatus?: boolean;
}

export default function WorkspaceShell({
  activeSection,
  children,
  connectionStatus = false,
}: WorkspaceShellProps) {
  const activeItem = navigation.find(
    (item) => item.id === activeSection
  );

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="ZaK Trading View home">
          <span className="brand-mark">Z</span>
          <span>
            <strong>ZaK Trading View</strong>
            <small>AI MARKET INTELLIGENCE</small>
          </span>
        </a>

        <div className="workspace-label">WORKSPACE</div>

        <nav className="navigation" aria-label="Main navigation">
          {navigation.map((item) => {
            const active = item.id === activeSection;

            return (
              <a
                href={item.href}
                key={item.id}
                className={`nav-item${active ? " active" : ""}`}
                aria-current={active ? "page" : undefined}
              >
                <span className="nav-icon" aria-hidden="true">
                  {item.icon}
                </span>
                <span>{item.label}</span>
                {item.id === "paper-trading" && (
                  <span className="nav-tag">SIM</span>
                )}
              </a>
            );
          })}
        </nav>

        <div className="sidebar-bottom">
          <div className="connection-indicator">
            <span className={`status-dot ${connectionStatus ? "online" : "offline"}`} />
            <span>{connectionStatus ? "Market data connected" : "Data not connected"}</span>
          </div>
          <div className="sidebar-footer">
            Independent market analysis platform
          </div>
        </div>
      </aside>

      <section className="main-area">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Workspace</span>
            <span className="breadcrumb-separator">/</span>
            <strong>{activeItem?.label ?? "Overview"}</strong>
          </div>

          <div className="topbar-actions">
            <span className="environment-label">PAPER ENVIRONMENT</span>
            <button
              className="profile-button"
              type="button"
              aria-label="User profile"
            >
              Z
            </button>
          </div>
        </header>

        {children}
      </section>
    </main>
  );
}