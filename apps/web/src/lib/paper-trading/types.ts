export type TradeMarket = "SPOT" | "FUTURES";
export type TradeSide = "LONG" | "SHORT";
export type TradeStatus = "OPEN" | "CLOSED";
export type MarginMode = "ISOLATED";
export type DecimalInput = number | string;

export interface PaperPosition {
  id: string;
  symbol: string;
  market: TradeMarket;
  side: TradeSide;
  marginMode: MarginMode;
  entryPrice: number;
  quantity: number;
  accountingVersion?: 2;
  entryPriceExact?: string;
  quantityExact?: string;
  leverage: number;
  margin: number;
  entryFee: number;
  exitPrice: number | null;
  exitPriceExact?: string;
  exitFee: number | null;
  grossPnl: number | null;
  netPnl: number | null;
  status: TradeStatus;
  openedAt: string;
  closedAt: string | null;
}

export interface PaperLedger {
  version: 1;
  startingCapital: number;
  cashBalance: number;
  feeRate: number;
  positions: PaperPosition[];
  updatedAt: string;
}

export interface OpenTradeInput {
  symbol: string;
  market: TradeMarket;
  side: TradeSide;
  entryPrice: DecimalInput;
  quantity: DecimalInput;
  leverage: number;
}

export interface CloseTradeInput {
  positionId: string;
  exitPrice: DecimalInput;
}

export interface LedgerSummary {
  cashBalance: number;
  lockedCapital: number;
  openPositions: number;
  closedPositions: number;
  realizedPnl: number;
  totalFees: number;
  unrealizedPnl: null;
  equity: null;
  valuationStatus: "UNAVAILABLE_WITHOUT_VERIFIED_MARK_PRICE";
}

export const DEFAULT_STARTING_CAPITAL = 10_000;
export const DEFAULT_FEE_RATE = 0.001;
export const MAX_POSITIONS = 200;
export const MAX_LEVERAGE = 20;