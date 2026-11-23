"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import WorkspaceShell from "./workspace-shell";

type MarketType = "spot" | "futures";
type TradeSide = "buy" | "sell";

interface PaperPositionSnapshot {
  id: string;
  symbol: string;
  side: "long" | "short";
  status: "open" | "closed";
  leverage: string | null;
  initial_quantity: string;
  remaining_quantity: string;
  entry_price: string;
  remaining_cost_basis: string | null;
  margin_locked: string | null;
  realized_pnl: string;
  entry_fee_original: string;
  entry_fee_posted: string;
  opened_at: string;
  closed_at: string | null;
}

interface PaperPortfolioSnapshot {
  id: string;
  market_type: MarketType;
  status: "active" | "disabled";
  position_mode: "one_way" | "hedge" | null;
  session: {
    id: string;
    session_number: number;
    status: "active" | "archived";
  };
  ledger: {
    book_id: string;
    cash_available: string;
    spot_position_cost: string;
    futures_margin_locked: string;
    realized_pnl: string;
    fee_expense: string;
    contributed_capital: string;
  };
  positions: PaperPositionSnapshot[];
}

interface PaperTradingBootstrap {
  account: {
    id: string;
    name: string;
    role: "owner" | "admin" | "trader" | "viewer";
  };
  portfolios: PaperPortfolioSnapshot[];
}

function amount(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : value;
}

function timestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

function errorMessage(status: number, detail?: unknown): string {
  if (status === 401) return "Your session has expired. Sign in again to continue.";
  if (status === 403) return "Your account role does not permit this action.";
  if (status === 404) return "Your Paper Trading account or portfolio was not found.";
  if (status === 409) return "The financial records could not be reconciled. Trading is blocked until the account is reviewed.";
  if (status === 422) return "The trade details are invalid. Check the symbol, quantity and selected position.";
  if (status === 503) return "Paper Trading is temporarily unavailable. No trade was confirmed.";
  if (typeof detail === "string" && detail.length > 0 && detail.length <= 300) {
    return detail;
  }
  return "The request could not be completed.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isManualTopUpEligible(value: string): boolean {
  if (!/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value)) {
    return false;
  }

  const balance = Number(value);
  return Number.isFinite(balance) && balance <= 100;
}

