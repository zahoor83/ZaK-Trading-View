import {
  DEFAULT_FEE_RATE,
  DEFAULT_STARTING_CAPITAL,
  MAX_LEVERAGE,
  MAX_POSITIONS,
  type CloseTradeInput,
  type LedgerSummary,
  type OpenTradeInput,
  type PaperLedger,
  type PaperPosition,
} from "./types";

import {
  differenceMultiplyToScaledInteger,
  multiplyToScaledInteger,
  productDivideToScaledInteger,
  productToScaledInteger,
  scaledIntegerToNumber,
  toScaledInteger,
} from "./decimal";

export class LedgerError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LedgerError";
  }
}

function assertFinitePositive(value: number, label: string): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new LedgerError(`${label} must be a finite positive number.`);
  }
}

function roundMoney(value: number): number {
  if (!Number.isFinite(value)) {
    throw new LedgerError("Monetary value must be finite.");
  }

  const absoluteValue = Math.abs(value);
  const scaled = (absoluteValue + Number.EPSILON) * 100;
  const roundedCents = Math.round(scaled);

  if (!Number.isSafeInteger(roundedCents)) {
    throw new LedgerError(
      "Monetary value exceeds the safe precision limit.",
    );
  }

  const roundedAmount = roundedCents / 100;

  if (roundedAmount === 0) {
    return 0;
  }

  return value < 0 ? -roundedAmount : roundedAmount;
}
function exactInputText(value: number | string): string {
  return typeof value === "string" ? value : value.toString();
}

function assertDecimalPositive(
  value: number | string,
  label: string,
): { exact: string; numeric: number } {
  const exact = exactInputText(value);

  let scaled: bigint;
  try {
    scaled = toScaledInteger(exact, 100);
  } catch {
    throw new LedgerError(`${label} has an invalid decimal representation.`);
  }

  const numeric = Number(exact);

  if (scaled <= 0n || !Number.isFinite(numeric) || numeric <= 0) {
    throw new LedgerError(`${label} must be a finite positive decimal.`);
  }

  return { exact, numeric };
}

function centsToMoney(value: bigint): number {
  try {
    return scaledIntegerToNumber(value, 2);
  } catch {
    throw new LedgerError("Monetary value exceeds the safe precision limit.");
  }
}

function moneyToCents(value: number, label: string): bigint {
  try {
    return toScaledInteger(value, 2);
  } catch {
    throw new LedgerError(`${label} exceeds the safe monetary precision limit.`);
  }
}
function isValidIsoTimestamp(value: unknown): value is string {
  if (typeof value !== "string") {
    return false;
  }

  const timestamp = Date.parse(value);

  return (
    Number.isFinite(timestamp) &&
    new Date(timestamp).toISOString() === value
  );
}

