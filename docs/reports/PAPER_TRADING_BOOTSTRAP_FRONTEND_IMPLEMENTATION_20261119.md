# ZaK Trading View — Paper Trading Bootstrap & Frontend Integration

Generated: 2026-11-19

## Scope implemented in this patch

### Backend bootstrap contract
- Added authenticated `GET /api/v1/paper-trading/bootstrap`.
- Uses the authenticated user identity and the existing `get_db_session` dependency.
- Does not provision accounts, create sessions, fund balances, or mutate any financial record.
- Resolves exactly one active account membership and exactly one Spot and Futures portfolio.
- Enforces `Permission.PORTFOLIO_READ` through `authorize_portfolio_permission`.
- Requires one active session per portfolio.
- Calls `validate_existing_paper_ledger` for both active books before returning any balances.
- Reads the six account snapshots and positions from the validated active book.
- Serializes financial Decimal values as exact strings and timestamps as ISO-8601 strings.
- Returns generic errors for missing/unauthorized account, inconsistent financial state, and database unavailability.
- Sets `Cache-Control: no-store` and `Pragma: no-cache`.

### Next.js BFF
- Added authenticated `GET /api/paper-trading/bootstrap`.
- Added only the exact backend GET path to the internal API allowlist.
- Reuses the existing session cookie bridge and server-only internal service transport.
- Validates the response envelope, maps upstream errors safely, and returns no-store headers.
- Added mock API support and Playwright coverage for authenticated/unauthenticated bootstrap, allowlist exactness, upstream failures, and malformed responses.

### Workspace
- Replaced localStorage-backed financial state with the authenticated bootstrap response.
- Removed browser-side account creation, trade simulation, manual price entry, local balance refresh, and local position closing.
- Spot Buy/Sell now submits to the existing backend Spot market-order endpoint with a fresh UUID v4 idempotency key.
- The client does not send an execution price; the backend remains responsible for price, fee and accounting.
- After a successful Spot trade, the UI reloads the authoritative bootstrap snapshot.
- Futures portfolios and positions are displayed read-only because no Futures execution API exists in the source snapshot.
- Reset and Manual Top-up controls remain disabled because their backend APIs are not implemented.
- Existing browser localStorage data is not deleted or overwritten; it is no longer used as financial truth.

## Verification performed in this environment

### Backend
Command:
`python -m pytest -q tests/test_paper_trading_api.py tests/test_paper_trading_bootstrap_api.py tests/test_paper_trading_bootstrap_service.py tests/test_paper_ledger_validation.py tests/test_paper_ledger_rules.py tests/test_paper_ledger_provisioning.py tests/test_paper_trade_execution.py tests/test_paper_trade_calculations.py tests/test_paper_authorization.py tests/test_account_authorization.py tests/test_paper_write_authorization.py tests/test_paper_idempotency.py tests/test_paper_transaction_journal.py tests/test_paper_trade_fingerprint.py`

Result: **259 passed** (the combined focused Paper Trading/API suite, including the additional missing-snapshot case).

### Frontend
Command:
`npm run typecheck`

Result: **passed** after the BFF route, allowlist, Playwright test changes, workspace rewrite, and removal of an unused server-only import from the Playwright test module.

### Build / browser E2E limitation
A fresh Next.js build could not run in this Linux validation container because the supplied `node_modules` contains only the Windows SWC native package (`@next/swc-win32-x64-msvc`). Next.js attempted to download the Linux SWC package, but network access to the npm registry was unavailable. Therefore no fresh Next.js build or Playwright E2E result is claimed here.

The uploaded historical Playwright report showed 404/HTML responses for the pre-existing Spot BFF tests. The route source existed, but the report used `next start`; a fresh build is required after applying route changes so the `.next` route manifest is current. An unused import of the `server-only` internal API client was also removed from the Playwright test module because importing it directly caused the earlier test-loader failure.

## Required verification on the Windows project machine

1. Run the backend focused pytest command from `D:\ZaK Trading View\apps\api`.
2. Run `npm run typecheck` from `D:\ZaK Trading View\apps\web`.
3. Run a fresh `npm run build` from `D:\ZaK Trading View\apps\web`.
4. Only after the fresh build succeeds, run `npx playwright test tests/e2e/auth.spec.ts`.
5. If the Spot BFF tests still return 404, inspect the newly generated `.next/server/app-paths-manifest.json` and route build output before changing route logic.

## Paper Trading nine-step checklist

1) Existing Implementation Audit — VERIFIED
2) Architecture & Data Model — VERIFIED
3) API & Service Contracts — PARTIAL
4) Ledger & Snapshot Integrity — VERIFIED
5) Position Accounting & Fees — VERIFIED
6) Execution Request Ownership — PARTIAL
7) UI/UX Paper Trading Controls — PARTIAL
8) Tests & Acceptance Criteria — PARTIAL
9) Documentation & Final Acceptance — PENDING

## Remaining acceptance work
- Run a real PostgreSQL-backed bootstrap integration test against the user's configured development database.
- Complete a fresh Windows Next.js build and the Playwright E2E suite.
- Implement and test Futures execution before enabling Futures trading controls.
- Implement Reset and Manual Top-up backend operations with the approved idempotent ledger/session policies before enabling those controls.
- Complete full regression/security verification and final acceptance documentation.