export default function PaperTradingWorkspace() {
  const [snapshot, setSnapshot] = useState<PaperTradingBootstrap | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [side, setSide] = useState<TradeSide>("buy");
  const [quantity, setQuantity] = useState("");
  const [positionId, setPositionId] = useState("");
  const [futuresSymbol, setFuturesSymbol] = useState("BTCUSDT");
  const [futuresAction, setFuturesAction] = useState<"open" | "reduce">("open");
  const [futuresPositionSide, setFuturesPositionSide] = useState<"long" | "short">("long");
  const [futuresQuantity, setFuturesQuantity] = useState("");
  const [futuresLeverage, setFuturesLeverage] = useState("1");
  const [futuresPositionId, setFuturesPositionId] = useState("");
  const submitLock = useRef(false);
  const resetIdempotencyKeys = useRef(new Map<string, string>());

  const portfolios = snapshot?.portfolios ?? [];
  const spot = portfolios.find((item) => item.market_type === "spot") ?? null;
  const futures = portfolios.find((item) => item.market_type === "futures") ?? null;
  const openSpotPositions = useMemo(
    () => spot?.positions.filter((item) => item.status === "open") ?? [],
    [spot],
  );
  const openFuturesPositions = useMemo(
    () => futures?.positions.filter((item) => item.status === "open") ?? [],
    [futures],
  );
  const openPositions = portfolios.flatMap((portfolio) =>
    portfolio.positions.filter((position) => position.status === "open").map(
      (position) => ({ ...position, market_type: portfolio.market_type }),
    ),
  );
  const closedPositions = portfolios.flatMap((portfolio) =>
    portfolio.positions.filter((position) => position.status === "closed").map(
      (position) => ({ ...position, market_type: portfolio.market_type }),
    ),
  );

  const loadSnapshot = useCallback(async (showSpinner = false): Promise<PaperTradingBootstrap | null> => {
    if (showSpinner) setRefreshing(true);
    setError("");

    try {
      const response = await fetch("/api/paper-trading/bootstrap", {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });

      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        throw new Error("Paper Trading returned an invalid response.");
      }

      if (!response.ok) {
        const detail = isRecord(payload) ? payload.detail : undefined;
        throw new Error(errorMessage(response.status, detail));
      }

      if (
        !isRecord(payload) ||
        !isRecord(payload.account) ||
        !Array.isArray(payload.portfolios) ||
        payload.portfolios.length !== 2
      ) {
        throw new Error("Paper Trading returned an incomplete account snapshot.");
      }

      const next = payload as unknown as PaperTradingBootstrap;
      if (
        !next.portfolios.some((item) => item.market_type === "spot") ||
        !next.portfolios.some((item) => item.market_type === "futures")
      ) {
        throw new Error("Spot and Futures portfolio records are incomplete.");
      }

      setSnapshot(next);
      setNotice("");
      const nextOpenSpotPositions =
        next.portfolios.find((item) => item.market_type === "spot")
          ?.positions.filter((item) => item.status === "open") ?? [];
      setPositionId((current) =>
        current && nextOpenSpotPositions.some((item) => item.id === current)
          ? current
          : nextOpenSpotPositions[0]?.id ?? "",
      );
      return next;
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Unable to load the authoritative Paper Trading snapshot.",
      );
      return null;
    } finally {
      setLoading(false);
      if (showSpinner) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
  }, [loadSnapshot]);

  async function submitSpotTrade(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (submitLock.current || !snapshot || !spot) return;

    const normalizedSymbol = symbol.trim().toUpperCase();
    const normalizedQuantity = quantity.trim();
    const decimalPattern = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,36})?$/;

    if (!/^[A-Z0-9_-]{1,64}$/.test(normalizedSymbol)) {
      setError("Enter a valid uppercase trading symbol.");
      return;
    }
    if (
      normalizedQuantity.length > 64 ||
      !decimalPattern.test(normalizedQuantity) ||
      /^0(?:\.0+)?$/.test(normalizedQuantity)
    ) {
      setError("Enter a positive quantity using plain decimal notation.");
      return;
    }
    if (side === "sell" && !openSpotPositions.some((item) => item.id === positionId)) {
      setError("Select an open Spot position to sell.");
      return;
    }

    submitLock.current = true;
    setSubmitting(true);
    setError("");
    setNotice("");

    try {
      const response = await fetch(
        `/api/accounts/${snapshot.account.id}/portfolios/${spot.id}/paper-trades/spot`,
        {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID().toLowerCase(),
          },
          body: JSON.stringify({
            symbol: normalizedSymbol,
            side,
            quantity: normalizedQuantity,
            ...(side === "sell" ? { position_id: positionId } : {}),
          }),
        },
      );

      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        throw new Error("Paper Trading returned an invalid trade response.");
      }

      if (!response.ok) {
        const detail = isRecord(payload) ? payload.detail : undefined;
        throw new Error(errorMessage(response.status, detail));
      }

      setQuantity("");
      const refreshed = await loadSnapshot(true);
      if (refreshed) {
        setNotice("Trade accepted by the backend. The authoritative account snapshot has been refreshed.");
      } else {
        setNotice("The backend accepted the trade, but the account snapshot could not be refreshed. Do not resubmit the trade; use Refresh to reload the account.");
      }
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "The Spot trade could not be completed.",
      );
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  async function submitFuturesTrade(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (submitLock.current || !snapshot || !futures) return;

    const normalizedSymbol = futuresSymbol.trim().toUpperCase();
    const normalizedQuantity = futuresQuantity.trim();
    const normalizedLeverage = futuresLeverage.trim();
    const decimalPattern = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,36})?$/;

    if (!/^[A-Z0-9_-]{1,64}$/.test(normalizedSymbol)) {
      setError("Enter a valid uppercase Futures trading symbol.");
      return;
    }

    if (
      normalizedQuantity.length > 64 ||
      !decimalPattern.test(normalizedQuantity) ||
      /^0(?:\.0+)?$/.test(normalizedQuantity)
    ) {
      setError("Enter a positive Futures quantity using plain decimal notation.");
      return;
    }

    if (
      normalizedLeverage.length > 64 ||
      !decimalPattern.test(normalizedLeverage) ||
      /^0(?:\.0+)?$/.test(normalizedLeverage)
    ) {
      setError("Enter a positive leverage using plain decimal notation.");
      return;
    }

    const selectedPosition =
      futuresAction === "reduce"
        ? openFuturesPositions.find((item) => item.id === futuresPositionId)
        : null;

    if (futuresAction === "reduce" && !selectedPosition) {
      setError("Select an open Futures position to reduce or close.");
      return;
    }

    if (selectedPosition && selectedPosition.leverage !== normalizedLeverage) {
      setError("The closing leverage must match the selected position.");
      return;
    }

    const positionSide = selectedPosition?.side ?? futuresPositionSide;
    const orderSide =
      futuresAction === "reduce"
        ? positionSide === "long" ? "sell" : "buy"
        : positionSide === "long" ? "buy" : "sell";

    submitLock.current = true;
    setSubmitting(true);
    setError("");
    setNotice("");

    try {
      const response = await fetch(
        `/api/accounts/${snapshot.account.id}/portfolios/${futures.id}/paper-trades/futures`,
        {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID().toLowerCase(),
          },
          body: JSON.stringify({
            symbol: normalizedSymbol,
            side: orderSide,
            position_side: positionSide,
            quantity: normalizedQuantity,
            leverage: normalizedLeverage,
            order_type: "market",
            position_id: selectedPosition?.id ?? null,
            reduce_only: futuresAction === "reduce",
            limit_price: null,
          }),
        },
      );

      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        throw new Error("Paper Trading returned an invalid Futures response.");
      }

      if (!response.ok) {
        const detail = isRecord(payload) ? payload.detail : undefined;
        throw new Error(errorMessage(response.status, detail));
      }

      setFuturesQuantity("");
      const refreshed = await loadSnapshot(true);

      if (refreshed) {
        setNotice("Futures trade accepted by the backend. The authoritative account snapshot has been refreshed.");
      } else {
        setNotice("The backend accepted the Futures trade, but the account snapshot could not be refreshed. Do not resubmit; use Refresh to reload the account.");
      }
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "The Futures trade could not be completed.",
      );
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  async function submitPortfolioReset(portfolio: PaperPortfolioSnapshot) {
    if (
      submitLock.current ||
      !snapshot ||
      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
      portfolio.status !== "active" ||
      portfolio.session.status !== "active"
    ) {
      return;
    }

    const marketLabel = portfolio.market_type === "spot" ? "Spot" : "Futures";
    const previousSessionId = portfolio.session.id;
    const previousSessionNumber = portfolio.session.session_number;
    const previousBookId = portfolio.ledger.book_id;

    const confirmed = window.confirm(
      `Reset ${marketLabel} portfolio?\n\n` +
      `Current session: ${previousSessionNumber}\n` +
      `Current session ID: ${previousSessionId}\n\n` +
      "This archives the current session and creates a new paper-trading session. " +
      "The operation cannot be undone. Continue?",
    );

    if (!confirmed) return;

    const idempotencyKey =
      resetIdempotencyKeys.current.get(portfolio.id) ??
      crypto.randomUUID().toLowerCase();

    resetIdempotencyKeys.current.set(portfolio.id, idempotencyKey);

    submitLock.current = true;
    setSubmitting(true);
    setError("");
    setNotice("");

    try {
      const response = await fetch(
        `/api/accounts/${snapshot.account.id}/portfolios/${portfolio.id}/reset`,
        {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({ confirmed: true }),
        },
      );

      const payload: unknown = await response.json().catch(() => null);

      if (!response.ok) {
        if (response.status === 409 || response.status === 503) {
          await loadSnapshot(true);
          setNotice(
            `${marketLabel} Reset outcome requires verification. The same idempotency key has been retained. ` +
            "Refresh and verify the session before attempting another operation.",
          );
          return;
        }

        resetIdempotencyKeys.current.delete(portfolio.id);

        setError(
          errorMessage(
            response.status,
            isRecord(payload) ? payload.detail : undefined,
          ),
        );
        return;
      }

      const uuidPattern =
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

      if (
        !isRecord(payload) ||
        payload.operation_type !== "reset" ||
        payload.status !== "completed" ||
        typeof payload.account_id !== "string" ||
        payload.account_id.toLowerCase() !== snapshot.account.id.toLowerCase() ||
        typeof payload.portfolio_id !== "string" ||
        payload.portfolio_id.toLowerCase() !== portfolio.id.toLowerCase() ||
        payload.market_type !== portfolio.market_type ||
        typeof payload.previous_session_id !== "string" ||
        payload.previous_session_id.toLowerCase() !== previousSessionId.toLowerCase() ||
        payload.previous_session_number !== previousSessionNumber ||
        typeof payload.previous_book_id !== "string" ||
        payload.previous_book_id.toLowerCase() !== previousBookId.toLowerCase() ||
        typeof payload.previous_session_archived_at !== "string" ||
        payload.previous_session_archived_at.length === 0 ||
        !Number.isFinite(Date.parse(payload.previous_session_archived_at)) ||
        typeof payload.new_session_id !== "string" ||
        !uuidPattern.test(payload.new_session_id) ||
        payload.new_session_id.toLowerCase() === previousSessionId.toLowerCase() ||
        !Number.isInteger(payload.new_session_number) ||
        (payload.new_session_number as number) <= previousSessionNumber ||
        typeof payload.new_book_id !== "string" ||
        !uuidPattern.test(payload.new_book_id) ||
        payload.new_book_id.toLowerCase() === previousBookId.toLowerCase() ||
        typeof payload.initial_balance !== "string" ||
        !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,36})?$/.test(payload.initial_balance) ||
        typeof payload.replayed !== "boolean"
      ) {
        await loadSnapshot(true);

        setNotice(
          `${marketLabel} Reset may have been accepted, but its response could not be validated. ` +
          "The same idempotency key has been retained. Verify the authoritative session before retrying.",
        );
        return;
      }

      const refreshed = await loadSnapshot(true);

      const refreshedPortfolio =
        refreshed?.portfolios.find((item) => item.id === portfolio.id) ?? null;

      const verified =
        refreshedPortfolio !== null &&
        refreshedPortfolio.status === "active" &&
        refreshedPortfolio.session.status === "active" &&
        refreshedPortfolio.session.id.toLowerCase() ===
          payload.new_session_id.toLowerCase() &&
        refreshedPortfolio.session.session_number === payload.new_session_number &&
        refreshedPortfolio.ledger.book_id.toLowerCase() ===
          payload.new_book_id.toLowerCase();

      if (!verified) {
        setNotice(
          `${marketLabel} Reset was accepted, but the refreshed authoritative snapshot ` +
          "does not yet confirm the new session and ledger. The same idempotency key has been retained. Do not create a new operation.",
        );
        return;
      }

      resetIdempotencyKeys.current.delete(portfolio.id);

      setNotice(
        `${marketLabel} portfolio reset verified. Session ${payload.previous_session_number} ` +
        `was archived and new session ${payload.new_session_number} is active. ` +
        `Initial balance: ${payload.initial_balance} USDT.` +
        `${payload.replayed ? " Existing reset operation replay confirmed." : ""}`,
      );
    } catch {
      await loadSnapshot(true);

      setNotice(
        `${marketLabel} Reset outcome could not be confirmed. The same idempotency key ` +
        "has been retained. Refresh and verify the authoritative session before retrying.",
      );
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }
  async function submitManualTopUp(portfolio: PaperPortfolioSnapshot) {
    if (
      submitLock.current ||
      !snapshot ||
      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
      portfolio.status !== "active" ||
      !isManualTopUpEligible(portfolio.ledger.cash_available)
    ) {
      return;
    }

    const previousBalance = portfolio.ledger.cash_available;
    const marketLabel = portfolio.market_type === "spot" ? "Spot" : "Futures";

    const confirmed = window.confirm(
      `Confirm ${marketLabel} Manual Top-up?\n\n` +
      `Current cash balance: ${previousBalance} USDT\n` +
      `Target cash balance: 1000.00 USDT\n\n` +
      "This creates an immutable ledger transaction. Continue?",
    );

    if (!confirmed) return;

    submitLock.current = true;
    setSubmitting(true);
    setError("");
    setNotice("");

    try {
      const response = await fetch(
        `/api/accounts/${snapshot.account.id}/portfolios/${portfolio.id}/manual-top-up`,
        {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID().toLowerCase(),
          },
          body: JSON.stringify({ confirmed: true }),
        },
      );

      const payload: unknown = await response.json().catch(() => null);

      if (!response.ok) {
        setError(
          errorMessage(
            response.status,
            isRecord(payload) ? payload.detail : undefined,
          ),
        );
        return;
      }

      if (
        !isRecord(payload) ||
        typeof payload.transaction_id !== "string" ||
        typeof payload.book_id !== "string" ||
        payload.market_type !== portfolio.market_type ||
        payload.transaction_type !== "manual_top_up" ||
        payload.status !== "posted" ||
        payload.entry_count !== 2 ||
        typeof payload.amount !== "string" ||
        typeof payload.previous_balance !== "string" ||
        typeof payload.new_balance !== "string" ||
        typeof payload.debit_total !== "string" ||
        typeof payload.credit_total !== "string" ||
        typeof payload.replayed !== "boolean"
      ) {
        const refreshed = await loadSnapshot(true);

        setNotice(
          refreshed
            ? "The backend returned success, but its Manual Top-up response could not be validated. The authoritative balance was refreshed. Do not resubmit without checking the ledger."
            : "The backend returned success, but the response and account refresh could not be verified. Do not resubmit. Refresh the account and verify the ledger first.",
        );
        return;
      }

      const refreshed = await loadSnapshot(true);

      setNotice(
        refreshed
          ? `${marketLabel} Manual Top-up posted: ${payload.amount} USDT credited. Balance: ${payload.previous_balance} → ${payload.new_balance} USDT.${payload.replayed ? " Existing transaction replay confirmed." : ""}`
          : `${marketLabel} Manual Top-up was accepted by the backend, but the account snapshot could not be refreshed. Do not resubmit; refresh the account and verify the ledger first.`,
      );
    } catch {
      setError(
        "The Manual Top-up outcome could not be confirmed. Refresh the authoritative account balance and verify the ledger before attempting another request.",
      );
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  return (
    <WorkspaceShell activeSection="paper-trading">
      <div className="dashboard-content paper-workspace">
        <section className="section-intro">
          <div className="eyebrow">AUTHORITATIVE PAPER LEDGER</div>
          <h1>Paper Trading</h1>
          <p className="welcome-description">
            Spot and Futures balances, sessions and positions are loaded from the authenticated backend ledger.
          </p>
        </section>

        <div className="paper-safety-banner" role="status">
          <strong>PAPER ENVIRONMENT ONLY</strong>
          <span>
            Virtual portfolios only. Spot market execution uses the backend market-data price.
            No exchange orders or real funds are involved.
          </span>
        </div>

        {error && (
          <div className="paper-error" role="alert">
            <strong>Action could not be completed</strong>
            <span>{error}</span>
          </div>
        )}
        {notice && (
          <div className="paper-valuation-note" role="status">
            <strong>Trade status</strong>
            <span>{notice}</span>
          </div>
        )}

        {loading ? (
          <section className="panel paper-loading" role="status">
            Loading authoritative Paper Trading account…
          </section>
        ) : !snapshot ? (
          <section className="panel paper-account-gate">
            <div className="eyebrow">ACCOUNT UNAVAILABLE</div>
            <h2>Paper Trading account could not be loaded</h2>
            <p>
              No browser-side replacement account will be created. Existing local browser history is left untouched and is not treated as financial data.
            </p>
            <button
              type="button"
              className="paper-primary-button"
              onClick={() => void loadSnapshot(true)}
              disabled={refreshing}
            >
              {refreshing ? "Retrying…" : "Retry account load"}
            </button>
          </section>
        ) : (
          <>
            <section className="panel paper-summary-grid">
              <article className="metric-card">
                <div className="metric-heading">ACCOUNT</div>
                <div className="metric-value">{snapshot.account.name}</div>
                <div className="metric-detail">Role: {snapshot.account.role}</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">SPOT AVAILABLE CASH</div>
                <div className="metric-value">{amount(spot?.ledger.cash_available)}</div>
                <div className="metric-detail">USDT · Backend ledger snapshot</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">FUTURES AVAILABLE CASH</div>
                <div className="metric-value">{amount(futures?.ledger.cash_available)}</div>
                <div className="metric-detail">USDT · Independent portfolio</div>
              </article>
              <article className="metric-card">
                <div className="metric-heading">OPEN POSITIONS</div>
                <div className="metric-value">{openPositions.length}</div>
                <div className="metric-detail">Spot and Futures combined</div>
              </article>
            </section>

            <section className="panel paper-positions-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Portfolio snapshots</div>
                  <div className="panel-subtitle">Validated journal and account snapshots</div>
                </div>
                <button
                  type="button"
                  className="paper-secondary-button"
                  onClick={() => void loadSnapshot(true)}
                  disabled={refreshing || submitting}
                >
                  {refreshing ? "Refreshing…" : "Refresh"}
                </button>
              </div>
              <div className="paper-summary-grid">
                {([spot, futures].filter(Boolean) as PaperPortfolioSnapshot[]).map((portfolio) => (
                  <article className="metric-card" key={portfolio.id}>
                    <div className="metric-heading">
                      {portfolio.market_type.toUpperCase()} · SESSION {portfolio.session.session_number}
                    </div>
                    <div className="metric-value">{amount(portfolio.ledger.cash_available)} USDT</div>
                    <div className="metric-detail">
                      Position mode: {portfolio.position_mode ?? "Spot"}
                    </div>
                    <div className="paper-position-details">
                      <span>Position cost: {amount(portfolio.ledger.spot_position_cost)}</span>
                      <span>Locked margin: {amount(portfolio.ledger.futures_margin_locked)}</span>
                      <span>Realized P&amp;L: {amount(portfolio.ledger.realized_pnl)}</span>
                      <span>Fees: {amount(portfolio.ledger.fee_expense)}</span>
                      <span>Contributed capital: {amount(portfolio.ledger.contributed_capital)}</span>
                      <span>Session: {portfolio.session.status}</span>
                    </div>
                  </article>
                ))}
              </div>
            </section>

            <section className="panel paper-trade-form-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Spot market order</div>
                  <div className="panel-subtitle">
                    Server-priced execution · Decimal quantity · Idempotent submission
                  </div>
                </div>
                <span className="panel-badge">BACKEND EXECUTION</span>
              </div>

              <form className="paper-trade-form" onSubmit={submitSpotTrade}>
                <label>
                  Side
                  <select
                    value={side}
                    onChange={(event) => setSide(event.target.value as TradeSide)}
                    disabled={submitting}
                  >
                    <option value="buy">Buy · Open position</option>
                    <option value="sell">Sell · Reduce position</option>
                  </select>
                </label>
                <label>
                  Symbol
                  <input
                    value={symbol}
                    onChange={(event) => setSymbol(event.target.value.toUpperCase())}
                    maxLength={64}
                    placeholder="BTCUSDT"
                    required
                    disabled={submitting}
                  />
                </label>
                <label>
                  Quantity
                  <input
                    type="text"
                    inputMode="decimal"
                    value={quantity}
                    onChange={(event) => setQuantity(event.target.value)}
                    maxLength={64}
                    placeholder="0.001"
                    required
                    disabled={submitting}
                  />
                </label>
                {side === "sell" && (
                  <label>
                    Open Spot position
                    <select
                      value={positionId}
                      onChange={(event) => setPositionId(event.target.value)}
                      required
                      disabled={submitting || openSpotPositions.length === 0}
                    >
                      <option value="">Select position</option>
                      {openSpotPositions.map((position) => (
                        <option key={position.id} value={position.id}>
                          {position.symbol} · Remaining {position.remaining_quantity}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <button
                  type="submit"
                  className="paper-primary-button"
                  disabled={
                    submitting ||
                    !spot ||
                    spot.status !== "active" ||
                    (side === "sell" && openSpotPositions.length === 0)
                  }
                >
                  {submitting ? "Submitting…" : side === "buy" ? "Place Spot Buy" : "Place Spot Sell"}
                </button>
              </form>
              <div className="paper-form-footnote">
                Execution price, fees and accounting are determined by the backend. Client-supplied prices are not accepted.
              </div>
            </section>

            <section className="panel paper-trade-form-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Futures market order</div>
                  <div className="panel-subtitle">
                    Backend-priced execution ? Position-aware ? Idempotent submission
                  </div>
                </div>
                <span className="panel-badge">BACKEND EXECUTION</span>
              </div>

              <form className="paper-trade-form" onSubmit={submitFuturesTrade}>
                <label>
                  Action
                  <select
                    value={futuresAction}
                    onChange={(event) => {
                      const nextAction = event.target.value as "open" | "reduce";
                      setFuturesAction(nextAction);
                      if (nextAction === "open") setFuturesPositionId("");
                    }}
                    disabled={submitting}
                  >
                    <option value="open">Open / Increase</option>
                    <option value="reduce">Reduce / Close</option>
                  </select>
                </label>

                {futuresAction === "open" ? (
                  <label>
                    Position direction
                    <select
                      value={futuresPositionSide}
                      onChange={(event) =>
                        setFuturesPositionSide(event.target.value as "long" | "short")
                      }
                      disabled={submitting}
                    >
                      <option value="long">Long</option>
                      <option value="short">Short</option>
                    </select>
                  </label>
                ) : (
                  <label>
                    Open Futures position
                    <select
                      value={futuresPositionId}
                      onChange={(event) => {
                        const selected = openFuturesPositions.find(
                          (item) => item.id === event.target.value,
                        );
                        setFuturesPositionId(event.target.value);
                        if (selected) {
                          setFuturesPositionSide(selected.side);
                          setFuturesSymbol(selected.symbol);
                          setFuturesLeverage(selected.leverage ?? "");
                        }
                      }}
                      required
                      disabled={submitting || openFuturesPositions.length === 0}
                    >
                      <option value="">Select position</option>
                      {openFuturesPositions.map((position) => (
                        <option key={position.id} value={position.id}>
                          {position.symbol} ? {position.side.toUpperCase()} ? Remaining {position.remaining_quantity}
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                <label>
                  Symbol
                  <input
                    value={futuresSymbol}
                    onChange={(event) => setFuturesSymbol(event.target.value.toUpperCase())}
                    maxLength={64}
                    placeholder="BTCUSDT"
                    required
                    disabled={submitting || futuresAction === "reduce"}
                  />
                </label>

                <label>
                  Quantity
                  <input
                    type="text"
                    inputMode="decimal"
                    value={futuresQuantity}
                    onChange={(event) => setFuturesQuantity(event.target.value)}
                    maxLength={64}
                    placeholder="0.001"
                    required
                    disabled={submitting}
                  />
                </label>

                <label>
                  Leverage
                  <input
                    type="text"
                    inputMode="decimal"
                    value={futuresLeverage}
                    onChange={(event) => setFuturesLeverage(event.target.value)}
                    maxLength={64}
                    placeholder="1"
                    required
                    disabled={submitting || futuresAction === "reduce"}
                  />
                </label>

                <button
                  type="submit"
                  className="paper-primary-button"
                  disabled={
                    submitting ||
                    !futures ||
                    futures.status !== "active" ||
                    (futuresAction === "reduce" && openFuturesPositions.length === 0)
                  }
                >
                  {submitting
                    ? "Submitting?"
                    : futuresAction === "open"
                      ? `Open Futures ${futuresPositionSide.toUpperCase()}`
                      : "Reduce / Close Futures Position"}
                </button>
              </form>

              <div className="paper-form-footnote">
                Market execution price, fees, margin and accounting are determined by the backend. Client-supplied prices are not accepted.
              </div>
            </section>

            <section className="panel paper-positions-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Open positions</div>
                  <div className="panel-subtitle">{openPositions.length} backend positions</div>
                </div>
              </div>
              {openPositions.length === 0 ? (
                <div className="paper-empty">No open positions.</div>
              ) : (
                <div className="paper-position-list">
                  {openPositions.map((position) => (
                    <article className="paper-position-card" key={position.id}>
                      <div className="paper-position-heading">
                        <strong>{position.symbol}</strong>
                        <span>{position.market_type.toUpperCase()} · {position.side.toUpperCase()}</span>
                      </div>
                      <div className="paper-position-details">
                        <span>Entry: {amount(position.entry_price)} USDT</span>
                        <span>Initial quantity: {amount(position.initial_quantity)}</span>
                        <span>Remaining quantity: {amount(position.remaining_quantity)}</span>
                        <span>Cost basis: {amount(position.remaining_cost_basis)}</span>
                        <span>Locked margin: {amount(position.margin_locked)}</span>
                        <span>Realized P&amp;L: {amount(position.realized_pnl)} USDT</span>
                        <span>Entry fee: {amount(position.entry_fee_posted)} USDT</span>
                        <span>Opened: {timestamp(position.opened_at)}</span>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>

            <section className="panel paper-history-panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Closed position history</div>
                  <div className="panel-subtitle">{closedPositions.length} backend records</div>
                </div>
              </div>
              {closedPositions.length === 0 ? (
                <div className="paper-empty">No closed positions yet.</div>
              ) : (
                <div className="paper-history-list">
                  {closedPositions.slice().reverse().map((position) => (
                    <article className="paper-history-row" key={position.id}>
                      <div>
                        <strong>{position.symbol}</strong>
                        <span>{position.market_type.toUpperCase()} · {position.side.toUpperCase()}</span>
                      </div>
                      <div>
                        <span>Entry {amount(position.entry_price)}</span>
                        <span>Quantity {amount(position.initial_quantity)}</span>
                        <span>Closed {timestamp(position.closed_at)}</span>
                      </div>
                      <div>
                        <strong>{amount(position.realized_pnl)}</strong>
                        <span>Realized P&amp;L · USDT</span>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>

            <section className="panel paper-account-gate">
              <div className="panel-title">Portfolio administration</div>
              <p>
                Manual Top-up is available to Owner and Admin roles when the selected active portfolio cash balance is 100 USDT or lower. Each portfolio is processed independently.
              </p>

              {!snapshot || (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ? (
                <p>Manual Top-up requires the Owner or Admin account role.</p>
              ) : null}

              <div className="paper-close-confirmation-actions">
                                {spot ? (
                  <button
                    type="button"
                    className="paper-secondary-button"
                    onClick={() => void submitPortfolioReset(spot)}
                    disabled={
                      !snapshot ||
                      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
                      spot.status !== "active" ||
                      spot.session.status !== "active" ||
                      submitting ||
                      refreshing
                    }
                  >
                    {submitting
                      ? "Resetting Spot..."
                      : `Reset Spot - Session ${spot.session.session_number}`}
                  </button>
                ) : null}

                {futures ? (
                  <button
                    type="button"
                    className="paper-secondary-button"
                    onClick={() => void submitPortfolioReset(futures)}
                    disabled={
                      !snapshot ||
                      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
                      futures.status !== "active" ||
                      futures.session.status !== "active" ||
                      submitting ||
                      refreshing
                    }
                  >
                    {submitting
                      ? "Resetting Futures..."
                      : `Reset Futures - Session ${futures.session.session_number}`}
                  </button>
                ) : null}







                {spot ? (
                  <button
                    type="button"
                    className="paper-secondary-button"
                    onClick={() => void submitManualTopUp(spot)}
                    disabled={
                      submitting ||
                      refreshing ||
                      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
                      spot.status !== "active" ||
                      !isManualTopUpEligible(spot.ledger.cash_available)
                    }
                  >
                    {submitting
                      ? "Processing…"
                      : `Manual Top-up Spot · ${spot.ledger.cash_available} USDT`}
                  </button>
                ) : null}

                {futures ? (
                  <button
                    type="button"
                    className="paper-secondary-button"
                    onClick={() => void submitManualTopUp(futures)}
                    disabled={
                      submitting ||
                      refreshing ||
                      (snapshot.account.role !== "owner" && snapshot.account.role !== "admin") ||
                      futures.status !== "active" ||
                      !isManualTopUpEligible(futures.ledger.cash_available)
                    }
                  >
                    {submitting
                      ? "Processing…"
                      : `Manual Top-up Futures · ${futures.ledger.cash_available} USDT`}
                  </button>
                ) : null}
              </div>

              <p>
                Portfolio Reset archives the current session and creates a new session with the configured initial balance. Manual Top-up remains a separate immutable-ledger operation.
              </p>
            </section>
          </>
        )}

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>Backend ledger authoritative · Browser storage is not a financial source of truth</span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}
