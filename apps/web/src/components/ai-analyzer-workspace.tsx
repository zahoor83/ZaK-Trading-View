"use client";

import { useMemo, useState } from "react";
import WorkspaceShell from "./workspace-shell";

type MarketType = "SPOT" | "FUTURES";
type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";
type AnalysisModule =
  | "Market structure"
  | "Trend analysis"
  | "Momentum"
  | "Volatility"
  | "Support and resistance"
  | "Risk assessment";

const timeframes: Timeframe[] = [
  "1m",
  "5m",
  "15m",
  "1h",
  "4h",
  "1d",
];

const modules: AnalysisModule[] = [
  "Market structure",
  "Trend analysis",
  "Momentum",
  "Volatility",
  "Support and resistance",
  "Risk assessment",
];

export default function AIAnalyzerWorkspace() {
  const [marketType, setMarketType] = useState<MarketType>("SPOT");
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");
  const [selectedModules, setSelectedModules] =
    useState<AnalysisModule[]>(modules);

  const normalizedSymbol = symbol.trim().toUpperCase();

  const symbolIsValid = /^[A-Z0-9]{2,30}$/.test(normalizedSymbol);

  const selectionSummary = useMemo(
    () => `${selectedModules.length} of ${modules.length} modules selected`,
    [selectedModules.length]
  );

  function toggleModule(module: AnalysisModule) {
    setSelectedModules((current) =>
      current.includes(module)
        ? current.filter((item) => item !== module)
        : [...current, module]
    );
  }

  return (
    <WorkspaceShell activeSection="ai-analyzer">
      <div className="dashboard-content analyzer-workspace">
        <section className="section-intro">
          <div className="eyebrow">EVIDENCE-BASED MARKET INTELLIGENCE</div>
          <h1>AI Analyzer</h1>
          <p className="welcome-description">
            Configure a structured market analysis with explicit evidence
            requirements and risk context.
          </p>
        </section>

        <section className="analyzer-status panel">
          <div className="analyzer-status-icon" aria-hidden="true">
            !
          </div>
          <div className="analyzer-status-copy">
            <strong>Analysis currently blocked</strong>
            <p>
              Trusted market data is not connected. No analysis, trading
              signal, confidence score or price prediction will be generated.
            </p>
          </div>
          <span className="panel-badge">DATA REQUIRED</span>
        </section>

        <section className="analyzer-config panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Analysis configuration</div>
              <div className="panel-subtitle">
                Select the market inputs and analysis scope.
              </div>
            </div>
            <span className="panel-badge">CONFIGURATION ONLY</span>
          </div>

          <div className="analyzer-field">
            <label className="analyzer-label">Market type</label>
            <div className="market-type-switch" aria-label="Analysis market">
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
          </div>

          <div className="analyzer-field">
            <label className="analyzer-label" htmlFor="analyzer-symbol">
              Instrument symbol
            </label>
            <input
              id="analyzer-symbol"
              className="analyzer-input"
              type="text"
              placeholder="Enter symbol, e.g. BTCUSDT"
              value={symbol}
              onChange={(event) => setSymbol(event.target.value)}
              maxLength={30}
              autoComplete="off"
              spellCheck={false}
            />
            <span className="analyzer-field-hint">
              {normalizedSymbol
                ? symbolIsValid
                  ? "Symbol format accepted. Exchange validity is not verified."
                  : "Enter 2–30 letters or numbers."
                : "No instrument selected."}
            </span>
          </div>

          <div className="analyzer-field">
            <label className="analyzer-label">Analysis timeframe</label>
            <div className="timeframe-toolbar" aria-label="Analysis timeframe">
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
          </div>
        </section>

        <section className="analyzer-modules panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Analysis modules</div>
              <div className="panel-subtitle">
                Choose which areas a future verified analysis should evaluate.
              </div>
            </div>
            <span className="panel-badge">{selectionSummary}</span>
          </div>

          <div className="analyzer-module-grid">
            {modules.map((module) => {
              const selected = selectedModules.includes(module);

              return (
                <button
                  type="button"
                  key={module}
                  className={`analyzer-module${selected ? " selected" : ""}`}
                  aria-pressed={selected}
                  onClick={() => toggleModule(module)}
                >
                  <span className="analyzer-module-check" aria-hidden="true">
                    {selected ? "✓" : "+"}
                  </span>
                  <span>{module}</span>
                </button>
              );
            })}
          </div>
        </section>

        <section className="analyzer-results panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Analysis results</div>
              <div className="panel-subtitle">
                Evidence and findings will appear here only after validation.
              </div>
            </div>
            <span className="panel-badge">BLOCKED</span>
          </div>

          <div className="analyzer-results-empty" role="status">
            <div className="analyzer-empty-icon" aria-hidden="true">
              !
            </div>
            <h2>No verified analysis available</h2>
            <p>
              Connect and validate trusted market data before requesting
              technical analysis. Missing, stale or contradictory inputs
              must prevent actionable conclusions.
            </p>
            <span>
              Selected market: {marketType} · Timeframe: {timeframe}
            </span>
          </div>

          <div className="analyzer-result-grid">
            {[
              "Market structure",
              "Trend",
              "Momentum",
              "Volatility",
              "Support / resistance",
              "Risk context",
            ].map((item) => (
              <div className="analyzer-result-item" key={item}>
                <span>{item}</span>
                <strong>Unavailable</strong>
              </div>
            ))}
          </div>
        </section>

        <section className="analyzer-action panel">
          <div>
            <strong>Analysis cannot run yet</strong>
            <p>
              {normalizedSymbol && !symbolIsValid
                ? "Correct the symbol format before continuing."
                : selectedModules.length === 0
                  ? "Select at least one analysis module."
                  : "A verified market-data connection and validated inputs are required."}
            </p>
          </div>
          <button
            type="button"
            className="analyzer-run-button"
            disabled
            title="Blocked until trusted market data is validated"
          >
            Analysis unavailable
          </button>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Evidence required · No fabricated analysis · No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}