function assertV2Position(
  position: PaperPosition,
  feeRate: number,
): void {
  if (position.accountingVersion !== 2) {
    throw new LedgerError("Invalid V2 accounting version.");
  }

  if (
    typeof position.entryPriceExact !== "string" ||
    typeof position.quantityExact !== "string"
  ) {
    throw new LedgerError("V2 position is missing exact entry values.");
  }

  const entry = assertDecimalPositive(position.entryPriceExact, "Entry price");
  const quantity = assertDecimalPositive(position.quantityExact, "Quantity");

  if (
    entry.numeric !== position.entryPrice ||
    quantity.numeric !== position.quantity
  ) {
    throw new LedgerError("V2 exact entry values do not match their numeric mirrors.");
  }

  const notionalCents = multiplyToScaledInteger(
    entry.exact,
    quantity.exact,
    2,
  );

  if (notionalCents <= 0n) {
    throw new LedgerError("V2 position notional is invalid.");
  }

  const expectedMarginCents = position.market === "SPOT"
    ? notionalCents
    : productDivideToScaledInteger(
        entry.exact,
        quantity.exact,
        position.leverage,
        2,
      );

  const expectedEntryFeeCents = productToScaledInteger(
    [entry.exact, quantity.exact, feeRate],
    2,
  );

  if (
    moneyToCents(position.margin, "Position margin") !== expectedMarginCents
  ) {
    throw new LedgerError("V2 position margin does not reconcile with exact values.");
  }

  if (
    moneyToCents(position.entryFee, "Entry fee") !== expectedEntryFeeCents
  ) {
    throw new LedgerError("V2 entry fee does not reconcile with exact values.");
  }

  if (position.status === "OPEN") {
    if (
      position.exitPrice !== null ||
      position.exitPriceExact !== undefined ||
      position.exitFee !== null ||
      position.grossPnl !== null ||
      position.netPnl !== null ||
      position.closedAt !== null
    ) {
      throw new LedgerError("Open V2 position contains closing data.");
    }

    return;
  }

  if (
    position.status !== "CLOSED" ||
    typeof position.exitPriceExact !== "string"
  ) {
    throw new LedgerError("Closed V2 position is missing its exact exit price.");
  }

  if (!isValidIsoTimestamp(position.closedAt)) {
    throw new LedgerError("Closed V2 position has an invalid timestamp.");
  }

  const exit = assertDecimalPositive(position.exitPriceExact, "Exit price");

  if (exit.numeric !== position.exitPrice) {
    throw new LedgerError("V2 exact exit price does not match its numeric mirror.");
  }

  const exitNotionalCents = multiplyToScaledInteger(
    exit.exact,
    quantity.exact,
    2,
  );

  const expectedExitFeeCents = productToScaledInteger(
    [exit.exact, quantity.exact, feeRate],
    2,
  );

  const expectedGrossPnlCents = position.side === "LONG"
    ? differenceMultiplyToScaledInteger(
        exit.exact,
        entry.exact,
        quantity.exact,
        2,
      )
    : differenceMultiplyToScaledInteger(
        entry.exact,
        exit.exact,
        quantity.exact,
        2,
      );

  const expectedNetPnlCents =
    expectedGrossPnlCents -
    expectedEntryFeeCents -
    expectedExitFeeCents;

  if (
    moneyToCents(position.exitFee ?? NaN, "Exit fee") !== expectedExitFeeCents ||
    moneyToCents(position.grossPnl ?? NaN, "Gross P&L") !== expectedGrossPnlCents ||
    moneyToCents(position.netPnl ?? NaN, "Net P&L") !== expectedNetPnlCents
  ) {
    throw new LedgerError("V2 closing values do not reconcile with exact prices and fees.");
  }

  if (exitNotionalCents <= 0n) {
    throw new LedgerError("V2 exit notional is invalid.");
  }
}
function assertLedger(ledger: PaperLedger): void {
  if (
    ledger.version !== 1 ||
    !Number.isFinite(ledger.startingCapital) ||
    ledger.startingCapital <= 0 ||
    !Number.isFinite(ledger.cashBalance) ||
    ledger.cashBalance < 0 ||
    !Number.isFinite(ledger.feeRate) ||
    ledger.feeRate < 0 ||
    ledger.feeRate > 0.05 ||
    !isValidIsoTimestamp(ledger.updatedAt) || !Array.isArray(ledger.positions)
  ) {
    throw new LedgerError("Invalid paper ledger.");
  }

  // Reject monetary values that cannot be represented safely in cents.
  roundMoney(ledger.startingCapital);
  roundMoney(ledger.cashBalance);

  const ids = new Set<string>();

  for (const position of ledger.positions) {
    if (
      !position ||
      typeof position.id !== "string" ||
      !position.id ||
      ids.has(position.id) ||
      typeof position.symbol !== "string" ||
      !/^[A-Z0-9]{2,30}$/.test(position.symbol) ||
      !["SPOT", "FUTURES"].includes(position.market) ||
      !["LONG", "SHORT"].includes(position.side) ||
      position.marginMode !== "ISOLATED" ||
      !["OPEN", "CLOSED"].includes(position.status) ||
      !Number.isFinite(position.entryPrice) ||
      position.entryPrice <= 0 ||
      !Number.isFinite(position.quantity) ||
      position.quantity <= 0 ||
      !Number.isInteger(position.leverage) ||
      position.leverage < 1 ||
      position.leverage > MAX_LEVERAGE ||
      !Number.isFinite(position.margin) ||
      position.margin <= 0 ||
      !Number.isFinite(position.entryFee) ||
      position.entryFee < 0 || !isValidIsoTimestamp(position.openedAt)
    ) {
      throw new LedgerError("Ledger contains an invalid position.");
    }

    roundMoney(position.margin);
    roundMoney(position.entryFee);

    if (
      position.market === "SPOT" &&
      (position.side !== "LONG" || position.leverage !== 1)
    ) {
      throw new LedgerError("Spot positions must be LONG with 1x leverage.");
    }

    if (position.accountingVersion === 2) {
      assertV2Position(position, ledger.feeRate);
      ids.add(position.id);
      continue;
    }

    if (
      position.accountingVersion !== undefined ||
      position.entryPriceExact !== undefined ||
      position.quantityExact !== undefined ||
      position.exitPriceExact !== undefined
    ) {
      throw new LedgerError("Legacy position contains unsupported accounting metadata.");
    }

    const notional = position.entryPrice * position.quantity;

    if (!Number.isFinite(notional) || notional <= 0) {
      throw new LedgerError("Position notional is invalid.");
    }

    roundMoney(notional);

    const expectedMargin = roundMoney(
      position.market === "SPOT"
        ? notional
        : notional / position.leverage,
    );

    if (position.margin !== expectedMargin) {
      throw new LedgerError("Position margin does not reconcile with its notional and leverage.");
    }

    const expectedEntryFee = roundMoney(notional * ledger.feeRate);

    if (position.entryFee !== expectedEntryFee) {
      throw new LedgerError("Position entry fee does not reconcile with the ledger fee rate.");
    }

    if (
      position.status === "OPEN" &&
      (
        position.exitPrice !== null ||
        position.exitFee !== null ||
        position.grossPnl !== null ||
        position.netPnl !== null ||
        position.closedAt !== null
      )
    ) {
      throw new LedgerError("Open position contains closing data.");
    }

    if (position.status === "CLOSED") {
      if (
        !Number.isFinite(position.exitPrice) ||
        (position.exitPrice ?? 0) <= 0 ||
        !Number.isFinite(position.exitFee) ||
        (position.exitFee ?? -1) < 0 ||
        !Number.isFinite(position.grossPnl) ||
        !Number.isFinite(position.netPnl) ||
        !isValidIsoTimestamp(position.closedAt)
      ) {
        throw new LedgerError("Closed position contains invalid closing data.");
      }

      const exitNotional = position.exitPrice! * position.quantity;

      if (!Number.isFinite(exitNotional) || exitNotional <= 0) {
        throw new LedgerError("Closed position exit notional is invalid.");
      }

      roundMoney(exitNotional);

      const expectedExitFee = roundMoney(exitNotional * ledger.feeRate);

      if (position.exitFee !== expectedExitFee) {
        throw new LedgerError("Position exit fee does not reconcile with the ledger fee rate.");
      }

      const priceDifference = position.side === "LONG"
        ? position.exitPrice! - position.entryPrice
        : position.entryPrice - position.exitPrice!;

      const expectedGrossPnl = roundMoney(
        priceDifference * position.quantity,
      );

      if (position.grossPnl !== expectedGrossPnl) {
        throw new LedgerError("Position gross P&L does not reconcile with its prices.");
      }

      const expectedNetPnl = roundMoney(
        expectedGrossPnl - position.entryFee - expectedExitFee,
      );

      if (position.netPnl !== expectedNetPnl) {
        throw new LedgerError("Position net P&L does not reconcile with its fees.");
      }
    }

    ids.add(position.id);
  }

  let openPositionCostsCents = 0n;
  let closedNetPnlCents = 0n;

  for (const position of ledger.positions) {
    if (position.status === "OPEN") {
      openPositionCostsCents += moneyToCents(position.margin, "Position margin");
      openPositionCostsCents += moneyToCents(position.entryFee, "Entry fee");
    } else {
      closedNetPnlCents += moneyToCents(position.netPnl ?? 0, "Net P&L");
    }
  }

  const expectedCashBalanceCents =
    moneyToCents(ledger.startingCapital, "Starting capital") -
    openPositionCostsCents +
    closedNetPnlCents;

  const expectedCashBalance = centsToMoney(expectedCashBalanceCents);

  if (ledger.cashBalance !== expectedCashBalance) {
    throw new LedgerError(
      "Wallet cash balance does not reconcile with starting capital and position history.",
    );
  }
  if (ledger.positions.length > 10_000) {
    throw new LedgerError("Ledger history limit exceeded.");
  }

  if (ledger.positions.filter(p => p.status === "OPEN").length > MAX_POSITIONS) {
    throw new LedgerError("Open position limit exceeded.");
  }
}
export function serializeLedger(ledger: PaperLedger): string {
  assertLedger(ledger);

  return JSON.stringify(ledger);
}

