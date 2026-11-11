import { LedgerError, parseLedger, serializeLedger } from "./ledger";
import type { PaperLedger } from "./types";

export const PAPER_LEDGER_STORAGE_KEY = "zak-tv-paper-ledger-v1";

export interface LedgerStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function assertStorageAvailable(storage: LedgerStorage): void {
  if (
    !storage ||
    typeof storage.getItem !== "function" ||
    typeof storage.setItem !== "function"
  ) {
    throw new LedgerError("Paper ledger storage is unavailable.");
  }
}

export function loadLedger(
  storage: LedgerStorage,
  key = PAPER_LEDGER_STORAGE_KEY,
): PaperLedger | null {
  assertStorageAvailable(storage);

  let serialized: string | null;

  try {
    serialized = storage.getItem(key);
  } catch {
    throw new LedgerError("Unable to read the saved paper ledger.");
  }

  if (serialized === null) {
    return null;
  }

  // Invalid stored data is rejected and never silently replaced.
  return parseLedger(serialized);
}

export function saveLedger(
  storage: LedgerStorage,
  ledger: PaperLedger,
  key = PAPER_LEDGER_STORAGE_KEY,
): void {
  assertStorageAvailable(storage);

  // Validate and serialize before touching existing stored data.
  const serialized = serializeLedger(ledger);

  try {
    storage.setItem(key, serialized);
  } catch {
    throw new LedgerError("Unable to save the paper ledger.");
  }
}