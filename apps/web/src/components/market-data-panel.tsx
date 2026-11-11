"use client";

import { useEffect, useMemo, useState } from "react";

type MarketType = "SPOT" | "FUTURES";
type Pipeline = {
  group: string;
  state: string;
  symbols: string[];
  clock_synchronized: boolean;
  metadata_fresh: boolean;
  book_synchronized: boolean;
  last_event_timestamp_ms: number | null;
  last_freshness_state: string | null;
  error_code: string | null;
};
type Health = { state: string; ready: boolean; pipelines: Pipeline[] };
type Latest = {
  group: string;
  symbol: string;
  event_type: string;
  event_timestamp_ms: number;
  event_timestamp_utc: string;
  freshness_state: string | null;
  book_synchronized: boolean;
  payload: Record<string, unknown>;
};

type Props = { marketType: MarketType; search: string; onConnectionChange: (connected: boolean) => void };

function positivePrice(value: unknown): number | null {
  if (typeof value !== "string" || !/^(?:\d+)(?:\.\d+)?$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function formatPrice(value: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: value >= 1 ? 8 : 12,
  }).format(value);
}

export default function MarketDataPanel({ marketType, search, onConnectionChange }: Props) {
  const [health, setHealth] = useState<Health | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [latest, setLatest] = useState<Latest | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [latestError, setLatestError] = useState<string | null>(null);
  const [loadingLatest, setLoadingLatest] = useState(false);

  const group = marketType === "SPOT" ? "spot" : "futures_mark_price";
  const pipeline = health?.pipelines.find((item) => item.group === group) ?? null;
  const symbols = useMemo(() => {
    const configured = pipeline?.symbols ?? [];
    const query = search.trim().toUpperCase();
    return query ? configured.filter((symbol) => symbol.includes(query)) : configured;
  }, [pipeline, search]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();

    const loadHealth = async () => {
      try {
        const response = await fetch("/api/market-data/health", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Health endpoint returned ${response.status}.`);
        const data: unknown = await response.json();
        if (!data || typeof data !== "object" || !Array.isArray((data as Health).pipelines)) {
          throw new Error("Health response has an invalid format.");
        }
        if (active) {
          setHealth(data as Health);
          setHealthError(null);
        }
      } catch (error) {
        if (active && !(error instanceof DOMException && error.name === "AbortError")) {
          setHealth(null);
          setHealthError(error instanceof Error ? error.message : "Health data unavailable.");
        }
      }
    };

    void loadHealth();
    const timer = window.setInterval(() => void loadHealth(), 5000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!symbols.includes(selectedSymbol)) {
      setSelectedSymbol(symbols[0] ?? "");
      setLatest(null);
      setLatestError(null);
    }
  }, [symbols, selectedSymbol]);

  useEffect(() => {
    if (!selectedSymbol) {
      setLatest(null);
      setLatestError(null);
      setLoadingLatest(false);
      return;
    }

    let active = true;
    let controller: AbortController | null = null;
    const loadLatest = async () => {
      controller?.abort();
      controller = new AbortController();
      setLoadingLatest(true);
      try {
        const response = await fetch(
          `/api/market-data/latest/${group}/${encodeURIComponent(selectedSymbol)}`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) throw new Error(`Latest event unavailable (${response.status}).`);
        const data: unknown = await response.json();
        if (!data || typeof data !== "object") throw new Error("Latest event has an invalid format.");
        const event = data as Latest;
        const expectedType = group === "spot" ? "trade" : "markPriceUpdate";
        if (
          event.group !== group ||
          event.symbol !== selectedSymbol ||
          event.event_type !== expectedType ||
          !Number.isSafeInteger(event.event_timestamp_ms) ||
          event.event_timestamp_ms <= 0 ||
          !event.payload || typeof event.payload !== "object"
        ) {
          throw new Error("Latest event failed frontend contract validation.");
        }
        const price = positivePrice(event.payload.p);
        if (price === null) throw new Error("Verified event does not contain a valid price.");
        if (active) {
          setLatest(event);
          setLatestError(null);
        }
      } catch (error) {
        if (active && !(error instanceof DOMException && error.name === "AbortError")) {
          setLatest(null);
          setLatestError(error instanceof Error ? error.message : "Latest market data unavailable.");
        }
      } finally {
        if (active) setLoadingLatest(false);
      }
    };

    void loadLatest();
    const timer = window.setInterval(() => void loadLatest(), 2000);
    return () => {
      active = false;
      controller?.abort();
      window.clearInterval(timer);
    };
  }, [group, selectedSymbol]);

  const price = latest ? positivePrice(latest.payload.p) : null;
  const pipelineReady = pipeline?.state === "READY";
  const freshnessReady = group === "spot"
    ? latest?.freshness_state === "FRESH"
    : latest?.freshness_state === "FRESH";
  const displayReady = Boolean(latest && price !== null && pipelineReady && freshnessReady);

  useEffect(() => {
    onConnectionChange(displayReady);
  }, [displayReady, onConnectionChange]);

  return (
    <section className="panel" aria-label="Verified live market data">
      <div className="panel-header">
        <div>
          <div className="panel-title">Verified market data</div>
          <div className="panel-subtitle">
            {pipeline ? `${group} pipeline · ${pipeline.state}` : "Waiting for configured market pipeline"}
          </div>
        </div>
        <span className="panel-badge">{displayReady ? "LIVE · VERIFIED" : "NOT READY"}</span>
      </div>

      {healthError ? <p role="alert">Market health unavailable: {healthError}</p> : null}
      {pipeline ? (
        <p role="status">
          Exchange clock: {pipeline.clock_synchronized ? "Synchronized" : "Not synchronized"}
          {" · "}Symbol metadata: {pipeline.metadata_fresh ? "Fresh" : "Not fresh"}
        </p>
      ) : null}

      <label className="market-search">
        <span>Instrument</span>
        <select
          aria-label="Configured market symbol"
          value={selectedSymbol}
          onChange={(event) => setSelectedSymbol(event.target.value)}
          disabled={symbols.length === 0}
        >
          {symbols.length === 0 ? <option value="">No configured symbols</option> : null}
          {symbols.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}
        </select>
      </label>

      <div className="metric-card" aria-live="polite">
        <div className="metric-heading">{marketType === "SPOT" ? "Last trade price" : "Futures mark price"}</div>
        <div className="metric-value">
          {displayReady && price !== null ? formatPrice(price) : "Unavailable"}
        </div>
        <div className="metric-detail">
          {displayReady && latest
            ? `Exchange event: ${new Date(latest.event_timestamp_utc).toLocaleString()}`
            : latestError ?? (loadingLatest ? "Retrieving validated event…" : "No verified fresh price available")}
        </div>
      </div>
      {pipeline?.state === "READY" && !latest ? (
        <p role="status">Pipeline is ready, but no latest event is currently available.</p>
      ) : null}
      {pipeline?.state === "FAILED" || pipeline?.state === "STALE" || pipeline?.state === "UNSYNCED" ? (
        <p role="alert">Market pipeline is {pipeline.state.toLowerCase()}; price display is blocked.</p>
      ) : null}
    </section>
  );
}
