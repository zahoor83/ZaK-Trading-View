"use client";

import { useMemo, useState } from "react";
import WorkspaceShell from "./workspace-shell";

type MarketType = "ALL" | "SPOT" | "FUTURES";
type Direction = "ALL" | "LONG" | "SHORT" | "WAIT";
type Timeframe = "ALL" | "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

const directions: Direction[] = ["ALL", "LONG", "SHORT", "WAIT"];

const timeframes: Timeframe[] = [
  "ALL", "1m", "5m", "15m", "1h", "4h", "1d",
];

export default function SignalsWorkspace() {
  const [market, setMarket] = useState<MarketType>("ALL");
  const [direction, setDirection] = useState<Direction>("ALL");
  const [timeframe, setTimeframe] = useState<Timeframe>("ALL");
  const [search, setSearch] = useState("");

  const activeFilters = useMemo(() => {
    const filters = [];

    if (market !== "ALL") filters.push(market);
    if (direction !== "ALL") filters.push(direction);
    if (timeframe !== "ALL") filters.push(timeframe);
    if (search.trim()) filters.push(`Search: ${search.trim().toUpperCase()}`);

    return filters;
  }, [market, direction, timeframe, search]);

  function resetFilters() {
    setMarket("ALL");
    setDirection("ALL");
    setTimeframe("ALL");
    setSearch("");
  }

  return (
    <WorkspaceShell activeSection="signals">
      <div className="dashboard-content signals-workspace">
        <section className="section-intro">
          <div className="eyebrow">MARKET RESEARCH AND SIGNAL HISTORY</div>
          <h1>Signals</h1>
          <p className="welcome-description">
            Review evidence-backed market opportunities, their risk context
            and lifecycle. Signals are research information, not exchange orders.
          </p>
        </section>

        <section className="signals-status panel">
          <div className="signals-status-indicator" aria-hidden="true">!</div>
          <div className="signals-status-copy">
            <strong>No verified signals available</strong>
            <p>
              Signal generation is unavailable until trusted market data
              and a validated analysis engine are connected.
            </p>
          </div>
          <span className="panel-badge">NOT CONNECTED</span>
        </section>

        <section className="signals-overview">
          <div className="metric-card">
            <div className="metric-label">Verified signals</div>
            <div className="metric-value">Unavailable</div>
            <div className="metric-detail">No verified signal feed</div>
          </div>

          <div className="metric-card">
            <div className="metric-label">Signal source</div>
            <div className="metric-value">Offline</div>
            <div className="metric-detail">Provider not connected</div>
          </div>

          <div className="metric-card">
            <div className="metric-label">Signal validation</div>
            <div className="metric-value">Blocked</div>
            <div className="metric-detail">Evidence validation required</div>
          </div>
        </section>

        <section className="signals-filters panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Signal discovery</div>
              <div className="panel-subtitle">
                Narrow the research history by market, direction and timeframe.
              </div>
            </div>
            <span className="panel-badge">FILTERS ONLY</span>
          </div>

          <div className="signals-filter-grid">
            <label className="signals-filter-field">
              <span>Market</span>
              <select
                value={market}
                onChange={(event) =>
                  setMarket(event.target.value as MarketType)
                }
              >
                <option value="ALL">All markets</option>
                <option value="SPOT">Spot</option>
                <option value="FUTURES">Futures</option>
              </select>
            </label>

            <label className="signals-filter-field">
              <span>Direction</span>
              <select
                value={direction}
                onChange={(event) =>
                  setDirection(event.target.value as Direction)
                }
              >
                {directions.map((item) => (
                  <option value={item} key={item}>
                    {item === "ALL" ? "All directions" : item}
                  </option>
                ))}
              </select>
            </label>

            <label className="signals-filter-field">
              <span>Timeframe</span>
              <select
                value={timeframe}
                onChange={(event) =>
                  setTimeframe(event.target.value as Timeframe)
                }
              >
                {timeframes.map((item) => (
                  <option value={item} key={item}>
                    {item === "ALL" ? "All timeframes" : item}
                  </option>
                ))}
              </select>
            </label>

            <label className="signals-filter-field">
              <span>Instrument search</span>
              <input
                type="search"
                placeholder="Search symbol"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                maxLength={30}
                autoComplete="off"
              />
            </label>
          </div>

          <div className="signals-filter-bottom">
            <div className="signals-active-filters">
              {activeFilters.length === 0
                ? "No filters applied"
                : `${activeFilters.length} filter(s) applied`}
            </div>

            <button
              type="button"
              className="signals-reset-button"
              onClick={resetFilters}
              disabled={activeFilters.length === 0}
            >
              Reset filters
            </button>
          </div>

          {activeFilters.length > 0 && (
            <div className="signals-filter-chips" aria-label="Active filters">
              {activeFilters.map((filter) => (
                <span key={filter}>{filter}</span>
              ))}
            </div>
          )}
        </section>

        <section className="signals-history panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Signal history</div>
              <div className="panel-subtitle">
                Validated research records and their lifecycle.
              </div>
            </div>
            <span className="panel-badge">NO VERIFIED RECORDS</span>
          </div>

          <div className="signals-table-wrap">
            <table className="signals-table signals-table-empty">
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Market</th>
                  <th>Direction</th>
                  <th>Timeframe</th>
                  <th>Entry</th>
                  <th>Invalidation</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td colSpan={7}>
                    <div className="signals-empty">
                      <div className="signals-empty-icon" aria-hidden="true">
                        !
                      </div>
                      <strong>No verified signal records</strong>
                      <p>
                        {activeFilters.length > 0
                          ? "Your filters are selected, but there is no verified signal history to search."
                          : "Signal records will appear after market data and signal validation are implemented."}
                      </p>
                      <span>
                        No synthetic entries, targets, prices or performance
                        results are displayed.
                      </span>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section className="signals-evidence panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Signal evidence requirements</div>
              <div className="panel-subtitle">
                Every future signal must be supported by verifiable inputs.
              </div>
            </div>
            <span className="panel-badge">REQUIRED</span>
          </div>

          <div className="signals-evidence-grid">
            {[
              ["Market source", "Verified exchange data and instrument metadata"],
              ["Timestamp", "Source timestamps and freshness validation"],
              ["Entry and invalidation", "Evidence-backed levels with defined conditions"],
              ["Targets and expiry", "Explicit calculation method and validity window"],
              ["Risk context", "Risk/reward assumptions and invalidation conditions"],
              ["Signal lifecycle", "Created, active, invalidated or expired states"],
            ].map(([title, description]) => (
              <div className="signals-evidence-item" key={title}>
                <strong>{title}</strong>
                <p>{description}</p>
              </div>
            ))}
          </div>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Research only · No fabricated signals · No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}