export function parseLedger(serialized: string): PaperLedger {
  if (typeof serialized !== "string") {
    throw new LedgerError("Stored paper ledger must be a JSON string.");
  }

  let parsed: unknown;

  try {
    parsed = JSON.parse(serialized);
  } catch {
    throw new LedgerError("Stored paper ledger contains invalid JSON.");
  }

  if (
    parsed === null ||
    typeof parsed !== "object" ||
    Array.isArray(parsed)
  ) {
    throw new LedgerError("Stored paper ledger has an invalid root structure.");
  }

  assertLedger(parsed as PaperLedger);

  return parsed as PaperLedger;
}
export function createLedger(
  startingCapital = DEFAULT_STARTING_CAPITAL,
  feeRate = DEFAULT_FEE_RATE,
): PaperLedger {
  assertFinitePositive(startingCapital, "Starting capital");

  if (!Number.isFinite(feeRate) || feeRate < 0 || feeRate > 0.05) {
    throw new LedgerError("Fee rate must be between 0 and 0.05.");
  }

  const normalizedCapital = roundMoney(startingCapital);

  assertFinitePositive(
    normalizedCapital,
    "Rounded starting capital",
  );

  return {
    version: 1,
    startingCapital: normalizedCapital,
    cashBalance: normalizedCapital,
    feeRate,
    positions: [],
    updatedAt: new Date().toISOString(),
  };
}

