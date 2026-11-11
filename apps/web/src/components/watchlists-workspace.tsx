"use client";

import { useEffect, useMemo, useState } from "react";
import WorkspaceShell from "./workspace-shell";

type MarketType = "SPOT" | "FUTURES";

type SavedWatchlists = {
  spot: string[];
  futures: string[];
};

const STORAGE_KEY = "zak-tv-watchlists-v1";

const emptyLists: SavedWatchlists = {
  spot: [],
  futures: [],
};

function normalizeSymbol(value: string): string {
  return value.trim().toUpperCase().replace(/\s+/g, "");
}

function isValidSymbolFormat(value: string): boolean {
  return /^[A-Z0-9]{2,30}$/.test(value);
}

function readSavedLists(): SavedWatchlists {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);

    if (!raw) return emptyLists;

    const parsed: unknown = JSON.parse(raw);

    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("spot" in parsed) ||
      !("futures" in parsed) ||
      !Array.isArray(parsed.spot) ||
      !Array.isArray(parsed.futures)
    ) {
      return emptyLists;
    }

    const clean = (items: unknown[]): string[] =>
      Array.from(
        new Set(
          items
            .filter((item): item is string => typeof item === "string")
            .map(normalizeSymbol)
            .filter(isValidSymbolFormat)
        )
      ).slice(0, 200);

    return {
      spot: clean(parsed.spot),
      futures: clean(parsed.futures),
    };
  } catch {
    return emptyLists;
  }
}

