"use client";

import { useCallback, useMemo, useState } from "react";
import WorkspaceShell from "./workspace-shell";
import MarketChart from "./market-chart";
import MarketDataPanel from "./market-data-panel";

type MarketType = "SPOT" | "FUTURES";
type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

const timeframes: Timeframe[] = [
  "1m", "5m", "15m", "1h", "4h", "1d",
];

export default function MarketsWorkspace() {
  const [marketType, setMarketType] = useState<MarketType>("SPOT");
  const [search, setSearch] = useState("");
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");
  const [marketDataConnected, setMarketDataConnected] = useState(false);
  const handleConnectionChange = useCallback((connected: boolean) => {
    setMarketDataConnected(connected);
  }, []);

  const marketDescription = useMemo(
    () =>
      marketType === "SPOT"
        ? "Binance Spot instruments"
        : "Binance USDâ“ˆ-M and COIN-M Futures instruments",
    [marketType]
  );

  return (
    <WorkspaceShell activeSection="markets" connectionStatus={marketDataConnected}>
      <div className="dashboard-content markets-workspace">

        <section className="section-intro">
          <div className="eyebrow">MARKET DISCOVERY</div>
          <h1>Markets</h1>
          <p className="welcome-description">
            Discover and analyse supported Spot and Futures instruments
            using verified public exchange market data.
          </p>
        </section>

        <section className="markets-toolbar panel">
          <div className="market-type-switch" aria-label="Market type">
            <button
              type="button"
              className={marketType === "SPOT" ? "selected" : ""}
              aria-pressed={marketType === "SPOT"}
              onClick={() => setMarketType("SPOT")}
            >
              Spot
            </button>

            <button
              type="button"
              className={marketType === "FUTURES" ? "selected" : ""}
              aria-pressed={marketType === "FUTURES"}
              onClick={() => setMarketType("FUTURES")}
            >
              Futures
            </button>
          </div>

          <label className="market-search">
            <span aria-hidden="true">âŒ•</span>
            <input
              type="search"
              placeholder="Search symbol or instrument"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="Search market instruments"
            />
          </label>
        </section>

        <section className="market-status-row">
          <div>
            <div className="market-status-title">
              {marketType === "SPOT" ? "Spot Market" : "Futures Market"}
            </div>
            <div className="market-status-description">
              {marketDescription}
            </div>
          </div>

          <span className="panel-badge">{marketDataConnected ? "DATA CONNECTED" : "DATA NOT CONNECTED"}</span>
        </section>

        <MarketDataPanel marketType={marketType} search={search} onConnectionChange={handleConnectionChange} />

        <section className="market-chart-panel panel">
          <div className="panel-header market-chart-header">
            <div>
              <div className="panel-title">Market chart</div>
              <div className="panel-subtitle">
                Select a timeframe for chart analysis
              </div>
            </div>

            <span className="panel-badge">NO VERIFIED DATA</span>
          </div>

          <div className="timeframe-toolbar" aria-label="Chart timeframe">
            {timeframes.map((item) => (
              <button
                type="button"
                key={item}
                className={timeframe === item ? "selected" : ""}
                aria-pressed={timeframe === item}
                onClick={() => setTimeframe(item)}
              >
                {item}
              </button>
            ))}
          </div>

          <MarketChart candles={[]} timeframe={timeframe} />

          <div className="chart-footer">
            <span>Chart engine: Lightweight Charts</span>
            <span>Source: Not connected</span>
          </div>
        </section>

        <section className="market-instruments panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Market instruments</div>
              <div className="panel-subtitle">
                {marketType} instrument discovery
              </div>
            </div>

            <span className="panel-badge">NOT CONNECTED</span>
          </div>

          <div className="market-table-wrap">
            <table className="market-table">
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Last price</th>
                  <th>24h change</th>
                  <th>24h volume</th>
                  <th>Market</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td colSpan={5}>
                    <div className="market-table-empty">
                      <strong>
                        {search.trim()
                          ? "Search unavailable"
                          : "No verified instruments available"}
                      </strong>
                      <p>
                        {search.trim()
                          ? "Instrument search will become available after verified market discovery is integrated."
                          : "No market instruments will be displayed until the exchange data source is verified."}
                      </p>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Public market research only Â· No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}