export function refreshPaperBalance(
  ledger: PaperLedger,
  targetBalance: number = 10_000,
  refreshThreshold: number = 100,
): PaperLedger {
  assertLedger(ledger);
  assertFinitePositive(targetBalance, "Target balance");
  assertFinitePositive(refreshThreshold, "Refresh threshold");

  const currentCashCents = moneyToCents(
    ledger.cashBalance,
    "Wallet balance",
  );

  const targetCents = moneyToCents(
    roundMoney(targetBalance),
    "Target balance",
  );

  const thresholdCents = moneyToCents(
    roundMoney(refreshThreshold),
    "Refresh threshold",
  );

  if (targetCents <= thresholdCents) {
    throw new LedgerError(
      "Target balance must be greater than the refresh threshold.",
    );
  }

  if (currentCashCents > thresholdCents) {
    return ledger;
  }

  const creditCents = targetCents - currentCashCents;

  const nextLedger: PaperLedger = {
    ...ledger,
    startingCapital: centsToMoney(
      moneyToCents(ledger.startingCapital, "Starting capital") +
      creditCents,
    ),
    cashBalance: centsToMoney(
      currentCashCents + creditCents,
    ),
    updatedAt: new Date().toISOString(),
  };

  assertLedger(nextLedger);

  return nextLedger;
}
export function openPaperPosition(
  ledger: PaperLedger,
  input: OpenTradeInput,
): PaperLedger {
  assertLedger(ledger);

  const symbol = input.symbol.trim().toUpperCase();

  if (!/^[A-Z0-9]{2,30}$/.test(symbol)) {
    throw new LedgerError("Invalid symbol format.");
  }

  if (!["SPOT", "FUTURES"].includes(input.market)) {
    throw new LedgerError("Invalid market.");
  }

  if (!["LONG", "SHORT"].includes(input.side)) {
    throw new LedgerError("Invalid trade side.");
  }

  const entry = assertDecimalPositive(input.entryPrice, "Entry price");
  const quantity = assertDecimalPositive(input.quantity, "Quantity");

  if (
    !Number.isInteger(input.leverage) ||
    input.leverage < 1 ||
    input.leverage > MAX_LEVERAGE
  ) {
    throw new LedgerError(`Leverage must be an integer between 1 and ${MAX_LEVERAGE}.`);
  }

  if (
    input.market === "SPOT" &&
    (input.side !== "LONG" || input.leverage !== 1)
  ) {
    throw new LedgerError("Spot supports LONG positions at 1x only.");
  }

  if (ledger.positions.filter(p => p.status === "OPEN").length >= MAX_POSITIONS) {
    throw new LedgerError("Maximum open position limit reached.");
  }

  let notionalCents: bigint;
  let marginCents: bigint;
  let entryFeeCents: bigint;

  try {
    notionalCents = multiplyToScaledInteger(
      entry.exact,
      quantity.exact,
      2,
    );

    marginCents = input.market === "SPOT"
      ? notionalCents
      : productDivideToScaledInteger(
          entry.exact,
          quantity.exact,
          input.leverage,
          2,
        );

    entryFeeCents = productToScaledInteger(
      [entry.exact, quantity.exact, ledger.feeRate],
      2,
    );
  } catch {
    throw new LedgerError("Trade calculations exceed supported decimal precision.");
  }

  if (notionalCents <= 0n || marginCents <= 0n) {
    throw new LedgerError("Trade notional or margin is below supported precision.");
  }

  const margin = centsToMoney(marginCents);
  const entryFee = centsToMoney(entryFeeCents);
  const requiredCashCents = marginCents + entryFeeCents;
  const cashBalanceCents = moneyToCents(ledger.cashBalance, "Wallet balance");

  if (requiredCashCents > cashBalanceCents) {
    throw new LedgerError("Insufficient paper wallet balance.");
  }

  const requiredCash = centsToMoney(requiredCashCents);
  const entryPrice = entry.numeric;
  const numericQuantity = quantity.numeric;

  const position: PaperPosition = {
    id: crypto.randomUUID(),
    symbol,
    market: input.market,
    side: input.side,
    marginMode: "ISOLATED",
    entryPrice,
    quantity: numericQuantity,
    accountingVersion: 2,
    entryPriceExact: entry.exact,
    quantityExact: quantity.exact,
    leverage: input.leverage,
    margin,
    entryFee,
    exitPrice: null,
    exitFee: null,
    grossPnl: null,
    netPnl: null,
    status: "OPEN",
    openedAt: new Date().toISOString(),
    closedAt: null,
  };

  const nextLedger: PaperLedger = {
    ...ledger,
    cashBalance: centsToMoney(cashBalanceCents - requiredCashCents),
    positions: [...ledger.positions, position],
    updatedAt: new Date().toISOString(),
  };

  assertLedger(nextLedger);
  return nextLedger;
}
export function closePaperPosition(
  ledger: PaperLedger,
  input: CloseTradeInput,
): PaperLedger {
  assertLedger(ledger);

  const index = ledger.positions.findIndex(
    p => p.id === input.positionId && p.status === "OPEN",
  );

  if (index === -1) {
    throw new LedgerError("Open position not found.");
  }

  const position = ledger.positions[index];
  const exit = assertDecimalPositive(input.exitPrice, "Exit price");

  let exitFee: number;
  let grossPnl: number;
  let netPnl: number;
  let cashReleased: number;

  if (position.accountingVersion === 2) {
    const entryExact = position.entryPriceExact!;
    const quantityExact = position.quantityExact!;

    let exitNotionalCents: bigint;
    let exitFeeCents: bigint;
    let grossPnlCents: bigint;

    try {
      exitNotionalCents = multiplyToScaledInteger(
        exit.exact,
        quantityExact,
        2,
      );

      exitFeeCents = productToScaledInteger(
        [exit.exact, quantityExact, ledger.feeRate],
        2,
      );

      grossPnlCents = position.side === "LONG"
        ? differenceMultiplyToScaledInteger(
            exit.exact,
            entryExact,
            quantityExact,
            2,
          )
        : differenceMultiplyToScaledInteger(
            entryExact,
            exit.exact,
            quantityExact,
            2,
          );
    } catch {
      throw new LedgerError("Exit calculations exceed supported decimal precision.");
    }

    if (exitNotionalCents <= 0n) {
      throw new LedgerError("Exit notional is invalid.");
    }

    const entryFeeCents = moneyToCents(position.entryFee, "Entry fee");
    const netPnlCents = grossPnlCents - entryFeeCents - exitFeeCents;

    exitFee = centsToMoney(exitFeeCents);
    grossPnl = centsToMoney(grossPnlCents);
    netPnl = centsToMoney(netPnlCents);

    let cashReleasedCents: bigint;

    if (position.market === "SPOT") {
      cashReleasedCents = exitNotionalCents - exitFeeCents;
    } else {
      const isolatedResultCents =
        moneyToCents(position.margin, "Position margin") + grossPnlCents;

      if (isolatedResultCents < 0n) {
        throw new LedgerError(
          "Loss exceeds isolated margin. Liquidation simulation is not yet implemented; position cannot be closed below its modeled margin.",
        );
      }

      cashReleasedCents = isolatedResultCents - exitFeeCents;
    }

    if (cashReleasedCents < 0n) {
      throw new LedgerError("Exit fee exceeds the funds available for release.");
    }

    cashReleased = centsToMoney(cashReleasedCents);
  } else {
    // Preserve legacy V1 Number-based accounting.
    const exitPrice = exit.numeric;
    const exitNotional = exitPrice * position.quantity;

    if (!Number.isFinite(exitNotional) || exitNotional <= 0) {
      throw new LedgerError("Exit notional is invalid.");
    }

    const legacyExitFee = roundMoney(exitNotional * ledger.feeRate);
    const priceDifference = position.side === "LONG"
      ? exitPrice - position.entryPrice
      : position.entryPrice - exitPrice;

    const legacyGrossPnl = roundMoney(priceDifference * position.quantity);
    const legacyNetPnl = roundMoney(
      legacyGrossPnl - position.entryFee - legacyExitFee,
    );

    exitFee = legacyExitFee;
    grossPnl = legacyGrossPnl;
    netPnl = legacyNetPnl;

    if (position.market === "SPOT") {
      cashReleased = roundMoney(exitNotional - exitFee);
    } else {
      const isolatedResult = roundMoney(position.margin + grossPnl);

      if (isolatedResult < 0) {
        throw new LedgerError(
          "Loss exceeds isolated margin. Liquidation simulation is not yet implemented; position cannot be closed below its modeled margin.",
        );
      }

      cashReleased = roundMoney(isolatedResult - exitFee);
    }

    if (cashReleased < 0) {
      throw new LedgerError("Exit fee exceeds the funds available for release.");
    }
  }

  const closedPosition: PaperPosition = {
    ...position,
    exitPrice: exit.numeric,
    ...(position.accountingVersion === 2
      ? { exitPriceExact: exit.exact }
      : {}),
    exitFee,
    grossPnl,
    netPnl,
    status: "CLOSED",
    closedAt: new Date().toISOString(),
  };

  const positions = [...ledger.positions];
  positions[index] = closedPosition;

  const nextLedger: PaperLedger = {
    ...ledger,
    cashBalance: centsToMoney(
      moneyToCents(ledger.cashBalance, "Wallet balance") +
      moneyToCents(cashReleased, "Cash released"),
    ),
    positions,
    updatedAt: new Date().toISOString(),
  };

  assertLedger(nextLedger);
  return nextLedger;
}
export function calculateLedgerSummary(
  ledger: PaperLedger,
): LedgerSummary {
  assertLedger(ledger);

  const openPositions = ledger.positions.filter(p => p.status === "OPEN");
  const closedPositions = ledger.positions.filter(p => p.status === "CLOSED");

  let lockedCapitalCents = 0n;
  let realizedPnlCents = 0n;
  let totalFeesCents = 0n;

  for (const position of openPositions) {
    lockedCapitalCents += moneyToCents(position.margin, "Locked capital");
  }

  for (const position of closedPositions) {
    realizedPnlCents += moneyToCents(position.netPnl ?? 0, "Realized P&L");
  }

  for (const position of ledger.positions) {
    totalFeesCents += moneyToCents(position.entryFee, "Entry fee");
    totalFeesCents += moneyToCents(position.exitFee ?? 0, "Exit fee");
  }

  const lockedCapital = centsToMoney(lockedCapitalCents);
  const realizedPnl = centsToMoney(realizedPnlCents);
  const totalFees = centsToMoney(totalFeesCents);

  return {
    cashBalance: ledger.cashBalance,
    lockedCapital,
    openPositions: openPositions.length,
    closedPositions: closedPositions.length,
    realizedPnl,
    totalFees,
    unrealizedPnl: null,
    equity: null,
    valuationStatus: "UNAVAILABLE_WITHOUT_VERIFIED_MARK_PRICE",
  };
}