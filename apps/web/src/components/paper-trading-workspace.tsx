"use client";

import { useEffect, useRef, useState } from "react";
import WorkspaceShell from "./workspace-shell";
import {
  calculateLedgerSummary,
  closePaperPosition,
  createLedger,
  openPaperPosition,
  refreshPaperBalance,
} from "../lib/paper-trading/ledger";
import {
  loadLedger,
  saveLedger,
} from "../lib/paper-trading/storage";
import { LedgerError } from "../lib/paper-trading/ledger";
import type {
  PaperLedger,
  TradeMarket,
  TradeSide,
} from "../lib/paper-trading/types";

const INITIAL_CAPITAL = 10_000;
const SIMULATION_FEE = 0.001;

function money(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export default function PaperTradingWorkspace() {
  const [ledger, setLedger] = useState<PaperLedger | null>(null);
  const [loading, setLoading] = useState(true);
  const [storageReady, setStorageReady] = useState(false);
  const [error, setError] = useState("");
  const [market, setMarket] = useState<TradeMarket>("SPOT");
  const [side, setSide] = useState<TradeSide>("LONG");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [entryPrice, setEntryPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [leverage, setLeverage] = useState("1");
  const [exitPrices, setExitPrices] = useState<Record<string, string>>({});
  const [pendingRefresh, setPendingRefresh] = useState(false);
  const refreshInProgressRef = useRef(false);
  const [pendingClose, setPendingClose] = useState<{
    positionId: string;
    exitPrice: string;
    grossPnl: number;
    exitFee: number;
    netPnl: number;
  } | null>(null);

  useEffect(() => {
    try {
      const restored = loadLedger(window.localStorage);
      setLedger(restored);
      setStorageReady(true);
    } catch (err) {
      setStorageReady(false);
      setError(
        err instanceof Error
          ? err.message
          : "Unable to restore the saved paper account.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  function persist(next: PaperLedger): boolean {
    try {
      saveLedger(window.localStorage, next);
      setLedger(next);
      setError("");
      return true;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to save the paper account.",
      );
      return false;
    }
  }

  function createAccount() {
    try {
      const next = createLedger(INITIAL_CAPITAL, SIMULATION_FEE);
      if (persist(next)) {
        setStorageReady(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Account creation failed.");
    }
  }

  function requestRefresh() {
    if (!ledger || ledger.cashBalance > 100) return;
    setPendingRefresh(true);
    setError("");
  }

  function cancelRefresh() {
    if (refreshInProgressRef.current) return;
    setPendingRefresh(false);
  }

  function confirmRefresh() {
    if (refreshInProgressRef.current || !ledger || !pendingRefresh) return;

    refreshInProgressRef.current = true;

    try {
      const next = refreshPaperBalance(ledger, 10_000, 100);

      if (next === ledger) {
        setPendingRefresh(false);
        setError("Refresh Balance is available only when cash is 100 USDT or below.");
        return;
      }

      if (persist(next)) {
        setPendingRefresh(false);
      }
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to refresh the paper balance.",
      );
    } finally {
      refreshInProgressRef.current = false;
    }
  }
  function submitTrade(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!ledger) return;

    try {
      const next = openPaperPosition(ledger, {
        symbol,
        market,
        side,
        entryPrice,
        quantity,
        leverage: Number(leverage),
      });

      if (persist(next)) {
        setEntryPrice("");
        setQuantity("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to open position.");
    }
  }

  function requestClose(positionId: string) {
    if (!ledger) return;

    try {
      const rawPrice = exitPrices[positionId];

      if (typeof rawPrice !== "string" || rawPrice.trim() === "") {
        throw new LedgerError("Enter an exit price before reviewing the close.");
      }

      const previewLedger = closePaperPosition(ledger, {
        positionId,
        exitPrice: rawPrice,
      });

      const preview = previewLedger.positions.find(
        (item) => item.id === positionId,
      );

      if (!preview || preview.status !== "CLOSED") {
        throw new LedgerError("Unable to calculate the close preview.");
      }

      setPendingClose({
        positionId,
        exitPrice: rawPrice,
        grossPnl: preview.grossPnl ?? 0,
        exitFee: preview.exitFee ?? 0,
        netPnl: preview.netPnl ?? 0,
      });

      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to review position close.");
    }
  }

  function confirmClose() {
    if (!ledger || !pendingClose) return;

    try {
      const next = closePaperPosition(ledger, {
        positionId: pendingClose.positionId,
        exitPrice: pendingClose.exitPrice,
      });

      if (persist(next)) {
        const closedId = pendingClose.positionId;

        setExitPrices((current) => {
          const updated = { ...current };
          delete updated[closedId];
          return updated;
        });

        setPendingClose(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to close position.");
    }
  }

  const summary = ledger ? calculateLedgerSummary(ledger) : null;
  const openPositions = ledger?.positions.filter((p) => p.status === "OPEN") ?? [];
  const closedPositions = ledger?.positions.filter((p) => p.status === "CLOSED") ?? [];

  return (
    <WorkspaceShell activeSection="paper-trading">
      <div className="dashboard-content paper-workspace">
        <section className="section-intro">
          <div className="eyebrow">SIMULATED TRADING</div>
          <h1>Paper Trading</h1>
          <p className="welcome-description">
            Practice Spot and Futures strategies using a virtual wallet.
            No exchange orders or real funds are involved.
          </p>
        </section>

        <div className="paper-safety-banner" role="status">
          <strong>PAPER ENVIRONMENT ONLY</strong>
          <span>
            Simulated prices are manually entered. No live execution,
            exchange balance or verified market valuation.
          </span>
        </div>

        {error && (
          <div className="paper-error" role="alert">
            <strong>Action could not be completed</strong>
            <span>{error}</span>
          </div>
        )}

        {loading ? (
          <section className="panel paper-loading" role="status">
            Loading saved paper accountâ€¦
          </section>
        ) : !storageReady ? (
          <section className="panel paper-account-gate">
            <div className="eyebrow">ACCOUNT RECOVERY REQUIRED</div>
            <h2>Paper account unavailable</h2>
            <p>
              The saved account could not be safely restored, or browser
              storage is unavailable. Existing stored data has not been
              automatically replaced.
            </p>
            <p>
              Resolve the storage problem before continuing. Do not create
              a replacement account over potentially recoverable data.
            </p>
          </section>
        ) : !ledger ? (
          <section className="panel paper-account-gate">
            <div className="eyebrow">VIRTUAL ACCOUNT</div>
            <h2>Create your paper account</h2>
            <p>
              Start with {money(INITIAL_CAPITAL)} virtual USDT.
              This is simulated capital, not real money.
            </p>
            <ul>
              <li>Default simulation fee: 0.10% per execution.</li>
              <li>Spot LONG and Futures Isolated LONG/SHORT.</li>
              <li>Futures leverage: 1x to 20x.</li>
              <li>No live exchange connection or real orders.</li>
            </ul>
            <button
              type="button"
              className="paper-primary-button"
              onClick={createAccount}
            >
              Create Paper Account
            </button>
          </section>
        ) : summary ? (
          <>
            {summary.cashBalance <= 100 && (
              <section className="panel paper-refresh-panel">
                <div className="panel-header">
                  <div>
                    <div className="panel-title">Paper wallet low balance</div>
                    <div className="panel-subtitle">
                      Available cash is 100 USDT or below.
                    </div>
                  </div>
                  <button
                    type="button"
                    className="paper-primary-button"
                    onClick={requestRefresh}
                    disabled={pendingRefresh}
                  >
                    Refresh Balance
                  </button>
                </div>
              </section>
            )}

            {pendingRefresh && (
              <section
                className="paper-close-confirmation"
                role="alertdialog"
                aria-labelledby="paper-refresh-title"
                aria-describedby="paper-refresh-description"
              >
                <strong id="paper-refresh-title">
                  Confirm Paper Balance Refresh
                </strong>
                <p id="paper-refresh-description">
                  This adds virtual funds only. Existing open positions,
                  locked margin, closed trade history, realized P&amp;L and
                  simulated fees will be preserved. No real funds or exchange
                  balance are involved.
                </p>
                <div className="paper-position-details">
                  <span>
                    Current available cash: {money(summary.cashBalance)} USDT
                  </span>
                  <span>Target available cash: 10,000.00 USDT</span>
                  <span>
                    Virtual credit to add: {money(10000 - summary.cashBalance)} USDT
                  </span>
                </div>
                <div className="paper-close-confirmation-actions">
                  <button
                    type="button"
                    className="paper-secondary-button"
                    onClick={cancelRefresh}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="paper-primary-button"
                    onClick={confirmRefresh}
                    disabled={refreshInProgressRef.current}
                  >
                    Confirm Refresh
                  </button>
                </div>
              </section>
            )}
            <section className="paper-summary-grid">
              <article className="metric-card">
                <div className="metric-heading">AVAILABLE CASH</div>
                <div className="metric-value">{money(summary.cashBalance)}</div>
                <div className="metric-detail">Virtual USDT</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">LOCKED MARGIN</div>
                <div className="metric-value">{money(summary.lockedCapital)}</div>
                <div className="metric-detail">Open positions</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">REALIZED NET P&amp;L</div>
                <div className="metric-value">{money(summary.realizedPnl)}</div>
                <div className="metric-detail">After simulated fees</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">TOTAL FEES</div>
                <div className="metric-value">{money(summary.totalFees)}</div>
                <div className="metric-detail">Simulated fees</div>
              </article>
            </section>

            <section className="paper-valuation-note">
              <strong>Unrealized P&amp;L: Unavailable</strong>
              <span>
                Equity: Unavailable â€” no verified mark price is connected.
                Cash balance is not total account equity.
              </span>
            </section>

            <section className="panel paper-trade-form-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Open simulated position</div>
                  <div className="panel-subtitle">
                    Manual entry values Â· No exchange execution
                  </div>
                </div>
                <span className="panel-badge">SIMULATION</span>
              </div>

              <form className="paper-trade-form" onSubmit={submitTrade}>
                <label>
                  Market
                  <select
                    value={market}
                    onChange={(event) => {
                      const nextMarket = event.target.value as TradeMarket;
                      setMarket(nextMarket);
                      if (nextMarket === "SPOT") {
                        setSide("LONG");
                        setLeverage("1");
                      } else if (Number(leverage) < 1) {
                        setLeverage("1");
                      }
                    }}
                  >
                    <option value="SPOT">Spot</option>
                    <option value="FUTURES">Futures Â· Isolated</option>
                  </select>
                </label>

                <label>
                  Side
                  <select
                    value={side}
                    onChange={(event) => setSide(event.target.value as TradeSide)}
                  >
                    <option value="LONG">LONG</option>
                    {market === "FUTURES" && (
                      <option value="SHORT">SHORT</option>
                    )}
                  </select>
                </label>

                <label>
                  Symbol
                  <input
                    value={symbol}
                    onChange={(event) => setSymbol(event.target.value)}
                    maxLength={30}
                    placeholder="BTCUSDT"
                    required
                  />
                </label>

                <label>
                  Manual entry price
                  <input
                    type="number"
                    min="0"
                    step="any"
                    value={entryPrice}
                    onChange={(event) => setEntryPrice(event.target.value)}
                    placeholder="Enter price"
                    required
                  />
                </label>

                <label>
                  Quantity
                  <input
                    type="number"
                    min="0"
                    step="any"
                    value={quantity}
                    onChange={(event) => setQuantity(event.target.value)}
                    placeholder="Enter quantity"
                    required
                  />
                </label>

                <label>
                  Leverage
                  <select
                    value={leverage}
                    disabled={market === "SPOT"}
                    onChange={(event) => setLeverage(event.target.value)}
                  >
                    {Array.from({ length: 20 }, (_, index) => index + 1).map((n) => (
                      <option key={n} value={String(n)}>{n}x</option>
                    ))}
                  </select>
                </label>

                <button type="submit" className="paper-primary-button">
                  Open Paper Position
                </button>
              </form>

              <div className="paper-form-footnote">
                Simulation fee assumption: 0.10% per execution.
                Not a statement of current exchange fees.
              </div>
            </section>

            <section className="panel paper-positions-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Open positions</div>
                  <div className="panel-subtitle">
                    {openPositions.length} active simulated positions
                  </div>
                </div>
              </div>

              {openPositions.length === 0 ? (
                <div className="paper-empty">
                  No open positions. Submit a simulated trade to begin.
                </div>
              ) : (
                <div className="paper-position-list">
                  {openPositions.map((position) => (
                    <article className="paper-position-card" key={position.id}>
                      <div className="paper-position-heading">
                        <strong>{position.symbol}</strong>
                        <span>{position.market} Â· {position.side}</span>
                        <span>{position.market === "FUTURES"
                          ? `ISOLATED Â· ${position.leverage}x`
                          : "1x"}
                        </span>
                      </div>
                      <div className="paper-position-details">
                        <span>Entry: {money(position.entryPrice)}</span>
                        <span>Quantity: {position.quantity}</span>
                        <span>Margin: {money(position.margin)}</span>
                        <span>Entry fee: {money(position.entryFee)}</span>
                      </div>
                      <div className="paper-close-row">
                        <label>
                          Manual exit price
                          <input
                            type="number"
                            min="0"
                            step="any"
                            value={exitPrices[position.id] ?? ""}
                            onChange={(event) => setExitPrices((current) => ({
                              ...current,
                              [position.id]: event.target.value,
                            }))}
                            placeholder="Enter exit price"
                          />
                        </label>
                        <button
                          type="button"
                          className="paper-secondary-button"
                          onClick={() => requestClose(position.id)}
                        >
                          Review Close
                        </button>
                      </div>
                      {pendingClose?.positionId === position.id ? (
                        <div
                          className="paper-close-confirmation"
                          role="alertdialog"
                          aria-labelledby={`paper-close-title-${position.id}`}
                          aria-describedby={`paper-close-description-${position.id}`}
                        >
                          <strong id={`paper-close-title-${position.id}`}>
                            Confirm simulated position close
                          </strong>
                          <p id={`paper-close-description-${position.id}`}>
                            Review these values. No exchange order will be placed.
                          </p>
                          <div className="paper-position-details">
                            <span>Symbol: {position.symbol}</span>
                            <span>Side: {position.side}</span>
                            <span>Exit price: {pendingClose.exitPrice} USDT</span>
                            <span>Gross P&amp;L: {money(pendingClose.grossPnl)} USDT</span>
                            <span>Exit fee: {money(pendingClose.exitFee)} USDT</span>
                            <span>Net P&amp;L: {money(pendingClose.netPnl)} USDT</span>
                          </div>
                          <div className="paper-close-confirmation-actions">
                            <button
                              type="button"
                              className="paper-secondary-button"
                              onClick={() => setPendingClose(null)}
                            >
                              Cancel
                            </button>
                            <button
                              type="button"
                              className="paper-primary-button"
                              onClick={confirmClose}
                            >
                              Confirm Close
                            </button>
                          </div>
                        </div>
                      ) : null}
                    </article>
                  ))}
                </div>
              )}
            </section>

            <section className="panel paper-history-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Closed trade history</div>
                  <div className="panel-subtitle">
                    {closedPositions.length} closed positions
                  </div>
                </div>
              </div>

              {closedPositions.length === 0 ? (
                <div className="paper-empty">No closed trades yet.</div>
              ) : (
                <div className="paper-history-list">
                  {closedPositions.slice().reverse().map((position) => (
                    <article className="paper-history-row" key={position.id}>
                      <div>
                        <strong>{position.symbol}</strong>
                        <span>{position.market} Â· {position.side}</span>
                      </div>
                      <div>
                        <span>Entry {money(position.entryPrice)}</span>
                        <span>Exit {money(position.exitPrice ?? 0)}</span>
                      </div>
                      <div>
                        <strong className={
                          (position.netPnl ?? 0) >= 0 ? "paper-profit" : "paper-loss"
                        }>
                          {money(position.netPnl ?? 0)}
                        </strong>
                        <span>Net P&amp;L Â· USDT</span>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>
          </>
        ) : null}

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Virtual ledger only Â· Manual simulation prices Â· No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}