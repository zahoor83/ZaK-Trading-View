import WorkspaceShell from "../components/workspace-shell";

const marketCards = [
  {
    name: "Market data",
    value: "Unavailable",
    detail: "Connection not configured",
  },
  {
    name: "Active signals",
    value: "Unavailable",
    detail: "Awaiting verified analysis",
  },
  {
    name: "Paper balance",
    value: "Unavailable",
    detail: "Virtual ledger not connected",
  },
  {
    name: "Portfolio P&L",
    value: "Unavailable",
    detail: "No verified portfolio data",
  },
];

export default function DashboardPage() {
  return (
    <WorkspaceShell activeSection="overview">
      <div className="dashboard-content">
        <section className="welcome-section">
          <div>
            <div className="eyebrow">MARKET INTELLIGENCE WORKSPACE</div>
            <h1>Market overview</h1>
            <p className="welcome-description">
              Research markets, evaluate AI-generated setups and manage
              simulated trading from one workspace.
            </p>
          </div>

          <div className="session-chip">
            <span className="status-dot offline" />
            Awaiting data connection
          </div>
        </section>

        <section className="metrics-grid" aria-label="Workspace summary">
          {marketCards.map((card) => (
            <article className="metric-card" key={card.name}>
              <div className="metric-heading">{card.name}</div>
              <div className="metric-value unavailable">{card.value}</div>
              <div className="metric-detail">{card.detail}</div>
            </article>
          ))}
        </section>

        <section className="primary-grid">
          <article className="panel chart-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Market chart</div>
                <div className="panel-subtitle">
                  Instrument data will appear here
                </div>
              </div>
              <span className="panel-badge">NO DATA</span>
            </div>

            <div className="chart-empty-state">
              <div className="empty-chart-icon">⌁</div>
              <h2>Market data unavailable</h2>
              <p>
                A verified market-data connection is required before prices
                and candles can be displayed.
              </p>
            </div>

            <div className="chart-footer">
              <span>Chart engine: Lightweight Charts</span>
              <span>Source: Not connected</span>
            </div>
          </article>

          <article className="panel watchlist-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Watchlist</div>
                <div className="panel-subtitle">
                  Your tracked instruments
                </div>
              </div>
              <button className="text-button" type="button" disabled>
                Add symbol
              </button>
            </div>

            <div className="watchlist-empty">
              <span className="empty-star">☆</span>
              <strong>No instruments yet</strong>
              <p>
                Symbols will be available after market discovery is implemented.
              </p>
            </div>
          </article>
        </section>

        <section className="secondary-grid">
          <article className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">AI market analysis</div>
                <div className="panel-subtitle">
                  Evidence-based setup evaluation
                </div>
              </div>
              <span className="panel-badge">PENDING</span>
            </div>
            <div className="compact-empty">
              Analysis is unavailable until verified market inputs are connected.
            </div>
          </article>

          <article className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Recent signals</div>
                <div className="panel-subtitle">LONG · SHORT · WAIT</div>
              </div>
              <span className="panel-badge">PENDING</span>
            </div>
            <div className="compact-empty">
              No signals are being displayed. No synthetic signals are generated.
            </div>
          </article>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Signal research and simulated trading only · No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}