export default function WatchlistsWorkspace() {
  const [marketType, setMarketType] = useState<MarketType>("SPOT");
  const [lists, setLists] = useState<SavedWatchlists>(emptyLists);
  const [search, setSearch] = useState("");
  const [symbolInput, setSymbolInput] = useState("");
  const [error, setError] = useState("");
  const [storageReady, setStorageReady] = useState(false);
  const [storageAvailable, setStorageAvailable] = useState(true);

  useEffect(() => {
    try {
      setLists(readSavedLists());
    } catch {
      setStorageAvailable(false);
    }

    setStorageReady(true);
  }, []);

  useEffect(() => {
    if (!storageReady) return;

    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(lists));
      setStorageAvailable(true);
    } catch {
      setStorageAvailable(false);
    }
  }, [lists, storageReady]);

  const activeKey = marketType === "SPOT" ? "spot" : "futures";
  const activeSymbols = lists[activeKey];

  const filteredSymbols = useMemo(() => {
    const query = normalizeSymbol(search);

    if (!query) return activeSymbols;

    return activeSymbols.filter((symbol) => symbol.includes(query));
  }, [activeSymbols, search]);

  function addSymbol(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const symbol = normalizeSymbol(symbolInput);

    if (!symbol) {
      setError("Enter a symbol first.");
      return;
    }

    if (!isValidSymbolFormat(symbol)) {
      setError("Use 2–30 letters or numbers without spaces or special characters.");
      return;
    }

    if (activeSymbols.includes(symbol)) {
      setError(`${symbol} is already in this watchlist.`);
      return;
    }

    if (activeSymbols.length >= 200) {
      setError("This watchlist has reached its 200-symbol limit.");
      return;
    }

    setLists((current) => ({
      ...current,
      [activeKey]: [...current[activeKey], symbol],
    }));

    setSymbolInput("");
    setSearch("");
    setError("");
  }

  function removeSymbol(symbol: string) {
    setLists((current) => ({
      ...current,
      [activeKey]: current[activeKey].filter((item) => item !== symbol),
    }));

    setError("");
  }

  function clearActiveWatchlist() {
    setLists((current) => ({
      ...current,
      [activeKey]: [],
    }));

    setError("");
    setSearch("");
  }

  return (
    <WorkspaceShell activeSection="watchlists">
      <div className="dashboard-content watchlists-workspace">
        <section className="section-intro">
          <div className="eyebrow">PERSONAL MARKET WORKSPACE</div>
          <h1>Watchlists</h1>
          <p className="welcome-description">
            Organize the instruments you want to monitor across Spot and Futures.
          </p>
        </section>

        <section className="watchlist-summary-grid">
          <article className="metric-card">
            <div className="metric-heading">SPOT WATCHLIST</div>
            <div className="metric-value">{lists.spot.length}</div>
            <div className="metric-detail">Saved symbols</div>
          </article>

          <article className="metric-card">
            <div className="metric-heading">FUTURES WATCHLIST</div>
            <div className="metric-value">{lists.futures.length}</div>
            <div className="metric-detail">Saved symbols</div>
          </article>
        </section>

        <section className="watchlist-controls panel">
          <div className="watchlist-tabs" aria-label="Watchlist market type">
            <button
              type="button"
              className={marketType === "SPOT" ? "selected" : ""}
              aria-pressed={marketType === "SPOT"}
              onClick={() => {
                setMarketType("SPOT");
                setSearch("");
                setError("");
              }}
            >
              Spot ({lists.spot.length})
            </button>

            <button
              type="button"
              className={marketType === "FUTURES" ? "selected" : ""}
              aria-pressed={marketType === "FUTURES"}
              onClick={() => {
                setMarketType("FUTURES");
                setSearch("");
                setError("");
              }}
            >
              Futures ({lists.futures.length})
            </button>
          </div>

          <form className="watchlist-add-form" onSubmit={addSymbol}>
            <label className="watchlist-input-wrap">
              <span aria-hidden="true">+</span>
              <input
                value={symbolInput}
                onChange={(event) => {
                  setSymbolInput(event.target.value);
                  setError("");
                }}
                placeholder="Enter symbol, e.g. BTCUSDT"
                aria-label="Symbol to add"
                maxLength={30}
              />
            </label>

            <button type="submit" className="watchlist-add-button">
              Add symbol
            </button>
          </form>

          {error && (
            <p className="watchlist-error" role="alert">
              {error}
            </p>
          )}
        </section>

        <section className="watchlist-table-panel panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">
                {marketType === "SPOT" ? "Spot instruments" : "Futures instruments"}
              </div>
              <div className="panel-subtitle">
                {activeSymbols.length} saved · Maximum 200
              </div>
            </div>

            <span className="panel-badge">USER SAVED</span>
          </div>

          <div className="watchlist-table-toolbar">
            <label className="market-search">
              <span aria-hidden="true">⌕</span>
              <input
                type="search"
                placeholder="Filter saved symbols"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                aria-label="Filter saved watchlist"
              />
            </label>

            <button
              type="button"
              className="watchlist-clear-button"
              onClick={clearActiveWatchlist}
              disabled={activeSymbols.length === 0}
            >
              Clear list
            </button>
          </div>

          {!storageReady ? (
            <div className="watchlist-empty" role="status">
              <strong>Loading saved watchlists…</strong>
            </div>
          ) : activeSymbols.length === 0 ? (
            <div className="watchlist-empty">
              <span className="empty-star">☆</span>
              <strong>Your {marketType.toLowerCase()} watchlist is empty</strong>
              <p>Add a symbol above to start organizing your market workspace.</p>
            </div>
          ) : filteredSymbols.length === 0 ? (
            <div className="watchlist-empty">
              <strong>No matching saved symbols</strong>
              <p>Try a different search term.</p>
            </div>
          ) : (
            <div className="watchlist-table-wrap">
              <table className="watchlist-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Last price</th>
                    <th>24h change</th>
                    <th>Data status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredSymbols.map((symbol) => (
                    <tr key={symbol}>
                      <td>
                        <strong>{symbol}</strong>
                        <small>{marketType} · User saved</small>
                      </td>
                      <td className="watchlist-unavailable">Unavailable</td>
                      <td className="watchlist-unavailable">Unavailable</td>
                      <td>
                        <span className="watchlist-data-status">
                          Not verified
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="watchlist-remove-button"
                          onClick={() => removeSymbol(symbol)}
                          aria-label={`Remove ${symbol}`}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="watchlist-storage-note">
            {storageAvailable
              ? "Saved in this browser. Exchange validity has not been verified."
              : "Browser storage is unavailable. Changes may not persist after refresh."}
          </div>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            User-managed lists only · Market data not connected · No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}